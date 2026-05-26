"""The audio callback bumps `_seq` on a wall-clock cadence (~5 Hz),
not a callback-count cadence.

Why this matters: the SSE player-events stream sends a snapshot
every 250 ms (4 Hz) while playing, and the frontend's `usePlayer`
hook drops snapshots whose `seq` hasn't advanced past the last
one it saw. If the callback's seq-bump rate falls below the SSE
poll rate, the frontend silently drops most of those polls and
the scrubber jumps several seconds at a time instead of sliding
smoothly.

The pre-fix scheme bumped `_seq` every 20 callbacks. At a typical
44.1k / 512-frame configuration that's ~4.3 Hz, fine. But on
configurations with larger callback buffers — exclusive-mode
WASAPI on devices that prefer 2048-frame buffers, hi-res output
that opens with a multi-thousand-frame block — the callback
fires at 2-10 Hz, which translated to 0.1-0.5 Hz seq bumps. The
frontend saw the same seq across multiple SSE polls, dropped
them all, and the user got multi-second scrubber jumps.

Tests below verify the wall-clock cadence with the player's
internal monotonic clock mocked out — the callback path must
bump seq when 200 ms have elapsed, regardless of how many
callbacks fired in that window.
"""
from __future__ import annotations

import numpy as np

from app.audio import player as player_mod
from app.audio.player import PCMPlayer


def _player() -> PCMPlayer:
    """A no-network player. `session_getter` is never invoked at
    construction; a no-op keeps the test offline."""
    return PCMPlayer(lambda: None)


def _drive_callback(p: PCMPlayer, *, frames: int = 256) -> None:
    """One audio-callback invocation. The pipeline isn't really
    running so the queue is empty — the callback hits the underrun
    path, which is exactly the branch we want to exercise for the
    seq-bump (steady-state-playback codepath uses the same logic)."""
    out = np.zeros((frames, 2), dtype=np.int16)
    p._audio_callback(out, frames, None, None)


def test_seq_bumps_around_5hz_for_fast_callbacks(monkeypatch):
    """Fast-callback case: simulate 100 Hz callbacks (10 ms apart).
    Over 1.0 simulated second the callback fires 100 times, but
    seq should bump roughly 5 times — once per 200 ms window. Allow
    ±1 slack to absorb floating-point precision at the 0.2-second
    boundary (`0.6 - 0.4 == 0.19999…` in IEEE-754 doubles)."""
    p = _player()
    p._stream_sample_rate = 44100

    now = [0.0]
    monkeypatch.setattr(player_mod.time, "monotonic", lambda: now[0])

    p._seq = 0
    p._seq_last_bump_t = 0.0

    # 100 callbacks * 10 ms = 1.0 s.
    for _ in range(100):
        _drive_callback(p)
        now[0] += 0.010

    # Expect ~5 bumps; tolerate one missed due to float ordering at
    # exactly 200 ms.
    assert 4 <= p._seq <= 5, (
        f"expected ~5 seq bumps in 1s @ 100Hz, got {p._seq}"
    )


def test_seq_still_bumps_at_slow_callback_rate(monkeypatch):
    """Slow-callback case: simulate ~5 Hz callbacks (~200 ms apart).
    This is the WASAPI-exclusive / large-buffer regime the old
    `>= 20 callbacks` scheme regressed on — it would have taken
    20 callbacks (4 seconds) to bump seq once. The wall-clock check
    bumps roughly per callback here. Stagger the deltas slightly to
    avoid hitting the float-precision boundary at the threshold."""
    p = _player()
    p._stream_sample_rate = 44100

    now = [0.0]
    monkeypatch.setattr(player_mod.time, "monotonic", lambda: now[0])

    p._seq = 0
    p._seq_last_bump_t = 0.0

    # 5 callbacks each spaced ~205 ms apart — keeps every delta
    # safely above 0.2 even after float rounding.
    for _ in range(5):
        now[0] += 0.205
        _drive_callback(p)

    assert p._seq == 5, (
        f"expected 5 seq bumps for 5 well-spaced slow callbacks, "
        f"got {p._seq}"
    )


def test_seq_does_not_bump_between_callbacks_under_200ms(monkeypatch):
    """Rapid-fire callbacks (three calls within 100 ms) only bump seq
    on the first. The explicit cadence keeps the bump rate
    predictable and decoupled from the callback rate."""
    p = _player()
    p._stream_sample_rate = 44100

    now = [0.0]
    monkeypatch.setattr(player_mod.time, "monotonic", lambda: now[0])

    p._seq = 0
    p._seq_last_bump_t = 0.0

    # Establish the baseline bump at t=0.3 — well past the 200 ms
    # threshold from the t=0 init.
    now[0] = 0.3
    _drive_callback(p)
    assert p._seq == 1, "first callback past 200ms should bump"

    # Three more callbacks within the next 100 ms — no bump.
    for delta in (0.020, 0.040, 0.060):
        now[0] = 0.3 + delta
        _drive_callback(p)

    assert p._seq == 1, (
        f"three sub-200ms callbacks must not bump seq again, got {p._seq}"
    )

    # The next callback past the 200 ms window does bump again. Use
    # 0.55 to give the threshold check some slack.
    now[0] = 0.55
    _drive_callback(p)
    assert p._seq == 2, "callback past the 200ms window must bump again"
