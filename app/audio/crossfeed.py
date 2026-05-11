"""Bauer-style crossfeed for headphone listening.

Hard-panned stereo (most pre-stereo-imaging-aware mixes from the 60s
and 70s, plus a lot of jazz, classical orchestral) sounds aggressive
on headphones because each ear hears one channel without the natural
cross-channel cues a speaker pair provides. Crossfeed bleeds a low-
passed copy of each channel into the opposite ear at low frequencies,
preserving the high-frequency stereo image while pulling the bass
toward the centre — the same imaging trick "Bauerstereophonic" was
proposing in the 60s.

Implementation notes:

* Sum-and-difference path. We low-pass each channel, then blend the
  two low-passes between L and R according to the user's amount.
  Highs (`L - L_low`) pass through untouched, so cymbals / consonants
  don't smear.
* Cutoff: 700 Hz, 2nd-order Butterworth. That's the "classic Bauer"
  setting; deeper than the Meier 600 Hz but lighter than the
  Linkwitz 750 Hz. Good middle ground.
* No delay. Time-aligned crossfeed (HRTF) is a separate, larger
  feature; this module is the simplified "level + filter only"
  variant the audiophile community typically means by "crossfeed".
* Stereo only. Mono / multichannel passes through untouched.

Public API mirrors `Equalizer`: configure, apply in-place. The
audio callback applies it after the EQ stage so frequency-response
correction lands first, imaging adjustment second.
"""
from __future__ import annotations

import threading
from typing import Optional

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi  # type: ignore


# Cutoff for the low-pass that defines what counts as "low frequencies"
# for the crossfeed bleed. Highs above this stay channel-isolated;
# lows below this get blended.
_CUTOFF_HZ = 700.0

# Filter order. 2nd-order Butterworth gives a 12 dB/oct rolloff at the
# cutoff — gentle enough that the transition isn't audible as a
# resonance, steep enough to actually separate the bands.
_FILTER_ORDER = 2


class Crossfeed:
    """Stateful Bauer-style crossfeed.

    Thread model: `set_amount` / `clear` are called from the HTTP
    handler thread; `apply` is called from the audio callback. A
    short lock serialises coefficient + state swaps so the callback
    never sees a half-installed filter.

    Stereo-only — `apply` checks `samples.shape[1] == 2` and no-ops
    anything else. Mono / 5.1 / etc. pass through.
    """

    def __init__(self, sample_rate: int):
        self._sample_rate = int(sample_rate)
        self._lock = threading.Lock()
        # SOS coefficients for the shared low-pass. `None` = bypass.
        # Lazily built on first non-zero `set_amount` and reused across
        # subsequent amount changes — the SOS depends only on the
        # sample rate, which is fixed for the player's lifetime.
        self._sos: Optional[np.ndarray] = None
        # Independent filter state per channel — both biquad chains
        # walk along their channel's samples; sharing state would
        # cross-pollute and cause an audible "wandering" image.
        self._state_l: Optional[np.ndarray] = None
        self._state_r: Optional[np.ndarray] = None
        # Amount as a 0..1 alpha. 0 = bypass; 0.5 = lows fully mono'd
        # (each ear gets the average of L_low and R_low). Typical
        # tasteful settings are 0.2-0.4.
        self._amount: float = 0.0

    # --- configuration ----------------------------------------------

    def set_amount(self, amount_pct: int) -> None:
        """Set crossfeed strength as a percent (0-100). 0 disables;
        any non-zero value installs the low-pass + state. Repeated
        calls with different non-zero amounts only mutate `_amount`,
        keeping the cached SOS coefficients and the existing filter
        state — the response is smooth across user adjustments
        without re-settling transients and without rebuilding the
        Butterworth coefficients on every slider tick."""
        clamped = max(0, min(100, int(amount_pct))) / 100.0
        if clamped <= 0.0:
            self.clear()
            return
        with self._lock:
            if self._sos is None:
                # First non-zero amount: build + cache the SOS and
                # initialise per-channel filter state.
                self._sos = self._build_sos().astype(np.float32, copy=False)
                zi_single = sosfilt_zi(self._sos)
                self._state_l = zi_single.astype(np.float32, copy=True)
                self._state_r = zi_single.astype(np.float32, copy=True)
            self._amount = clamped

    def clear(self) -> None:
        """Disable crossfeed. `apply()` becomes a pass-through."""
        with self._lock:
            self._sos = None
            self._state_l = None
            self._state_r = None
            self._amount = 0.0

    def is_active(self) -> bool:
        return self._sos is not None

    def amount(self) -> int:
        """Current strength as a 0-100 percent. 0 when bypassed."""
        return int(round(self._amount * 100.0))

    # --- filtering --------------------------------------------------

    def apply(self, samples: np.ndarray) -> None:
        """Blend low-frequency content between L and R in place.
        Mono / non-stereo input is passed through untouched (the
        whole point is stereo image adjustment, so a mono stream
        has nothing to adjust).

        Float32 only — same constraint and rationale as the EQ.
        """
        if self._sos is None or self._state_l is None or self._state_r is None:
            return
        if samples.ndim != 2 or samples.shape[1] != 2:
            # Mono / 5.1 / etc. — the crossfeed math is stereo-only.
            return
        with self._lock:
            l = samples[:, 0]
            r = samples[:, 1]
            l_low, self._state_l = sosfilt(self._sos, l, zi=self._state_l)
            r_low, self._state_r = sosfilt(self._sos, r, zi=self._state_r)
            l_high = l - l_low
            r_high = r - r_low
            alpha = self._amount
            # L_out = L_high + (1-α)*L_low + α*R_low
            # R_out = R_high + (1-α)*R_low + α*L_low
            #
            # At α=0 this simplifies to L_high + L_low = L (perfect
            # bypass). At α=0.5 the two low-pass paths land equal-
            # weighted on each output, producing mono lows.
            samples[:, 0] = l_high + (1.0 - alpha) * l_low + alpha * r_low
            samples[:, 1] = r_high + (1.0 - alpha) * r_low + alpha * l_low

    # --- internals --------------------------------------------------

    def _build_sos(self) -> np.ndarray:
        """Compute SOS for the shared low-pass at this sample rate.
        SciPy's Butterworth in SOS form is numerically stable across
        the rates we hit (44.1 / 48 / 88.2 / 96 / 176.4 / 192 kHz)."""
        return butter(
            _FILTER_ORDER,
            _CUTOFF_HZ,
            btype="low",
            fs=self._sample_rate,
            output="sos",
        )
