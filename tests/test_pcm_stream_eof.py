"""Regression guard for the dead-stream-after-EOF freeze.

When a track plays to natural end-of-file, the audio callback raises
`sd.CallbackStop`. PortAudio's docs say the stream is now in a
"stopped" terminal state and `Pa_StartStream` cannot revive it — the
stream must be reopened. Before this fix, `_on_stream_finished`
transitioned state to "ended" but left `self._stream` pointing at the
corpse. The next `load()` then snapshotted that ref as `active_stream`,
took path (A) ("kept stream open"), and rebound the new pipeline
against a dead OutputStream. `play()` called `stream.start()`, which
on macOS returns without error but never re-wakes the CoreAudio
callback thread. State said "playing", no callback fired, the
decoder filled the queue and blocked.

This test pins the contract: on natural EOF, the stream ref is
cleared AND the underlying OutputStream is closed.
"""
from __future__ import annotations

import queue
import threading

from app.audio.player import PCMPlayer


def _player() -> PCMPlayer:
    return PCMPlayer(lambda: None)


class _FakeStream:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_natural_eof_closes_and_nulls_stream():
    p = _player()
    fake = _FakeStream()
    p._stream = fake
    p._state = "playing"
    p._replacing_stream = False
    p._preload = None
    p._pcm_queue = queue.Queue()
    p._decoder_done = threading.Event()
    p._decoder_done.set()

    p._on_stream_finished()

    assert p._stream is None, (
        "natural EOF must clear self._stream so next load() takes "
        "the full-reopen path, not the kept-stream-open path"
    )
    assert fake.closed, "the dead OutputStream must be closed"
    assert p._state == "ended"


def test_eof_close_swallows_close_errors():
    """A close() that raises must not propagate — finished_callback
    runs on sounddevice's own thread and a bare exception there
    would crash the audio path entirely. The state transition still
    has to happen."""
    p = _player()

    class _RaisingStream:
        def close(self) -> None:
            raise RuntimeError("boom")

    p._stream = _RaisingStream()
    p._state = "playing"
    p._replacing_stream = False
    p._preload = None
    p._pcm_queue = queue.Queue()
    p._decoder_done = threading.Event()
    p._decoder_done.set()

    p._on_stream_finished()

    assert p._stream is None
    assert p._state == "ended"


def test_device_loss_path_does_not_null_stream():
    """The device-loss recovery path needs `self._stream` to still
    point at the (just-aborted) OutputStream so it can be replaced.
    The EOF close-and-null behavior must only fire on natural EOF,
    not when the decoder still has work pending."""
    p = _player()
    fake = _FakeStream()
    p._stream = fake
    p._state = "playing"
    p._replacing_stream = False
    p._preload = None
    p._pcm_queue = queue.Queue()
    p._decoder_done = threading.Event()
    # decoder NOT done -> device-loss heuristic should fire and
    # spawn the recovery thread, leaving self._stream alone.

    p._on_stream_finished()

    assert p._stream is fake, (
        "device-loss path must not null self._stream — recovery "
        "needs to reopen on a fresh device"
    )
    assert not fake.closed
