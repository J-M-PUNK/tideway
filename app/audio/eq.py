"""10-band parametric EQ for the PCM engine.

Each band is a peaking biquad (RBJ Audio EQ Cookbook, "constant-Q
peaking") centered at the standard iso frequencies. Coefficients
are packed as second-order sections and filtered via
`scipy.signal.sosfilt` (C-speed; a pure-Python loop over 10 bands
× 1024 samples × 2 channels won't hit the audio callback's ~20ms
deadline).

Public API deliberately matches the shape the VLC path uses so
server.py and the settings UI can stay engine-agnostic:

    eq = Equalizer(sample_rate)
    eq.set_bands([2.0, 1.0, 0, 0, 0, 0, 0, 0, 0, 0], preamp=-1.0)
    eq.apply(outdata)            # in-place filter
    eq.clear()                   # disable (pass-through)
"""
from __future__ import annotations

import math
import threading
from typing import Optional

import numpy as np
from scipy.signal import sosfilt, sosfilt_zi  # type: ignore


# ISO-standard 10-band center frequencies. One peaking biquad
# centered at each, rendered as a slider in the Settings UI.
BAND_FREQUENCIES_HZ: tuple[float, ...] = (
    60.0, 170.0, 310.0, 600.0, 1000.0,
    3000.0, 6000.0, 12000.0, 14000.0, 16000.0,
)

# Per-band Q. Picked so that the filter skirts overlap enough that
# a flat curve across adjacent bands produces a smooth result (no
# "ripples" in the combined response) but each band is selective
# enough to be audibly distinct.
BAND_Q: float = 1.41


class Equalizer:
    """Stateful 10-band biquad EQ.

    Thread-model: `set_bands` / `clear` are called from the HTTP
    handler thread; `apply` is called from the sounddevice audio
    callback thread. A short lock serialises coefficient updates
    so the callback never sees half-written SOS state.
    """

    BAND_COUNT = len(BAND_FREQUENCIES_HZ)

    def __init__(self, sample_rate: int, channels: int = 2):
        self._sample_rate = int(sample_rate)
        self._channels = int(channels)
        self._lock = threading.Lock()
        # (BAND_COUNT, 6) SOS matrix; None = EQ disabled (bypass).
        self._sos: Optional[np.ndarray] = None
        # Filter state per band per channel, shape (BAND_COUNT, 2, channels).
        self._state: Optional[np.ndarray] = None
        # Preamp as a linear multiplier (10 ** (dB/20)). 1.0 = unity.
        self._preamp_linear: float = 1.0

    # --- configuration ----------------------------------------------

    def set_bands(
        self, bands: list[float], preamp_db: Optional[float] = None
    ) -> None:
        """Replace active coefficients with a curve built from
        `bands` (gain in dB per band) and `preamp_db`. Empty `bands`
        disables the EQ entirely."""
        if not bands:
            self.clear()
            return
        if len(bands) != self.BAND_COUNT:
            raise ValueError(
                f"expected {self.BAND_COUNT} bands, got {len(bands)}"
            )
        sos = _build_sos(bands, self._sample_rate)
        # Zero-initialized state — same shape sosfilt expects when
        # using zi= with an axis. sosfilt_zi gives steady-state
        # initial conditions for a DC input, which is the right
        # starting point for audio that begins from silence.
        zi_single = sosfilt_zi(sos)  # shape (BAND_COUNT, 2)
        # Broadcast across the channel axis: (BAND_COUNT, 2, channels)
        state = np.tile(
            zi_single[:, :, None], (1, 1, self._channels)
        ).astype(np.float32, copy=False)
        preamp = 1.0 if preamp_db is None else 10.0 ** (float(preamp_db) / 20.0)
        with self._lock:
            self._sos = sos
            self._state = state
            self._preamp_linear = preamp

    def clear(self) -> None:
        """Disable filtering. `apply()` becomes a pass-through."""
        with self._lock:
            self._sos = None
            self._state = None
            self._preamp_linear = 1.0

    def is_active(self) -> bool:
        return self._sos is not None

    def sample_rate(self) -> int:
        return self._sample_rate

    # --- filtering --------------------------------------------------

    def apply(self, samples: np.ndarray) -> None:
        """Filter `samples` (shape (N, channels), dtype float32) in
        place. No-op when inactive. Called from the audio callback,
        so must be fast and cannot block for long.

        Only accepts float32 because applying an EQ to int PCM
        requires converting to float first anyway — caller does the
        int16/int32 ↔ float32 hop when the engine is in
        bit-perfect mode with a non-flat EQ.
        """
        if self._sos is None or self._state is None:
            return
        with self._lock:
            if self._preamp_linear != 1.0:
                samples *= self._preamp_linear
            # sosfilt operates along one axis; stereo is (N, 2) so
            # we filter each channel by passing axis=0.
            filtered, self._state = sosfilt(
                self._sos, samples, axis=0, zi=self._state
            )
            samples[:] = filtered.astype(samples.dtype, copy=False)


