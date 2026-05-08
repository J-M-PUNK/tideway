"""Tests for ReplayGain loudness leveling.

Covers both the pure gain calculation (`compute_gain_db`) and the
stateful audio-path stage (`ReplayGain.apply`). The audio engine
folds the application into its existing int↔float round-trip so
this module's surface is small — the gain itself is just a scalar
multiply, the interesting code is the resolver in front of it.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from app.audio.replaygain import (
    ReplayGain,
    ReplayGainTags,
    compute_gain_db,
)


# Reference fixture: a quiet master with a typical track-level gain
# and a low peak. Album-level tags differ slightly so we can tell
# the two modes apart.
TAGS = ReplayGainTags(
    track_gain_db=-3.5,
    track_peak=0.85,
    album_gain_db=-4.2,
    album_peak=0.92,
)


# --- compute_gain_db -------------------------------------------------


def test_off_mode_returns_zero():
    assert compute_gain_db(TAGS, "off", preamp_db=0, prevent_clipping=True) == 0.0
    # Even with preamp set, off means off.
    assert compute_gain_db(TAGS, "off", preamp_db=5, prevent_clipping=True) == 0.0


def test_track_mode_reads_track_tag():
    g = compute_gain_db(TAGS, "track", preamp_db=0, prevent_clipping=False)
    assert g == pytest.approx(-3.5)


def test_album_mode_reads_album_tag():
    g = compute_gain_db(TAGS, "album", preamp_db=0, prevent_clipping=False)
    assert g == pytest.approx(-4.2)


def test_missing_tag_falls_back_to_zero():
    """If Tidal doesn't ship a tag for the chosen mode, we apply no
    gain rather than guessing. This is per-stream behaviour: the
    user's mode selection stays active and the next track will pick
    up its tag if it has one."""
    sparse = ReplayGainTags(track_gain_db=None, track_peak=None)
    assert compute_gain_db(sparse, "track", 0, True) == 0.0
    assert compute_gain_db(sparse, "album", 0, True) == 0.0


def test_preamp_adds_on_top():
    g = compute_gain_db(TAGS, "track", preamp_db=2.0, prevent_clipping=False)
    assert g == pytest.approx(-3.5 + 2.0)


def test_clipping_prevention_clamps_positive_gain():
    """A track with peak=0.85 and rg=-3.5 dB; user adds preamp=+10 dB.
    Total proposed gain = +6.5 dB. Max safe gain for peak 0.85 is
    20*log10(1/0.85) ≈ +1.41 dB. Clipping prevention should clamp
    to that ceiling."""
    g = compute_gain_db(TAGS, "track", preamp_db=10, prevent_clipping=True)
    expected_ceiling = -20.0 * math.log10(0.85)
    assert g == pytest.approx(expected_ceiling, abs=1e-6)


def test_clipping_prevention_off_lets_user_push_past():
    g = compute_gain_db(TAGS, "track", preamp_db=10, prevent_clipping=False)
    assert g == pytest.approx(-3.5 + 10.0)


def test_clipping_prevention_does_not_raise_quiet_tracks():
    """A negative total gain stays negative — clipping prevention is
    only a ceiling, not a floor. Pulling a track down 5 dB shouldn't
    get nudged back up because the ceiling math went the other way."""
    g = compute_gain_db(TAGS, "track", preamp_db=-3, prevent_clipping=True)
    assert g == pytest.approx(-3.5 - 3.0)


def test_clipping_prevention_handles_peak_above_unity():
    """Some loud masters report peak > 1.0 (intersample peaks, etc.).
    The ceiling formula -20*log10(peak) goes negative there, meaning
    we always attenuate. The clamp must respect that even when the
    proposed total gain is also negative but less so."""
    loud = ReplayGainTags(
        track_gain_db=2.0, track_peak=1.5, album_gain_db=None, album_peak=None
    )
    g = compute_gain_db(loud, "track", preamp_db=0, prevent_clipping=True)
    expected_ceiling = -20.0 * math.log10(1.5)
    assert g == pytest.approx(expected_ceiling)


# --- ReplayGain (the stage) ----------------------------------------


def test_unity_gain_is_pass_through():
    rg = ReplayGain()
    samples = np.array([[0.5, 0.25], [-0.3, 0.1]], dtype=np.float32)
    original = samples.copy()
    rg.apply(samples)
    np.testing.assert_array_equal(samples, original)


def test_clear_resets_to_unity():
    rg = ReplayGain()
    rg.set_gain_db(-6.0)
    assert rg.is_active()
    rg.clear()
    assert not rg.is_active()
    samples = np.full((4, 2), 0.5, dtype=np.float32)
    rg.apply(samples)
    np.testing.assert_array_equal(samples, np.full((4, 2), 0.5, dtype=np.float32))


def test_set_gain_zero_db_clears():
    """0 dB == unity. Persisting the linear value 1.0 instead of just
    not-installing-a-stage means `apply` early-exits cleanly."""
    rg = ReplayGain()
    rg.set_gain_db(-6.0)
    rg.set_gain_db(0.0)
    assert not rg.is_active()


def test_minus_6db_halves_amplitude():
    rg = ReplayGain()
    rg.set_gain_db(-6.0)
    samples = np.full((4, 2), 1.0, dtype=np.float32)
    rg.apply(samples)
    # -6 dB ≈ 0.5012, not exactly 0.5. Allow a small tolerance.
    np.testing.assert_allclose(samples, 0.5012, atol=1e-3)


def test_plus_6db_roughly_doubles_amplitude():
    rg = ReplayGain()
    rg.set_gain_db(6.0)
    samples = np.full((4, 2), 0.4, dtype=np.float32)
    rg.apply(samples)
    np.testing.assert_allclose(samples, 0.4 * 1.995, atol=1e-3)


def test_gain_linear_matches_db():
    rg = ReplayGain()
    for db in (-12.0, -3.0, 0.0, 3.0, 12.0):
        rg.set_gain_db(db)
        if db == 0.0:
            assert rg.gain_linear() == 1.0
        else:
            assert rg.gain_linear() == pytest.approx(10.0 ** (db / 20.0))
