"""Parametric EQ for the PCM engine.

The engine is a cascade of RBJ Audio EQ Cookbook biquads (peaking +
low/high shelf), packed as second-order sections and filtered via
`scipy.signal.sosfilt` (C-speed; a pure-Python loop over N bands
× 1024 samples × 2 channels won't hit the audio callback's ~20ms
deadline).

Two callers build cascades on top of the same biquad math:

  - The **manual parametric EQ** (`build_parametric_sos`) — a
    user-editable list of `ParametricBand`s, each with its own
    type / frequency / gain / Q.
  - The **AutoEQ headphone profiles** (`app/audio/autoeq/`) — a
    cascade compiled from a measurement-correction file.

Both install via `Equalizer.set_sos`; the manual path is:

    eq = Equalizer(sample_rate)
    sos = build_parametric_sos(bands, sample_rate)
    eq.set_sos(sos, preamp_db=-1.0)   # or eq.clear() to bypass
    eq.apply(outdata)                 # in-place filter
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.signal import sosfilt, sosfilt_zi, sosfreqz  # type: ignore


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
    """Stateful biquad-cascade EQ.

    Thread-model: `set_sos` / `clear` are called from the HTTP
    handler thread; `apply` is called from the sounddevice audio
    callback thread. A short lock serialises coefficient updates
    so the callback never sees half-written SOS state.
    """

    def __init__(self, sample_rate: int, channels: int = 2):
        self._sample_rate = int(sample_rate)
        self._channels = int(channels)
        self._lock = threading.Lock()
        # (N, 6) SOS matrix; None = EQ disabled (bypass).
        self._sos: Optional[np.ndarray] = None
        # Filter state per section per channel, shape (N, 2, channels).
        self._state: Optional[np.ndarray] = None
        # Preamp as a linear multiplier (10 ** (dB/20)). 1.0 = unity.
        self._preamp_linear: float = 1.0

    # --- configuration ----------------------------------------------

    def set_sos(
        self,
        sos: np.ndarray,
        preamp_db: Optional[float] = None,
    ) -> None:
        """Install a pre-built SOS matrix as the active filter. Both
        the manual parametric EQ (`build_parametric_sos`) and the
        AutoEQ headphone-profile path compile their bands to an SOS
        and install it here.

        `sos` must be `(N, 6)` float-friendly. An empty array with no
        (or unity) preamp clears the EQ entirely; an empty array WITH
        a non-unity preamp installs a preamp-only stage — flat manual
        bands compile to zero biquads but the user's preamp must
        still be audible. Caller is responsible for getting the
        sample rate right — coefficients depend on it.
        """
        sos_arr = np.asarray(sos, dtype=np.float32)
        preamp = 1.0 if preamp_db is None else 10.0 ** (float(preamp_db) / 20.0)
        if sos_arr.size == 0:
            if preamp == 1.0:
                self.clear()
            else:
                with self._lock:
                    self._sos = None
                    self._state = None
                    self._preamp_linear = preamp
            return
        if sos_arr.ndim != 2 or sos_arr.shape[1] != 6:
            raise ValueError(
                f"sos must be (N, 6); got shape {sos_arr.shape}"
            )
        self._install_sos(sos_arr, preamp_db)

    def _install_sos(
        self,
        sos: np.ndarray,
        preamp_db: Optional[float],
    ) -> None:
        """Shared coefficient-swap path for `set_bands` and
        `set_sos`. State is reinitialised on every install — the
        AutoEQ profile path swaps cascades only on user action
        (mode change / profile pick), never per-callback, so the
        settle transient is acceptable."""
        # `sosfilt_zi` gives steady-state initial conditions for a
        # DC input, the right starting point for audio that begins
        # from silence.
        zi_single = sosfilt_zi(sos)  # shape (N, 2)
        state = np.tile(
            zi_single[:, :, None], (1, 1, self._channels)
        ).astype(np.float32, copy=False)
        preamp = 1.0 if preamp_db is None else 10.0 ** (float(preamp_db) / 20.0)
        with self._lock:
            self._sos = sos.astype(np.float32, copy=False)
            self._state = state
            self._preamp_linear = preamp

    def clear(self) -> None:
        """Disable filtering. `apply()` becomes a pass-through."""
        with self._lock:
            self._sos = None
            self._state = None
            self._preamp_linear = 1.0

    def is_active(self) -> bool:
        # A preamp-only stage (no biquads) still alters the audio, so
        # the callback must run `apply` for it.
        return self._sos is not None or self._preamp_linear != 1.0

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
        if self._sos is None and self._preamp_linear == 1.0:
            return
        with self._lock:
            if self._preamp_linear != 1.0:
                samples *= self._preamp_linear
            if self._sos is None or self._state is None:
                # Preamp-only stage — no biquads to run.
                return
            # sosfilt operates along one axis; stereo is (N, 2) so
            # we filter each channel by passing axis=0.
            filtered, self._state = sosfilt(
                self._sos, samples, axis=0, zi=self._state
            )
            samples[:] = filtered.astype(samples.dtype, copy=False)


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


# Stability floor for the shelf alpha's sqrt argument — see the
# comment at the use sites. 1e-2 keeps the pole pair comfortably
# inside the unit circle even at the worst gain/slope combo the
# manual EQ's validation bounds allow.
_SHELF_ALPHA_FLOOR = 1e-2


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
    # Floor the sqrt argument at a small positive value rather than
    # zero: extreme slope/gain combos (e.g. +23 dB with S=2) drive it
    # negative, and a floor of exactly 0 makes alpha 0, which puts the
    # biquad's poles ON the unit circle — a marginally-stable filter
    # that rings forever instead of shelving. The positive floor keeps
    # the poles strictly inside; the audible result for those extreme
    # settings is a strongly resonant shelf, not an oscillator.
    # Mirrored in web/src/lib/eqCurve.ts so the drawn curve matches.
    alpha = (
        sin_w0
        / 2.0
        * math.sqrt(max(_SHELF_ALPHA_FLOOR, (A + 1.0 / A) * (1.0 / q - 1.0) + 2.0))
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
    # Stability floor — see the comment in `_low_shelf_biquad`.
    alpha = (
        sin_w0
        / 2.0
        * math.sqrt(max(_SHELF_ALPHA_FLOOR, (A + 1.0 / A) * (1.0 / q - 1.0) + 2.0))
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
# Manual parametric EQ
# ---------------------------------------------------------------------------
# The manual EQ is a user-editable list of parametric bands — each a
# peaking or shelf biquad with its own frequency, gain, and Q. This
# replaces the old fixed-frequency / fixed-Q "graphic" manual EQ; it
# runs through the same `_compute_biquad` cascade the AutoEQ profile
# path already uses, so there's no new DSP — just a builder that maps
# the user's bands to an SOS matrix.

# Editable ranges enforced at the API layer. Frequencies span the
# audible band; gain/Q ranges are generous enough for real tone-
# shaping but rule out values that would produce nonsense filters or
# NaN audio (Q must stay > 0 — the biquad bandwidth math divides by
# it). Mirrors the spirit of the AutoEQ import bounds in
# `app/audio/autoeq/profiles.py`, tightened to the editor's range.
MANUAL_FREQ_MIN_HZ: float = 20.0
MANUAL_FREQ_MAX_HZ: float = 20000.0
MANUAL_GAIN_ABS_MAX_DB: float = 24.0
MANUAL_Q_MIN: float = 0.1
MANUAL_Q_MAX: float = 10.0
# Cap the band count so a malformed payload can't hand the audio
# engine a 10,000-section cascade. Comfortably above any real manual
# curve.
MANUAL_MAX_BANDS: int = 32

_MANUAL_FILTER_TYPES: frozenset[str] = frozenset({PEAKING, LOW_SHELF, HIGH_SHELF})


def manual_eq_config() -> dict:
    """Editable bounds + allowed filter types for the manual
    parametric EQ. The API hands this to the Settings UI so the band
    controls clamp to the same ranges the server validates against —
    one source of truth, no hardcoded limits in the frontend."""
    return {
        "filter_types": [PEAKING, LOW_SHELF, HIGH_SHELF],
        "freq_min": MANUAL_FREQ_MIN_HZ,
        "freq_max": MANUAL_FREQ_MAX_HZ,
        "gain_abs_max": MANUAL_GAIN_ABS_MAX_DB,
        "q_min": MANUAL_Q_MIN,
        "q_max": MANUAL_Q_MAX,
        "max_bands": MANUAL_MAX_BANDS,
    }


@dataclass
class ParametricBand:
    """One band of the manual parametric EQ.

    `filter_type` is one of PK / LSC / HSC (same codes the AutoEQ
    cascade uses). `enabled` lets the UI keep a band's settings while
    momentarily taking it out of the cascade — a disabled band
    compiles to no biquad rather than a flat-gain no-op.
    """

    filter_type: str
    freq_hz: float
    gain_db: float
    q: float
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "type": self.filter_type,
            "freq": self.freq_hz,
            "gain": self.gain_db,
            "q": self.q,
            "enabled": self.enabled,
        }


def parametric_band_from_dict(raw: dict) -> ParametricBand:
    """Validate and coerce a JSON-ish band dict into a `ParametricBand`.

    Raises `ValueError` on any out-of-range / wrong-type field so a
    malformed API payload or settings file fails loudly instead of
    feeding the biquad math a negative Q or a sub-DC frequency. The
    error message names the offending field for easy debugging.
    """
    ftype = str(raw.get("type", "")).upper()
    if ftype not in _MANUAL_FILTER_TYPES:
        raise ValueError(
            f"band type {ftype!r} not one of {sorted(_MANUAL_FILTER_TYPES)}"
        )
    try:
        freq = float(raw["freq"])
        gain = float(raw["gain"])
        q = float(raw["q"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"band has a missing/non-numeric field: {exc}") from exc
    if not (MANUAL_FREQ_MIN_HZ <= freq <= MANUAL_FREQ_MAX_HZ):
        raise ValueError(
            f"band frequency {freq} Hz out of range "
            f"[{MANUAL_FREQ_MIN_HZ}, {MANUAL_FREQ_MAX_HZ}]"
        )
    # `not (abs(gain) <= MAX)` instead of `abs(gain) > MAX`: NaN
    # compares False on both sides, so the latter would silently
    # accept a NaN gain (Python's json parser and Pydantic both let
    # the bare NaN literal through), poisoning persisted settings.
    # The freq/Q range checks above reject NaN the same way.
    if not (abs(gain) <= MANUAL_GAIN_ABS_MAX_DB):
        raise ValueError(
            f"band gain {gain} dB exceeds ±{MANUAL_GAIN_ABS_MAX_DB} dB"
        )
    if not (MANUAL_Q_MIN <= q <= MANUAL_Q_MAX):
        raise ValueError(
            f"band Q {q} out of range [{MANUAL_Q_MIN}, {MANUAL_Q_MAX}]"
        )
    # `enabled` defaults to True so a band dict without the key (e.g. a
    # hand-written settings file) is treated as active, not silently
    # muted.
    enabled = bool(raw.get("enabled", True))
    return ParametricBand(
        filter_type=ftype,
        freq_hz=freq,
        gain_db=gain,
        q=q,
        enabled=enabled,
    )


def parse_parametric_bands(raw_bands: list) -> list[ParametricBand]:
    """Validate a whole band list, enforcing the count cap. Returns
    the coerced bands (including disabled ones — the builder drops
    those, but callers persist the full list so the user's disabled
    bands survive a round-trip)."""
    if len(raw_bands) > MANUAL_MAX_BANDS:
        raise ValueError(
            f"{len(raw_bands)} bands exceeds the {MANUAL_MAX_BANDS}-band cap"
        )
    return [parametric_band_from_dict(b) for b in raw_bands]


# A band whose gain is within this of 0 dB is a unity (pass-through)
# biquad — skip it entirely so a flat-but-present band layout stays
# bit-perfect. This is what lets the editor seed grabbable default
# bands without flipping audio off bit-perfect until the user shapes
# one.
_UNITY_GAIN_EPS_DB = 1e-6


def build_parametric_sos(
    bands: list[ParametricBand], sample_rate: int
) -> np.ndarray:
    """Compile the audibly-active bands into an `(N, 6)` SOS matrix at
    `sample_rate`. Disabled bands, flat (0 dB) bands, and an empty
    list all compile to an empty `(0, 6)` array — the manual-EQ
    analogue of `profile_to_sos`, sharing the same `_compute_biquad`
    dispatcher. Pass the result to `Equalizer.set_sos` (empty →
    clear / bypass)."""
    rows = [
        _compute_biquad(
            band.filter_type,
            band.freq_hz,
            band.gain_db,
            band.q,
            sample_rate,
        )
        for band in bands
        if band.enabled and abs(band.gain_db) > _UNITY_GAIN_EPS_DB
    ]
    if not rows:
        return np.empty((0, 6), dtype=np.float32)
    return np.stack(rows).astype(np.float32, copy=False)


def manual_eq_alters_audio(
    bands: list, preamp_db: Optional[float] = None
) -> bool:
    """Whether a manual EQ configuration (band dicts or
    ParametricBand objects, plus the master preamp) actually changes
    the audio. Flat bands compile to unity and are skipped by
    `build_parametric_sos`, so an all-flat curve with no preamp is
    sonically bit-perfect; a non-zero preamp alters the audio even
    with zero audible bands (the engine installs a preamp-only
    stage). The signal-path 'EQ active' indicator uses this so a
    freshly-seeded flat default layout doesn't flip the bit-perfect
    badge off."""
    if preamp_db is not None and abs(preamp_db) > _UNITY_GAIN_EPS_DB:
        return True
    for b in bands:
        if isinstance(b, ParametricBand):
            enabled, gain = b.enabled, b.gain_db
        else:
            enabled = bool(b.get("enabled", True))
            gain = float(b.get("gain", 0.0))
        if enabled and abs(gain) > _UNITY_GAIN_EPS_DB:
            return True
    return False


# Default manual EQ layout — six flat bands the editor seeds a fresh
# curve with so the user has grabbable nodes instead of an empty
# graph. Low/high shelves bracket the ends; four peaking bands span
# the mids. All gains start at 0 dB, so seeding this layout is
# bit-perfect (every band is unity) until the user drags one.
_DEFAULT_BAND_SPECS: tuple[tuple[str, float, float], ...] = (
    (LOW_SHELF, 105.0, 0.7),
    (PEAKING, 250.0, 1.0),
    (PEAKING, 800.0, 1.0),
    (PEAKING, 2500.0, 1.0),
    (PEAKING, 6000.0, 1.0),
    (HIGH_SHELF, 10000.0, 0.7),
)


def default_parametric_bands() -> list[ParametricBand]:
    """Fresh copies of the default six-band manual layout."""
    return [
        ParametricBand(filter_type=t, freq_hz=f, gain_db=0.0, q=q)
        for (t, f, q) in _DEFAULT_BAND_SPECS
    ]


# ---------------------------------------------------------------------------
# Frequency-response curves
# ---------------------------------------------------------------------------
# Used by the AutoEQ profile graph (`app/audio/autoeq/response.py`
# imports these). The manual parametric editor computes its own curve
# client-side (web/src/lib/eqCurve.ts) so the graph tracks a node live
# as it's dragged, with no server round-trip.


def log_frequency_grid(
    points: int = 512,
    f_min: float = 20.0,
    f_max: float = 20_000.0,
) -> np.ndarray:
    """Geometric series from `f_min` to `f_max` — the natural spacing
    for an audio frequency-response chart's log-x axis."""
    return np.logspace(np.log10(f_min), np.log10(f_max), int(points))