def _build_sos(bands_db: list[float], sample_rate: int) -> np.ndarray:
    """Compute a (BAND_COUNT, 6) SOS matrix for the given per-band
    gains (dB). Each row = one peaking biquad at BAND_FREQUENCIES_HZ[i]
    with gain bands_db[i]."""
    sos = np.empty((len(bands_db), 6), dtype=np.float32)
    for i, gain_db in enumerate(bands_db):
        sos[i] = _peaking_biquad(
            BAND_FREQUENCIES_HZ[i], float(gain_db), BAND_Q, sample_rate
        )
    return sos


def _peaking_biquad(
    freq_hz: float, gain_db: float, q: float, sample_rate: int
) -> np.ndarray:
    """RBJ Audio EQ Cookbook peaking biquad. Returns (b0 b1 b2 a0 a1 a2)
    normalized so a0 == 1 (what scipy's sosfilt expects)."""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * freq_hz / sample_rate
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / (2.0 * q)

    b0 = 1.0 + alpha * A
    b1 = -2.0 * cos_w0
    b2 = 1.0 - alpha * A
    a0 = 1.0 + alpha / A
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha / A

    return np.array(
        [b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0],
        dtype=np.float32,
    )


# Shelf-biquad helpers used by the upcoming AutoEQ headphone-profile
# path (see `docs/autoeq-headphone-profiles-scope.md`). The manual
# 10-band UI stays peaking-only; these are an internal capability the
# profile cascade builder will draw on.
#
# `q` for shelves follows RBJ's "S" slope convention rather than the
# bandwidth-Q used for peaking. AutoEQ's `*ParametricEQ.txt` files
# emit `Q` values that are slope-Q under that convention, so the
# numeric value parses through directly.


def _low_shelf_biquad(
    freq_hz: float, gain_db: float, q: float, sample_rate: int
) -> np.ndarray:
    """RBJ Audio EQ Cookbook low-shelf biquad. Boosts (or cuts) a
    region below `freq_hz` by `gain_db`; response approaches 0 dB
    well above the corner. Coefficients packed (b0 b1 b2 a0 a1 a2)
    normalised so a0 == 1."""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * freq_hz / sample_rate
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    # alpha derivation per the RBJ cookbook's shelf form:
    #   alpha = sin(w0)/2 * sqrt((A + 1/A)*(1/S - 1) + 2)
    # with S being slope (1 = max steepness without overshoot).
    # The Q parameter here IS S — AutoEQ's files use this convention.
    alpha = (
        sin_w0
        / 2.0
        * math.sqrt(max(0.0, (A + 1.0 / A) * (1.0 / q - 1.0) + 2.0))
    )
    sqrt_A = math.sqrt(A)
    two_sqrt_A_alpha = 2.0 * sqrt_A * alpha

    b0 = A * ((A + 1.0) - (A - 1.0) * cos_w0 + two_sqrt_A_alpha)
    b1 = 2.0 * A * ((A - 1.0) - (A + 1.0) * cos_w0)
    b2 = A * ((A + 1.0) - (A - 1.0) * cos_w0 - two_sqrt_A_alpha)
    a0 = (A + 1.0) + (A - 1.0) * cos_w0 + two_sqrt_A_alpha
    a1 = -2.0 * ((A - 1.0) + (A + 1.0) * cos_w0)
    a2 = (A + 1.0) + (A - 1.0) * cos_w0 - two_sqrt_A_alpha

    return np.array(
        [b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0],
        dtype=np.float32,
    )


