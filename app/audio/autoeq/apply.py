"""Build SOS coefficients from an `AutoEqProfile`.

Phase 2 stays narrow: profile bands → SOS matrix at a given
sample rate. The user-tilt layer (bass / treble shelves the user
adds on top) is Phase 5, not here.

Returned matrix has shape `(N, 6)` where N is the number of
filter bands in the profile, suitable for direct hand-off to
`scipy.signal.sosfilt` or to `app.audio.eq.Equalizer.set_sos`.
"""
from __future__ import annotations

import numpy as np

from app.audio.eq import _compute_biquad

from .profiles import AutoEqProfile


def profile_to_sos(profile: AutoEqProfile, sample_rate: int) -> np.ndarray:
    """Compile a profile's filter list into a `(len(bands), 6)`
    SOS matrix at the target sample rate.

    Returns an empty `(0, 6)` array for profiles with no bands —
    let callers decide whether to treat that as "bypass" or as
    an error.
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
