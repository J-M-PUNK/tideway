"""Tests for the track-change responsiveness fix: backgrounded pipeline
disposal and the load-stall watchdog recovery.

The track-change dropout came from `_teardown` joining a wedged decoder
thread (~1s) on the request thread while holding the GIL, starving the
realtime audio callback. Disposal now runs on a daemon; these pin its
ordering (join then close) and the safety swallows. The watchdog's
load-stall recovery flips a stuck "loading" to a (recoverable) error.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

from app.audio.player import PCMPlayer


# --- _dispose_pipeline_async --------------------------------------------


def test_dispose_joins_a_live_thread_then_closes():
    thread = MagicMock()
    thread.is_alive.return_value = True
    decoder = MagicMock()
    PCMPlayer._dispose_pipeline_async(thread, decoder)
    thread.join.assert_called_once()  # bounded join
    decoder.close.assert_called_once()  # close AFTER the join


def test_dispose_skips_join_for_an_already_dead_thread():
    thread = MagicMock()
    thread.is_alive.return_value = False
    decoder = MagicMock()
    PCMPlayer._dispose_pipeline_async(thread, decoder)
    thread.join.assert_not_called()
    decoder.close.assert_called_once()


def test_dispose_tolerates_none_args():
    PCMPlayer._dispose_pipeline_async(None, None)  # must not raise


def test_dispose_swallows_a_close_error():
    decoder = MagicMock()
    decoder.close.side_effect = RuntimeError("container already gone")
    PCMPlayer._dispose_pipeline_async(None, decoder)  # must not raise
    decoder.close.assert_called_once()


# --- load-stall watchdog recovery ---------------------------------------


def _stuck_loading_player() -> PCMPlayer:
    p = PCMPlayer.__new__(PCMPlayer)
    p._lock = threading.RLock()
    p._state = "loading"
    p._current_track_id = "t1"
    p._last_error = None
    p._seq = 0
    # _emit() reads these; keep it a clean no-op (no listeners).
    p._listeners = []
    p.snapshot = MagicMock()  # type: ignore[method-assign]
    return p


def test_load_stall_forces_a_recoverable_error():
    p = _stuck_loading_player()
    p._force_load_stall_error()
    assert p._state == "error"
    assert p._last_error == "Track load timed out"
    assert p._seq == 1


def test_load_stall_is_a_noop_when_the_load_already_completed():
    # The load raced to completion between the watchdog's check and the
    # lock — must not stomp a now-playing track with an error.
    p = _stuck_loading_player()
    p._state = "playing"
    p._force_load_stall_error()
    assert p._state == "playing"
    assert p._last_error is None
    assert p._seq == 0
