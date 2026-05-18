"""Regression guard for the track-change audio "explosion".

The audio callback is lock-free. `_swap_pipeline_to` rewrites the
pipeline refs (queue, decoder, ReplayGain) from another thread on a
track change while the stream stays open. Before the `_swapping`
guard, the callback could run mid-swap and push the new track's
samples through the old track's ReplayGain/filter state — a split
second of full-scale clipped audio. These tests pin the guard shut:
the callback must emit silence while a swap is in progress, and the
swap must hold the guard across the ReplayGain re-derive.
"""
from __future__ import annotations

import logging
import queue
import threading
from types import SimpleNamespace

import numpy as np

from app.audio.player import PCMPlayer, audio_log


def _player() -> PCMPlayer:
    # session_getter is never invoked at construction; a stub keeps
    # the test offline.
    return PCMPlayer(lambda: None)


def test_callback_is_silent_and_non_draining_while_swapping():
    p = _player()
    frames = 256
    p._pcm_queue.put(np.ones((frames, 2), dtype=np.int16))
    depth_before = p._pcm_queue.qsize()

    p._swapping = True
    out = np.ones((frames, 2), dtype=np.int16)
    p._audio_callback(out, frames, None, None)

    assert np.count_nonzero(out) == 0, "must output silence during a swap"
    assert (
        p._pcm_queue.qsize() == depth_before
    ), "must not drain the queue during a swap"


def test_swap_holds_guard_across_replaygain_then_clears():
    p = _player()
    seen_during_swap = []

    # The guard must still be set when ReplayGain is re-derived —
    # that's the exact window the bug lived in.
    p._apply_replaygain_for = lambda info: seen_during_swap.append(
        p._swapping
    )

    pre = SimpleNamespace(
        decoder=object(),
        queue=queue.Queue(),
        thread=threading.Thread(target=lambda: None),
        stop_flag=threading.Event(),
        done=threading.Event(),
        track_id="new-track",
        duration_ms=1000,
        stream_info=SimpleNamespace(
            track_replay_gain_db=-3.0, album_replay_gain_db=-2.0
        ),
        source_urls=None,
        source_path="/tmp/x.flac",
        sample_rate=44100,
        channels=2,
        sd_dtype="int16",
    )

    records = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r.getMessage())
    audio_log.addHandler(handler)
    try:
        p._swap_pipeline_to(pre)
    finally:
        audio_log.removeHandler(handler)

    assert seen_during_swap == [True], "guard must be set during the swap"
    assert p._swapping is False, "guard must clear after the swap"
    assert p._current_track_id == "new-track"
    assert any("swap" in m and "new-track" in m for m in records), (
        "swap must be logged for post-incident tracing"
    )
