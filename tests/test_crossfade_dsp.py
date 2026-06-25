"""Tests for the equal-power crossfade DSP primitives.

The crossfade output can only be judged by ear on real hardware, so the
math is pinned hard here: the gains must form an equal-power pair (so a
steady-loudness source stays steady across the seam), saturate at the
ends, and the block mixer must ramp per-sample and preserve shape/dtype.
"""
from __future__ import annotations

import numpy as np

from app.audio.crossfade import equal_power_gains, mix_crossfade_block


def test_gains_saturate_at_the_ends():
    out_g, in_g = equal_power_gains(np.array([0, 100]), 100)
    # Start of fade: fully outgoing, no incoming.
    assert out_g[0] == 1.0
    assert in_g[0] == 0.0
    # End of fade: fully incoming, outgoing silent.
    assert in_g[1] == 1.0
    assert abs(out_g[1]) < 1e-6


def test_midpoint_is_equal_power():
    out_g, in_g = equal_power_gains(np.array([50]), 100)
    # cos(pi/4) == sin(pi/4) ~= 0.7071
    assert abs(float(out_g[0]) - 0.70710677) < 1e-5
    assert abs(float(in_g[0]) - 0.70710677) < 1e-5


def test_equal_power_invariant_holds_across_the_fade():
    positions = np.arange(0, 1001)
    out_g, in_g = equal_power_gains(positions, 1000)
    power = out_g.astype(np.float64) ** 2 + in_g.astype(np.float64) ** 2
    # out**2 + in**2 == 1 everywhere — the whole point of equal-power.
    assert np.allclose(power, 1.0, atol=1e-6)


def test_positions_are_clamped_outside_the_window():
    # Before the fade -> fully outgoing; past the end -> fully incoming.
    out_g, in_g = equal_power_gains(np.array([-50, 100, 250]), 100)
    assert out_g[0] == 1.0 and in_g[0] == 0.0
    assert abs(out_g[2]) < 1e-6 and in_g[2] == 1.0


def test_zero_total_is_an_instant_switch():
    out_g, in_g = equal_power_gains(np.array([0, 1, 2]), 0)
    assert np.all(out_g == 0.0)
    assert np.all(in_g == 1.0)


def test_mix_block_endpoints_match_pure_sources():
    frames = 64
    out_block = np.full((frames, 2), 0.8, dtype=np.float32)
    in_block = np.full((frames, 2), -0.5, dtype=np.float32)
    # A fade exactly one block long: first frame is all-out, last all-in.
    mixed = mix_crossfade_block(out_block, in_block, start_pos=0, total=frames)
    assert mixed.dtype == np.float32
    assert mixed.shape == (frames, 2)
    assert np.allclose(mixed[0], out_block[0], atol=1e-6)  # pos 0 -> outgoing
    assert np.allclose(mixed[-1], in_block[-1], atol=2e-2)  # near end -> incoming


def test_mix_block_ramps_monotonically_for_constant_sources():
    # Outgoing constant +1, incoming constant 0: the mix is just the
    # outgoing gain, which must fall monotonically across the fade.
    frames = 128
    out_block = np.ones((frames, 1), dtype=np.float32)
    in_block = np.zeros((frames, 1), dtype=np.float32)
    mixed = mix_crossfade_block(out_block, in_block, start_pos=0, total=frames)[:, 0]
    assert np.all(np.diff(mixed) <= 1e-6)  # non-increasing
    assert mixed[0] > mixed[-1]


def test_mix_block_respects_start_pos():
    # A block taken from the back half of the fade should be quieter on
    # the outgoing side than one from the front.
    frames = 32
    out_block = np.ones((frames, 1), dtype=np.float32)
    in_block = np.zeros((frames, 1), dtype=np.float32)
    front = mix_crossfade_block(out_block, in_block, 0, 1000)[:, 0].mean()
    back = mix_crossfade_block(out_block, in_block, 900, 1000)[:, 0].mean()
    assert back < front