def _high_shelf_biquad(
    freq_hz: float, gain_db: float, q: float, sample_rate: int
) -> np.ndarray:
    """RBJ Audio EQ Cookbook high-shelf biquad. Boosts (or cuts) a
    region above `freq_hz` by `gain_db`; response approaches 0 dB
    well below the corner. Mirrors `_low_shelf_biquad` with the
    cosine-sign flips the cookbook specifies for the high-shelf
    form."""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * freq_hz / sample_rate
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = (
        sin_w0
        / 2.0
        * math.sqrt(max(0.0, (A + 1.0 / A) * (1.0 / q - 1.0) + 2.0))
    )
    sqrt_A = math.sqrt(A)
    two_sqrt_A_alpha = 2.0 * sqrt_A * alpha

    b0 = A * ((A + 1.0) + (A - 1.0) * cos_w0 + two_sqrt_A_alpha)
    b1 = -2.0 * A * ((A - 1.0) + (A + 1.0) * cos_w0)
    b2 = A * ((A + 1.0) + (A - 1.0) * cos_w0 - two_sqrt_A_alpha)
    a0 = (A + 1.0) - (A - 1.0) * cos_w0 + two_sqrt_A_alpha
    a1 = 2.0 * ((A - 1.0) - (A + 1.0) * cos_w0)
    a2 = (A + 1.0) - (A - 1.0) * cos_w0 - two_sqrt_A_alpha

    return np.array(
        [b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0],
        dtype=np.float32,
    )


# AutoEQ ParametricEQ.txt files identify each band by a two- or
# three-letter type code. The set we need to support is small.
FilterType = str  # "PK" (peaking) | "LSC" (low shelf) | "HSC" (high shelf)
PEAKING: FilterType = "PK"
LOW_SHELF: FilterType = "LSC"
HIGH_SHELF: FilterType = "HSC"


def _compute_biquad(
    filter_type: FilterType,
    freq_hz: float,
    gain_db: float,
    q: float,
    sample_rate: int,
) -> np.ndarray:
    """Dispatch to the right biquad helper for the AutoEQ profile
    cascade. Raises ValueError on unknown filter types so a typo'd
    profile fails loudly instead of silently producing a flat
    response."""
    if filter_type == PEAKING:
        return _peaking_biquad(freq_hz, gain_db, q, sample_rate)
    if filter_type == LOW_SHELF:
        return _low_shelf_biquad(freq_hz, gain_db, q, sample_rate)
    if filter_type == HIGH_SHELF:
        return _high_shelf_biquad(freq_hz, gain_db, q, sample_rate)
    raise ValueError(f"unknown biquad filter type: {filter_type!r}")


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------
# Curated by-ear presets. Kept minimal so users don't wade through
# 20 variants of "a little bit more bass." Each value is gain in dB
# per band at BAND_FREQUENCIES_HZ.

PRESETS: list[dict] = [
    {"index": 0, "name": "Flat",
     "bands": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
    {"index": 1, "name": "Bass Boost",
     "bands": [6, 5, 3, 1, 0, 0, 0, 0, 0, 0]},
    {"index": 2, "name": "Treble Boost",
     "bands": [0, 0, 0, 0, 0, 1, 3, 5, 6, 6]},
    {"index": 3, "name": "Bass + Treble",
     "bands": [5, 4, 2, 0, 0, 0, 2, 4, 5, 5]},
    {"index": 4, "name": "Vocal Boost",
     "bands": [-2, -1, 0, 2, 4, 3, 2, 0, 0, 0]},
    {"index": 5, "name": "Loudness",
     "bands": [5, 3, 0, 0, -1, 0, 0, 2, 4, 5]},
    {"index": 6, "name": "Classical",
     "bands": [0, 0, 0, 0, 0, 0, -3, -3, -3, -5]},
    {"index": 7, "name": "Rock",
     "bands": [4, 3, -2, -3, -1, 1, 3, 5, 5, 4]},
    {"index": 8, "name": "Pop",
     "bands": [-1, 0, 1, 3, 4, 2, 0, -1, -1, -2]},
    {"index": 9, "name": "Electronic",
     "bands": [4, 3, 1, 0, -1, 1, 0, 2, 4, 5]},
    {"index": 10, "name": "Jazz",
     "bands": [3, 2, 1, 2, -1, -1, 0, 1, 2, 3]},
    {"index": 11, "name": "Acoustic",
     "bands": [4, 3, 2, 1, 2, 2, 3, 3, 2, 1]},
    {"index": 12, "name": "Headphones",
     "bands": [3, 4, 2, -1, -2, -1, 1, 3, 4, 5]},
]


def preset_bands(preset_index: int) -> list[float]:
    """Return the band amplitudes for the given preset index, or a
    flat curve if the index is unknown."""
    for p in PRESETS:
        if p["index"] == preset_index:
            return list(p["bands"])
    return [0.0] * len(BAND_FREQUENCIES_HZ)
