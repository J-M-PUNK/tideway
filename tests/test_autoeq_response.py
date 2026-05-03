"""Phase 6 — frequency response computation for the FR graph.

These tests pin the response shape (lengths, log-grid spacing,
None-for-missing-CSV behavior) and a few sanity checks on the
post-EQ values. The exact dB values across the curve aren't
worth pinning — the existing tests in test_autoeq_loader.py /
test_eq_shelf_filters.py cover the underlying math; here we
just verify response.py's plumbing.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.audio.autoeq.apply import TiltConfig
from app.audio.autoeq.profiles import parse_profile_text
from app.audio.autoeq.response import (
    FrequencyResponse,
    compute_response,
    log_frequency_grid,
)


SAMPLE_RATE = 48_000


def test_log_grid_endpoints_and_log_spacing():
    """The grid covers the audible band 20 Hz - 20 kHz with
    geometric spacing — equal log-spacing means equal pixel
    spacing on the chart's log-x axis."""
    grid = log_frequency_grid(points=256)
    assert len(grid) == 256
    assert grid[0] == pytest.approx(20.0)
    assert grid[-1] == pytest.approx(20_000.0)
    # Successive ratios should be constant for log-spacing.
    ratios = grid[1:] / grid[:-1]
    assert np.allclose(ratios, ratios[0], atol=1e-6)


def test_response_with_no_profile_is_flat_zeros():
    """Profile mode with nothing picked yet → all-zero post-EQ
    array, no raw / target. UI renders the zero line as a flat
    "no correction" line, which is the right visual."""
    out = compute_response(
        profile=None,
        tilt=TiltConfig(),
        sample_rate=SAMPLE_RATE,
        data_root=Path("/nonexistent"),
        points=128,
    )
    assert isinstance(out, FrequencyResponse)
    assert len(out.frequencies_hz) == 128
    assert out.raw_db is None
    assert out.target_db is None
    assert all(v == 0.0 for v in out.post_eq_db)


def test_response_with_profile_no_csv_returns_cascade_only(tmp_path):
    """If the headphone's CSV isn't bundled, response returns the
    cascade-only post-EQ line (raw/target null). Catches the
    "missing measurement" graceful-degrade path."""
    text = """
    Preamp: -2 dB
    Filter 1: ON LSC Fc 100 Hz Gain 4 dB Q 0.7
    """
    profile = parse_profile_text(
        text,
        profile_id="oratory1990/Test Headphone",
        brand="Test",
        model="Headphone",
        source="oratory1990",
    )
    out = compute_response(
        profile=profile,
        tilt=TiltConfig(),
        sample_rate=SAMPLE_RATE,
        data_root=tmp_path,  # empty
        points=128,
    )
    assert out.raw_db is None
    assert out.target_db is None
    assert len(out.post_eq_db) == 128
    # Below the 100 Hz shelf, post-EQ should approach the shelf
    # gain (4 dB) plus the master preamp (-2 dB) = ~+2 dB.
    low_band = [
        v
        for f, v in zip(out.frequencies_hz, out.post_eq_db)
        if 20 <= f <= 30
    ]
    assert low_band, "log grid should include the 20-30 Hz band"
    assert abs(np.mean(low_band) - 2.0) < 0.5


def test_response_with_csv_bundled_returns_three_curves(tmp_path):
    """Vendor a tiny synthetic CSV next to a profile and verify
    response.py finds it, parses raw + target, and returns the
    full three-curve payload. Doesn't pin exact values — the
    interp + cascade math is covered by the underlying tests."""
    # Create the directory layout response.py expects:
    # <data_root>/<source>/<brand model>/<brand model>.csv
    headphone_dir = tmp_path / "oratory1990" / "Test Headphone"
    headphone_dir.mkdir(parents=True)
    csv_lines = ["frequency,raw,target"]
    # Two-decade synthetic CSV with raw = -3 dB everywhere,
    # target = 0 dB everywhere. Just enough for compute_response
    # to interpolate over.
    for freq in (20.0, 100.0, 1000.0, 10_000.0, 20_000.0):
        csv_lines.append(f"{freq},-3,0")
    (headphone_dir / "Test Headphone.csv").write_text(
        "\n".join(csv_lines), encoding="utf-8"
    )

    profile = parse_profile_text(
        "Preamp: 0 dB\nFilter 1: ON PK Fc 1000 Hz Gain 0 dB Q 1",
        profile_id="oratory1990/Test Headphone",
        brand="Test",
        model="Headphone",
        source="oratory1990",
    )
    out = compute_response(
        profile=profile,
        tilt=TiltConfig(),
        sample_rate=SAMPLE_RATE,
        data_root=tmp_path,
        points=64,
    )
    assert out.raw_db is not None
    assert out.target_db is not None
    assert len(out.raw_db) == 64
    assert len(out.target_db) == 64
    # Synthetic raw is -3 everywhere → interpolated raw should be
    # ~-3 across the grid.
    assert all(abs(v - (-3.0)) < 0.5 for v in out.raw_db)
    # Cascade is a 0 dB peaking band → effectively no boost. So
    # post-EQ should land near raw (-3 dB) everywhere.
    assert all(abs(v - (-3.0)) < 1.0 for v in out.post_eq_db)


def test_tilt_changes_post_eq_curve(tmp_path):
    """Adding a +6 dB bass tilt should raise the low-frequency
    post-EQ values relative to a flat tilt. Pins the integration
    between TiltConfig and compute_response — tilt isn't just
    metadata, it has to flow through the cascade."""
    text = "Preamp: 0 dB\nFilter 1: ON PK Fc 1000 Hz Gain 0 dB Q 1"
    profile = parse_profile_text(text)
    flat = compute_response(
        profile=profile,
        tilt=TiltConfig(),
        sample_rate=SAMPLE_RATE,
        data_root=tmp_path,
        points=128,
    )
    bassy = compute_response(
        profile=profile,
        tilt=TiltConfig(bass_db=6.0),
        sample_rate=SAMPLE_RATE,
        data_root=tmp_path,
        points=128,
    )
    # 30 Hz bin: bass tilt should land it ~6 dB above flat.
    flat_low = next(
        v for f, v in zip(flat.frequencies_hz, flat.post_eq_db) if f > 25
    )
    bassy_low = next(
        v for f, v in zip(bassy.frequencies_hz, bassy.post_eq_db) if f > 25
    )
    assert (bassy_low - flat_low) > 4.0
    # 5 kHz bin: tilt's bass shelf shouldn't reach this far up.
    flat_mid = next(
        v
        for f, v in zip(flat.frequencies_hz, flat.post_eq_db)
        if f > 4500 and f < 5500
    )
    bassy_mid = next(
        v
        for f, v in zip(bassy.frequencies_hz, bassy.post_eq_db)
        if f > 4500 and f < 5500
    )
    assert abs(bassy_mid - flat_mid) < 0.5
