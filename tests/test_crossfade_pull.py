"""Tests for PCMPlayer._pull_block — the queue-draining core of the
crossfade callback mix.

The realtime mix can only be judged by ear, but the buffer-filling it
depends on is deterministic and pinned here: a full fill, leftover-carry
handling, the underrun-vs-end distinction (zeros either way, but
`source_done` only on a true end), and the stereo->mono downmix.
"""
from __future__ import annotations

import queue
import threading

import numpy as np

from app.audio.player import PCMPlayer


def _chunk(n: int, ch: int = 2, val: float = 1.0) -> np.ndarray:
    return np.full((n, ch), val, dtype=np.float32)


def test_fills_a_full_block_from_queued_chunks():
    q: queue.Queue = queue.Queue()
    q.put(_chunk(50, val=0.5))
    q.put(_chunk(50, val=0.25))
    buf, carry, filled, done = PCMPlayer._pull_block(
        q, None, threading.Event(), 100, 2
    )
    assert filled == 100
    assert carry is None
    assert done is False
    assert np.allclose(buf[:50], 0.5)
    assert np.allclose(buf[50:], 0.25)


def test_carries_the_leftover_of_an_oversized_chunk():
    q: queue.Queue = queue.Queue()
    q.put(_chunk(150, val=0.7))  # bigger than the 100-frame request
    buf, carry, filled, done = PCMPlayer._pull_block(
        q, None, threading.Event(), 100, 2
    )
    assert filled == 100
    assert carry is not None and carry.shape[0] == 50
    assert np.allclose(buf, 0.7)


def test_consumes_an_existing_carry_before_the_queue():
    buf, carry, filled, done = PCMPlayer._pull_block(
        queue.Queue(), _chunk(40, val=0.3), threading.Event(), 100, 2
    )
    # Carry supplied 40 frames; queue empty -> underrun, rest zeros.
    assert filled == 40
    assert done is False
    assert np.allclose(buf[:40], 0.3)
    assert np.all(buf[40:] == 0.0)


def test_underrun_leaves_zeros_without_marking_done():
    q: queue.Queue = queue.Queue()
    q.put(_chunk(30, val=0.9))
    # done event NOT set: the source is behind, not finished.
    buf, carry, filled, done = PCMPlayer._pull_block(
        q, None, threading.Event(), 100, 2
    )
    assert filled == 30
    assert done is False
    assert np.allclose(buf[:30], 0.9)
    assert np.all(buf[30:] == 0.0)


def test_reports_source_done_only_on_a_true_end():
    ev = threading.Event()
    ev.set()
    buf, carry, filled, done = PCMPlayer._pull_block(
        queue.Queue(), None, ev, 100, 2
    )
    assert filled == 0
    assert done is True
    assert np.all(buf == 0.0)


def test_downmixes_stereo_to_mono():
    q: queue.Queue = queue.Queue()
    chunk = np.zeros((10, 2), dtype=np.float32)
    chunk[:, 0] = 1.0  # L=1, R=0 -> mean 0.5
    q.put(chunk)
    buf, carry, filled, done = PCMPlayer._pull_block(
        q, None, threading.Event(), 10, 1
    )
    assert buf.shape == (10, 1)
    assert np.allclose(buf[:, 0], 0.5)
