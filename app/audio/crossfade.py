"""Equal-power crossfade mixing primitives.

Kept in its own module, separate from the realtime player, so the DSP
is unit-testable without standing up the audio engine — the part that
matters most here is that the math is provably correct, because the
output can only be judged by ear on real hardware.

The player's callback calls `mix_crossfade_block` once it's inside the
overlap window, handing it one callback's worth of frames from both the
outgoing (fading down) and incoming (fading up) tracks.

Why equal-power rather than a linear fade: a linear crossfade sums two
correlated signals whose amplitudes add to less than 1 in the middle, so
the perceived loudness dips at the seam. Equal-power keeps
out_gain**2 + in_gain**2 == 1 across the whole overlap (a quarter-cosine
pair), so a steady-loudness source stays at steady loudness through the
transition — the standard choice for music players.
"""
from __future__ import annotations

import numpy as np


def equal_power_gains(
    positions: np.ndarray, total: int
) -> tuple[np.ndarray, np.ndarray]:
    """Equal-power fade gains at the given sample `positions` over a fade
    of `total` samples.

    Returns ``(out_gains, in_gains)`` as float32 arrays the same shape as
    `positions`. Positions are clamped to ``[0, total]`` so the gains
    saturate at the ends (fully outgoing before the fade, fully incoming
    after) rather than wrapping. A non-positive `total` is treated as an
    instantaneous switch: outgoing silent, incoming full.
    """
    if total <= 0:
        ones = np.ones(np.shape(positions), dtype=np.float32)
        return np.zeros_like(ones), ones
    t = np.clip(np.asarray(positions, dtype=np.float64) / float(total), 0.0, 1.0)
    angle = t * (np.pi / 2.0)
    return np.cos(angle).astype(np.float32), np.sin(angle).astype(np.float32)


def mix_crossfade_block(
    out_block: np.ndarray, in_block: np.ndarray, start_pos: int, total: int
) -> np.ndarray:
    """Mix one callback's worth of frames inside a crossfade.

    `out_block` is the outgoing (fading down) PCM and `in_block` the
    incoming (fading up), both float32 ``(frames, channels)`` of equal
    length. `start_pos` is the fade position, in samples, of the block's
    first frame; `total` is the full fade length in samples. The gain
    ramps per-sample across the block (not once per block) so there's no
    zipper noise at low buffer sizes. Returns a new float32 block.
    """
    n = out_block.shape[0]
    positions = np.arange(start_pos, start_pos + n)
    out_g, in_g = equal_power_gains(positions, total)
    return out_block * out_g[:, None] + in_block * in_g[:, None]
