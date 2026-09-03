"""The audio callback survives a seek clearing its buffer (#405).

Reported on Linux: repeated seeking through the progress bar during
DASH playback raised

    app/audio/player.py: 'NoneType' object has no attribute 'shape'

followed by "AUDIO STALL: callback silent for 10.7s while state=playing"
and, sometimes, the app closing.

`seek()` runs on the request thread and clears `_callback_carry` under
`_lock`. The audio callback is deliberately lock-free, so `_lock` does
not keep it out — it only stops a callback that has not started yet,
via the `_seeking` guard at the top. A callback already inside the fill
loop is past that guard, and the buffer it is mid-way through reading
goes None underneath it.

Two changes, both exercised here: the loop reads the buffer into a
local so nothing can null it mid-expression, and it re-checks
`_seeking` each iteration so an in-flight callback yields to a seek
instead of feeding pre-seek audio.
"""
from __future__ import annotations

import queue
import threading

import numpy as np

from app.audio.player import PCMPlayer


def _player() -> PCMPlayer:
    p = PCMPlayer(lambda: None)
    p._stream_sample_rate = 44100
    return p


def _chunk(frames: int = 512) -> np.ndarray:
    return np.zeros((frames, 2), dtype=np.int16)


def test_the_carry_is_read_once_into_a_local():
    """The crash fix, asserted structurally.

    The race cannot be reproduced in-process: the window between the
    None-check and the `.shape` read is a few bytecodes, and CPython
    only switches threads every 5 ms, so a Python writer thread never
    lands in it. A concurrent stress test passes with or without the
    fix, which makes it worse than no test — it looks like coverage.
    In production the callback runs on PortAudio's thread, which is
    what made it reachable.

    So assert the property that makes the crash impossible: the fill
    loop reads `_callback_carry` into a local and never dereferences
    the attribute afterwards.
    """
    import inspect

    src = inspect.getsource(PCMPlayer._audio_callback)
    body = src[src.index("while written < frames:") :]
    fill = body[: body.index("# Cast tap")] if "# Cast tap" in body else body

    assert "carry = self._callback_carry" in fill, (
        "the fill loop should take a local reference"
    )
    assert "self._callback_carry.shape" not in fill, (
        "dereferencing the attribute is the crash: a concurrent seek() "
        "nulls it between the check and the read"
    )


def test_seek_in_flight_yields_silence():
    """A callback that started before the seek must stop feeding the
    old track rather than finishing its buffer."""
    p = _player()
    p._callback_carry = _chunk(256)
    p._seeking = True
    out = np.full((256, 2), 999, dtype=np.int16)

    p._audio_callback(out, 256, None, None)

    assert not out.any(), "pre-seek audio was written after _seeking was set"


def test_swap_in_flight_yields_silence():
    """Same for a pipeline swap, which the callback already treats the
    same way at the top."""
    p = _player()
    p._callback_carry = _chunk(256)
    p._swapping = True
    out = np.full((256, 2), 999, dtype=np.int16)

    p._audio_callback(out, 256, None, None)

    assert not out.any()


def test_normal_playback_still_writes_audio():
    """The guards must not silence ordinary playback — a fix that
    always emits zeros would pass the tests above."""
    p = _player()
    p._callback_carry = np.full((256, 2), 7, dtype=np.int16)
    p._pcm_queue = queue.Queue()
    p._decoder_done = threading.Event()
    out = np.zeros((256, 2), dtype=np.int16)

    p._audio_callback(out, 256, None, None)

    assert out.any(), "no audio written during normal playback"


def test_carry_is_consumed_across_callbacks():
    """A carry larger than one callback is sliced, not dropped."""
    p = _player()
    p._callback_carry = np.full((512, 2), 5, dtype=np.int16)
    p._pcm_queue = queue.Queue()
    p._decoder_done = threading.Event()

    p._audio_callback(np.zeros((256, 2), dtype=np.int16), 256, None, None)

    assert p._callback_carry is not None
    assert p._callback_carry.shape[0] == 256