def cascade_magnitude_db(
    sos: np.ndarray,
    preamp_db: float,
    grid: np.ndarray,
    sample_rate: int,
) -> np.ndarray:
    """Magnitude (dB) of an SOS cascade evaluated at `grid` (Hz), with
    a scalar `preamp_db` added on top (the preamp is a single linear
    gain before the biquads; magnitudes multiply → dB add). An empty
    `sos` is a flat pass-through, so the result is just `preamp_db`
    everywhere."""
    if sos is None or np.asarray(sos).size == 0:
        return np.full(len(grid), float(preamp_db), dtype=np.float64)
    _, h = sosfreqz(sos, worN=grid, fs=sample_rate)
    # Clamp to avoid log(0) for a band sitting right at Nyquist.
    magnitude = np.maximum(np.abs(h), 1e-12)
    return 20.0 * np.log10(magnitude) + float(preamp_db)


def graphic_gains_to_parametric(gains: list[float]) -> list[ParametricBand]:
    """Convert a legacy graphic-EQ gain list (one value per
    `BAND_FREQUENCIES_HZ` entry) into parametric peaking bands at those
    ISO frequencies with the old constant `BAND_Q`. Flat (zero-gain)
    bands are dropped — they're no-ops, and leaving them in would
    clutter the editor. Used for both settings migration and preset
    conversion so the two stay consistent.

    Gains clamp into the parametric editor's ±MANUAL_GAIN_ABS_MAX_DB
    range: the legacy POST accepted arbitrary floats, and an
    out-of-range migrated band would fail `parametric_band_from_dict`
    on every subsequent apply — permanently wedging the user's EQ.
    """
    out: list[ParametricBand] = []
    for freq, gain in zip(BAND_FREQUENCIES_HZ, gains):
        if not (abs(gain) > _UNITY_GAIN_EPS_DB):
            # `not >` instead of `<`: also drops NaN from a corrupt
            # legacy settings file instead of migrating it forward.
            continue
        clamped = max(
            -MANUAL_GAIN_ABS_MAX_DB, min(MANUAL_GAIN_ABS_MAX_DB, float(gain))
        )
        out.append(
            ParametricBand(
                filter_type=PEAKING,
                freq_hz=float(freq),
                gain_db=clamped,
                q=BAND_Q,
            )
        )
    return out


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


def parametric_preset(preset_index: int) -> list[ParametricBand]:
    """Resolve a preset to parametric bands. The curated presets are
    defined as graphic-EQ gains at the ISO frequencies (one place to
    edit); this converts the requested one to peaking bands. Unknown
    index → empty list (flat)."""
    return graphic_gains_to_parametric(preset_bands(preset_index))


def parametric_presets() -> list[dict]:
    """Serializable preset list for the API: each entry carries its
    name plus the resolved parametric bands so the Settings UI can
    render an exact mini-curve preview and apply the preset without a
    second round-trip."""
    return [
        {
            "index": p["index"],
            "name": p["name"],
            "bands": [
                b.to_dict() for b in graphic_gains_to_parametric(p["bands"])
            ],
        }
        for p in PRESETS
    ]
