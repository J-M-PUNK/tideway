"""Build SOS coefficients from an `AutoEqProfile`, optionally
augmented with the Phase 5 user-tilt shelves.

The cascade structure:

    Master preamp = profile.preamp_db + tilt.preamp_offset_db
    SOS rows:
      [profile band 1]
      [profile band 2]
      ...
      [profile band N]
      [low-shelf @ 80 Hz with tilt.bass_db]    (only when nonzero)
      [high-shelf @ 8 kHz with tilt.treble_db] (only when nonzero)

Tilt shelves are positioned AFTER the profile bands so the
profile correction lands on a clean signal first; the tilt is a
taste-layer adjustment on top. Phase 6's FR graph will visualise
this composition cleanly.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.audio.eq import (
    _compute_biquad,
    _high_shelf_biquad,
    _low_shelf_biquad,
)

from .profiles import AutoEqProfile


# Phase 5 tilt-shelf parameters. Pinned constants, not per-user
# adjustable — the user picks the gain, not where the shelf
# corner lives. Values match common "warmth / brightness" tilt
# controls in audiophile EQs (PEACE, foobar2000's PEQ).
TILT_BASS_FREQ_HZ = 80.0
TILT_BASS_Q = 0.7
TILT_TREBLE_FREQ_HZ = 8000.0
TILT_TREBLE_Q = 0.7

# A nudge below this in dB is treated as zero — avoids appending
# a no-op biquad to the cascade for sub-noise-floor slider
# positions.
_TILT_EPS_DB = 0.05


@dataclass
class TiltConfig:
    """User taste-layer adjustments stacked on top of the profile.

    All three values default to 0.0 = "no tilt" — equivalent to
    not having Phase 5 at all. Backwards-compatible with the
    Phase 2-4 code paths via the default.

    Sliders in the UI use a -12..+12 dB range; the dataclass
    doesn't clamp because that's a presentation concern.
    """

    preamp_offset_db: float = 0.0
    bass_db: float = 0.0
    treble_db: float = 0.0

    def is_flat(self) -> bool:
        return (
            abs(self.preamp_offset_db) < _TILT_EPS_DB
            and abs(self.bass_db) < _TILT_EPS_DB
            and abs(self.treble_db) < _TILT_EPS_DB
        )


def profile_to_sos(profile: AutoEqProfile, sample_rate: int) -> np.ndarray:
    """Compile a profile's filter list into a `(len(bands), 6)`
    SOS matrix at the target sample rate.

    Returns an empty `(0, 6)` array for profiles with no bands —
    let callers decide whether to treat that as "bypass" or as
    an error.

    This is the Phase 2 entry point. Phase 5 callers that need
    tilt shelves use `cascade_with_tilt` instead; this function
    stays unchanged so older callers + tests still see the same
    contract.
    """
    if not profile.bands:
        return np.empty((0, 6), dtype=np.float32)
    rows = [
        _compute_biquad(
            band.filter_type,
            band.freq_hz,
            band.gain_db,
            band.q,
            sample_rate,
        )
        for band in profile.bands
    ]
    return np.stack(rows).astype(np.float32, copy=False)


def cascade_with_tilt(
    profile: AutoEqProfile,
    sample_rate: int,
    tilt: TiltConfig,
) -> tuple[np.ndarray, float]:
    """Build the full Phase 5 cascade — profile bands + tilt
    shelves — and return `(sos, total_preamp_db)`.

    `total_preamp_db` is `profile.preamp_db + tilt.preamp_offset_db`.
    Caller installs both via `Equalizer.set_sos(sos, preamp_db=...)`.

    A flat tilt produces the same SOS as `profile_to_sos` (no
    extra biquads appended), so the audio path is identical to
    Phase 2-4 behavior when the user hasn't moved any tilt
    sliders.
    """
    rows: list[np.ndarray] = [
        _compute_biquad(
            band.filter_type,
            band.freq_hz,
            band.gain_db,
            band.q,
            sample_rate,
        )
        for band in profile.bands
    ]
    if abs(tilt.bass_db) >= _TILT_EPS_DB:
        rows.append(
            _low_shelf_biquad(
                TILT_BASS_FREQ_HZ, tilt.bass_db, TILT_BASS_Q, sample_rate
            )
        )
    if abs(tilt.treble_db) >= _TILT_EPS_DB:
        rows.append(
            _high_shelf_biquad(
                TILT_TREBLE_FREQ_HZ,
                tilt.treble_db,
                TILT_TREBLE_Q,
                sample_rate,
            )
        )
    if not rows:
        return np.empty((0, 6), dtype=np.float32), 0.0
    sos = np.stack(rows).astype(np.float32, copy=False)
    total_preamp = profile.preamp_db + tilt.preamp_offset_db
    return sos, total_preamp
