"""Phase 1 of the AutoEQ work — see docs/autoeq-headphone-profiles-scope.md.

These tests pin the new low-shelf (LSC) and high-shelf (HSC) biquad
implementations against their RBJ Audio EQ Cookbook reference shape:

  - At frequencies WELL BELOW a low-shelf's corner, the response
    should approach the shelf's gain.
  - At frequencies WELL ABOVE a high-shelf's corner, same.
  - Across the corner, the shape is monotone — no resonant peaks.
  - At DC for low-shelf and at Nyquist for high-shelf, the response
    is settled (within ~0.5 dB of asymptotic gain).

We don't try to match a specific AutoEQ profile's published preview
in this phase — that's a more rigorous check for Phase 2 once the
profile parser exists. The shelf-shape test is sufficient to catch
the broad classes of regression (sign error in coefficients, wrong
slope-Q convention, type/dispatch bugs).
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.signal import sosfreqz  # type: ignore

from app.audio.eq import (
    HIGH_SHELF,
    LOW_SHELF,
    PEAKING,
    _compute_biquad,
    _high_shelf_biquad,
    _low_shelf_biquad,
    _peaking_biquad,
)


SAMPLE_RATE = 48_000


def _magnitude_db_at(sos_row: np.ndarray, freq_hz: float) -> float:
    """Compute the SOS row's magnitude response in dB at one
    frequency. `sosfreqz` wants a (1, 6) matrix, hence the reshape."""
    sos = sos_row.reshape(1, 6)
    # `worN` accepts an array of physical frequencies when fs is set.
    _, h = sosfreqz(sos, worN=np.array([freq_hz]), fs=SAMPLE_RATE)
    return 20.0 * math.log10(max(abs(h[0]), 1e-12))


# ---------------------------------------------------------------------------
# Low-shelf: boost / cut applied at frequencies BELOW the corner
# ---------------------------------------------------------------------------


def test_low_shelf_boost_settles_to_gain_at_dc():
    """A +6 dB low shelf at 200 Hz with slope-Q = 0.7 should produce
    a magnitude very close to +6 dB well below the corner."""
    biquad = _low_shelf_biquad(200.0, 6.0, 0.7, SAMPLE_RATE)
    # 20 Hz is two and a half decades below the corner — fully settled.
    db = _magnitude_db_at(biquad, 20.0)
    assert abs(db - 6.0) < 0.5, f"expected ~+6 dB at 20 Hz, got {db:.2f}"


def test_low_shelf_settles_to_unity_well_above_corner():
    """Same filter should approach 0 dB at frequencies well above
    the corner (the shelf's pass-through region)."""
    biquad = _low_shelf_biquad(200.0, 6.0, 0.7, SAMPLE_RATE)
    db = _magnitude_db_at(biquad, 8000.0)  # 1.6 decades above corner
    assert abs(db) < 0.5, f"expected ~0 dB at 8 kHz, got {db:.2f}"


def test_low_shelf_cut_settles_to_negative_gain_at_dc():
    """Sanity-check the negative-gain branch: a -4 dB shelf should
    settle near -4 dB below the corner. Catches sign errors that a
    boost-only test would miss."""
    biquad = _low_shelf_biquad(150.0, -4.0, 0.7, SAMPLE_RATE)
    db = _magnitude_db_at(biquad, 20.0)
    assert abs(db - (-4.0)) < 0.5, f"expected ~-4 dB at 20 Hz, got {db:.2f}"


def test_low_shelf_no_resonant_peak_above_corner():
    """A shelf with reasonable Q should be monotonically returning
    to 0 dB above the corner — no overshoot. Tests the shelf form
    actually behaves like a shelf, not a poorly-tuned bandpass."""
    biquad = _low_shelf_biquad(300.0, 6.0, 0.7, SAMPLE_RATE)
    # Sample a few frequencies above the corner and check they're
    # all ≤ the asymptotic gain (allowing small numerical slack).
    for f in (500.0, 1000.0, 2000.0, 4000.0):
        db = _magnitude_db_at(biquad, f)
        assert db <= 6.0 + 0.1, (
            f"low shelf overshot at {f} Hz: {db:.2f} dB > 6 dB"
        )


# ---------------------------------------------------------------------------
# High-shelf: mirror image — boost/cut applied above the corner
# ---------------------------------------------------------------------------


def test_high_shelf_boost_settles_to_gain_at_nyquist():
    """A +6 dB high shelf at 8 kHz should settle at +6 dB well
    above the corner. Test at 20 kHz, near Nyquist for 48 kHz."""
    biquad = _high_shelf_biquad(8000.0, 6.0, 0.7, SAMPLE_RATE)
    db = _magnitude_db_at(biquad, 20000.0)
    assert abs(db - 6.0) < 0.5, f"expected ~+6 dB at 20 kHz, got {db:.2f}"


def test_high_shelf_settles_to_unity_well_below_corner():
    """Same filter approaches 0 dB at frequencies well below the
    corner (the shelf's pass-through region)."""
    biquad = _high_shelf_biquad(8000.0, 6.0, 0.7, SAMPLE_RATE)
    db = _magnitude_db_at(biquad, 100.0)
    assert abs(db) < 0.5, f"expected ~0 dB at 100 Hz, got {db:.2f}"


def test_high_shelf_cut_settles_to_negative_gain():
    """Sanity-check the cut branch — symmetric to the low-shelf cut
    test but for the high-shelf path."""
    biquad = _high_shelf_biquad(10000.0, -3.0, 0.7, SAMPLE_RATE)
    db = _magnitude_db_at(biquad, 20000.0)
    assert abs(db - (-3.0)) < 0.5, f"expected ~-3 dB at 20 kHz, got {db:.2f}"


def test_high_shelf_no_resonant_dip_below_corner():
    """A high shelf shouldn't dip below 0 dB on its way down (no
    resonant cut). Mirrors the low-shelf overshoot check."""
    biquad = _high_shelf_biquad(6000.0, 6.0, 0.7, SAMPLE_RATE)
    for f in (4000.0, 2000.0, 1000.0, 500.0, 100.0):
        db = _magnitude_db_at(biquad, f)
        # Asymptotic low-frequency gain should be ~0 dB; no dips.
        assert db >= -0.1, (
            f"high shelf dipped at {f} Hz: {db:.2f} dB < 0 dB"
        )


# ---------------------------------------------------------------------------
# Cross-cutting — dispatch + cascading + regression on peaking
# ---------------------------------------------------------------------------


def test_compute_biquad_dispatches_correctly():
    """The new `_compute_biquad` dispatcher must produce the same
    coefficients as calling each biquad helper directly. Pinning
    this so a refactor that re-routes the dispatch can't silently
    swap filter types."""
    np.testing.assert_array_equal(
        _compute_biquad(PEAKING, 1000.0, 3.0, 1.0, SAMPLE_RATE),
        _peaking_biquad(1000.0, 3.0, 1.0, SAMPLE_RATE),
    )
    np.testing.assert_array_equal(
        _compute_biquad(LOW_SHELF, 200.0, 4.0, 0.7, SAMPLE_RATE),
        _low_shelf_biquad(200.0, 4.0, 0.7, SAMPLE_RATE),
    )
    np.testing.assert_array_equal(
        _compute_biquad(HIGH_SHELF, 8000.0, -2.0, 0.7, SAMPLE_RATE),
        _high_shelf_biquad(8000.0, -2.0, 0.7, SAMPLE_RATE),
    )


def test_compute_biquad_rejects_unknown_type():
    """A typo'd filter type from a malformed AutoEQ file should
    fail loudly rather than silently produce a flat response."""
    with pytest.raises(ValueError, match="unknown biquad filter type"):
        _compute_biquad("XYZ", 1000.0, 3.0, 1.0, SAMPLE_RATE)


def test_zero_gain_is_pass_through_for_all_types():
    """A 0 dB filter of any type should produce a magnitude of ~0 dB
    everywhere — defensive check that the gain conversion doesn't
    introduce a unity offset for the "no change" case."""
    for ft in (PEAKING, LOW_SHELF, HIGH_SHELF):
        biquad = _compute_biquad(ft, 1000.0, 0.0, 0.7, SAMPLE_RATE)
        for f in (50.0, 500.0, 5000.0, 15000.0):
            db = _magnitude_db_at(biquad, f)
            assert abs(db) < 0.05, (
                f"{ft} 0 dB filter not flat at {f} Hz: {db:.4f} dB"
            )


def test_small_autoeq_style_cascade():
    """Sketch of an AutoEQ-style profile cascade: one low shelf for
    sub-bass lift, two peaking bands for mid corrections, one high
    shelf for treble. Verifies the cascade sums correctly — the
    target is a profile-shaped curve, not a known headphone."""
    # Approximate "boost the bass and tame the treble" profile.
    bands = [
        _low_shelf_biquad(100.0, 4.0, 0.7, SAMPLE_RATE),
        _peaking_biquad(1000.0, -2.0, 1.0, SAMPLE_RATE),
        _peaking_biquad(3000.0, -3.0, 2.0, SAMPLE_RATE),
        _high_shelf_biquad(10000.0, -2.0, 0.7, SAMPLE_RATE),
    ]
    sos = np.stack(bands)

    # Spot-check the cascade response at characteristic frequencies.
    # Each band contributes its asymptotic gain at its strong-influence
    # region; bands far from a probe frequency contribute ~0 dB.
    def cascade_db(freq_hz: float) -> float:
        _, h = sosfreqz(sos, worN=np.array([freq_hz]), fs=SAMPLE_RATE)
        return 20.0 * math.log10(max(abs(h[0]), 1e-12))

    # 30 Hz: deep in the low shelf's region, peaking bands and high
    # shelf contribute ~0. Expect ~+4 dB.
    assert abs(cascade_db(30.0) - 4.0) < 0.5

    # 20 kHz: deep in the high shelf's region. Expect ~-2 dB.
    assert abs(cascade_db(20000.0) - (-2.0)) < 0.5

    # 1 kHz: at one peaking band's center. Other bands' contributions
    # are smaller but non-zero, so we just check the sign and rough
    # magnitude rather than pin a specific value.
    assert -3.5 < cascade_db(1000.0) < -1.0
