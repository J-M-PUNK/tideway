"""A stream that dies mid-track must not pass for the end of the track.

The decoder thread used to swallow the failure and signal "done", the
callback then spliced in the preload (or the stream finished as a
natural EOF), and the queue advanced: tracks skipped at random. Now
the failure is carried on the pipeline's done event, the callback
won't splice past it, and the stream end reports a session-scoped
error, which the frontend doesn't skip.
"""
from __future__ import annotations

import queue
import threading

import numpy as np
import pytest
import sounddevice as sd

from app.audio.player import PCMPlayer, _DecodeDone


def _player() -> PCMPlayer:
    return PCMPlayer(lambda: None)


class _Decoder:
    """Yields `chunks` PCM chunks, then raises `exc` (or ends)."""

    def __init__(self, chunks: int, exc=None, on_raise=None):
        self._left = chunks
        self._exc = exc
        self._on_raise = on_raise

    def next_pcm(self):
        if self._left > 0:
            self._left -= 1
            return np.zeros((64, 2), dtype=np.int16)
        if self._exc is not None:
            if self._on_raise is not None:
                self._on_raise()
            raise self._exc
        return None


def _run_loop(decoder, stop_flag=None):
    q: queue.Queue = queue.Queue()
    stop = stop_flag or threading.Event()
    done = _DecodeDone()
    PCMPlayer._decoder_loop(decoder, q, stop, done)
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return done, items


def test_mid_track_failure_is_recorded_on_done():
    done, items = _run_loop(_Decoder(3, ConnectionError("segment 7 reset")))
    assert done.is_set()
    assert done.error is not None
    assert "segment 7 reset" in done.error
    # The audio decoded before the failure still plays, then the sentinel.
    assert len(items) == 4 and items[-1] is None


def test_natural_end_has_no_error():
    done, items = _run_loop(_Decoder(2))
    assert done.is_set() and done.error is None
    assert items[-1] is None


def test_stop_abort_is_not_a_failure():
    """Seek / track change / teardown abort the source under the
    decoder; the resulting exception is the intended exit."""
    stop = threading.Event()
    done, _ = _run_loop(
        _Decoder(1, ConnectionError("response closed"), on_raise=stop.set),
        stop_flag=stop,
    )
    assert done.is_set() and done.error is None


class _FakeStream:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _finished_after_failure(p: PCMPlayer) -> _FakeStream:
    fake = _FakeStream()
    p._stream = fake
    p._state = "playing"
    p._replacing_stream = False
    p._pcm_queue = queue.Queue()
    p._decoder_done = _DecodeDone()
    p._decoder_done.error = "Stream interrupted (ConnectionError: reset)"
    p._decoder_done.set()
    return fake


def test_stream_end_after_failure_reports_session_error():
    p = _player()
    p._preload = None
    fake = _finished_after_failure(p)

    p._on_stream_finished()

    snap = p.snapshot()
    assert snap.state == "error"
    assert snap.error_scope == "session"
    assert "ConnectionError" in snap.error
    assert p._stream is None and fake.closed


def test_stream_end_after_failure_does_not_bridge_to_preload(monkeypatch):
    """With a preload waiting, the old code bridged straight into it:
    the skip. The failure path must leave the preload alone."""
    p = _player()
    sentinel = object()
    p._preload = sentinel
    _finished_after_failure(p)
    monkeypatch.setattr(
        p, "_bridge_to_preload",
        lambda pre: pytest.fail("bridged past a failed track"),
    )

    p._on_stream_finished()

    assert p._state == "error"
    assert p._preload is sentinel


def test_callback_does_not_splice_preload_after_failure(monkeypatch):
    p = _player()
    p._callback_carry = None
    p._pcm_queue = queue.Queue()
    p._decoder_done = _DecodeDone()
    p._decoder_done.error = "Stream interrupted"
    p._decoder_done.set()
    monkeypatch.setattr(
        p, "_try_gapless_swap",
        lambda: pytest.fail("spliced the next track over a failure"),
    )
    out = np.full((256, 2), 9, dtype=np.int16)

    with pytest.raises(sd.CallbackStop):
        p._audio_callback(out, 256, None, None)
    assert not out.any()


def test_clearing_the_error_clears_its_scope():
    p = _player()
    p._set_error("Stream interrupted", scope="session")
    p._set_error("Track load timed out")
    assert p.snapshot().error_scope is None
    p._set_error("x", scope="session")
    p._set_error(None, scope="session")
    assert p.snapshot().error is None and p.snapshot().error_scope is None


def test_snapshot_dict_carries_error_scope():
    """The frontend decides skip-or-stay from `error_scope`; it has to
    reach the JSON the player SSE stream sends."""
    import server

    p = _player()
    p._set_error("Stream interrupted", scope="session")
    p._state = "error"
    assert server._snapshot_dict(p.snapshot())["error_scope"] == "session"


def test_play_after_interruption_reopens_where_it_stopped(monkeypatch):
    """The failure closed the output stream, so plain play() had nothing
    to start and the Play button did nothing. It must reload the track
    and seek back to the position it stopped at."""
    p = _player()
    p._stream = None
    p._state = "error"
    p._set_error("Stream interrupted", scope="session")
    p._current_track_id = "101"
    p._source_quality = "high_lossless"
    p._current_duration_ms = 200_000
    p._stream_sample_rate = 44100
    p._samples_emitted = 44100 * 50  # stopped 50 s in
    calls = []

    class _Stream:
        active = False

    def _load(track_id, quality=None):
        calls.append(("load", track_id, quality))
        with p._lock:
            p._set_error(None)
            p._state = "paused"
            p._stream = _Stream()  # an open output stream again
        return p.snapshot()

    monkeypatch.setattr(p, "load", _load)
    monkeypatch.setattr(
        p, "seek", lambda frac: calls.append(("seek", round(frac, 3))),
    )
    monkeypatch.setattr(p, "_start_stream", lambda stream: None)

    snap = p.play()

    assert calls == [("load", "101", "high_lossless"), ("seek", 0.25)]
    assert snap.state == "playing"


def test_play_after_track_error_is_unchanged():
    """Only connection-scoped errors reopen; a plain error keeps the old
    behaviour (no stream, nothing to start)."""
    p = _player()
    p._stream = None
    p._state = "error"
    p._set_error("Track load timed out")
    p._current_track_id = "101"
    assert p.play().state == "error"
