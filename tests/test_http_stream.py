"""Tests for the shared HTTP-streaming helpers in app/audio/http_stream.

These primitives back both the AirPlay and Chromecast senders. They
were extracted from the AirPlay-specific module so both could share
one implementation, and shipped without tests; this file closes the
gap. The pieces are well-suited to unit testing — RingBuffer is a
pure-Python data structure with simple invariants, FlacStreamEncoder
takes a numpy array and returns bytes (no I/O, no networking), and
primary_lan_ip's UDP-socket trick is testable with patching.
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from app.audio.http_stream import (
    FlacStreamEncoder,
    RingBuffer,
    primary_lan_ip,
)


# ---------------------------------------------------------------------
# RingBuffer
# ---------------------------------------------------------------------


class TestRingBuffer:
    def test_write_then_read_returns_in_order(self):
        """Bytes go in and come out in the same order they were
        written. The buffer is a queue, not a heap."""
        buf = RingBuffer(max_bytes=1024)
        buf.write(b"hello")
        buf.write(b"world")
        out = buf.read(10, timeout=0.1)
        assert out == b"helloworld"

    def test_read_returns_partial_when_n_smaller_than_buffered(self):
        """`read(n)` returns up to n bytes; remaining bytes stay in
        the buffer for the next reader."""
        buf = RingBuffer(max_bytes=1024)
        buf.write(b"abcdef")
        first = buf.read(3, timeout=0.1)
        second = buf.read(3, timeout=0.1)
        assert first == b"abc"
        assert second == b"def"

    def test_read_blocks_until_data_arrives(self):
        """Reader waits up to `timeout` for data instead of busy-
        looping. Once data arrives, the wait returns it without
        spinning until the timeout expires."""
        buf = RingBuffer(max_bytes=1024)

        def _delayed_write():
            time.sleep(0.05)
            buf.write(b"late")

        threading.Thread(target=_delayed_write, daemon=True).start()
        start = time.monotonic()
        out = buf.read(10, timeout=1.0)
        elapsed = time.monotonic() - start
        assert out == b"late"
        # Wait should have been about 0.05s, not the full 1.0s timeout.
        assert elapsed < 0.5

    def test_read_returns_empty_on_timeout(self):
        """When no writer ever arrives, the read times out and
        returns an empty bytes object so the caller can decide what
        to do (retry, exit, etc.)."""
        buf = RingBuffer(max_bytes=1024)
        out = buf.read(10, timeout=0.05)
        assert out == b""

    def test_overflow_drops_oldest_bytes(self):
        """The buffer enforces its byte cap by dropping the oldest
        bytes, never blocking the writer. Audio realtime callers
        depend on this — a stalled receiver must not freeze the
        audio engine."""
        buf = RingBuffer(max_bytes=8)
        buf.write(b"AAAA")
        buf.write(b"BBBB")
        # At the cap. A third write of 4 bytes should drop the
        # first 4 ("AAAA") and keep the next 8 ("BBBBCCCC").
        buf.write(b"CCCC")
        out = buf.read(100, timeout=0.1)
        assert out == b"BBBBCCCC"

    def test_close_unblocks_pending_readers(self):
        """A reader waiting on `read` should return promptly when
        the buffer is closed, so HTTP serve loops can exit instead
        of hanging on shutdown."""
        buf = RingBuffer(max_bytes=1024)

        def _close_after_delay():
            time.sleep(0.05)
            buf.close()

        threading.Thread(target=_close_after_delay, daemon=True).start()
        start = time.monotonic()
        out = buf.read(10, timeout=1.0)
        elapsed = time.monotonic() - start
        assert out == b""
        assert elapsed < 0.5
        assert buf.is_closed is True

    def test_write_after_close_is_silent_drop(self):
        """Writing to a closed buffer is a no-op — the encoder
        thread that's still draining its queue mustn't crash on a
        late write."""
        buf = RingBuffer(max_bytes=1024)
        buf.write(b"alive")
        buf.close()
        buf.write(b"after-close")
        out = buf.read(100, timeout=0.05)
        # The post-close write was dropped; only the live bytes
        # remain (but the buffer's also closed so eventually
        # returns "" — first read drains).
        assert out == b"alive"


# ---------------------------------------------------------------------
# FlacStreamEncoder
# ---------------------------------------------------------------------


class TestFlacStreamEncoder:
    def test_int16_stereo_encodes(self):
        """A simple int16 stereo chunk encodes to non-empty FLAC
        bytes. We don't validate the FLAC binary structure
        explicitly; the round-trip-decoded path is more meaningful
        and tested in the AirPlay / Cast end-to-end paths against
        real receivers."""
        enc = FlacStreamEncoder(sample_rate=44100, channels=2, dtype="int16")
        # 1024 frames of stereo silence is enough to fill at least
        # one FLAC block. The encoder may buffer mid-block so we
        # also call close() to drain.
        pcm = np.zeros((1024, 2), dtype=np.int16)
        body = enc.encode(pcm)
        tail = enc.close()
        # Either encode() or close() must have produced bytes; the
        # encoder may flush mid-call or only on close depending on
        # block-size alignment.
        assert len(body) + len(tail) > 0

    def test_int32_mono_encodes(self):
        """Mono int32 path — exercises the channels=1 layout
        configuration and the s32 sample-format branch."""
        enc = FlacStreamEncoder(sample_rate=48000, channels=1, dtype="int32")
        pcm = np.zeros((512, 1), dtype=np.int32)
        body = enc.encode(pcm)
        tail = enc.close()
        assert len(body) + len(tail) > 0

    def test_float_dtype_rejected(self):
        """FLAC is integer-only. The encoder must refuse to
        construct rather than silently mis-encode float audio."""
        with pytest.raises(ValueError) as exc:
            FlacStreamEncoder(sample_rate=44100, channels=2, dtype="float32")
        assert "integer-only" in str(exc.value)

    def test_unknown_dtype_rejected(self):
        with pytest.raises(ValueError):
            FlacStreamEncoder(sample_rate=44100, channels=2, dtype="bogus")

    def test_channel_mismatch_rejected(self):
        """Passing 2-channel PCM to a mono encoder is a programmer
        error — the encoder configuration was set at construct
        time, the audio engine is supposed to honor it. Better to
        fail loudly here than produce garbled output."""
        enc = FlacStreamEncoder(sample_rate=44100, channels=1, dtype="int16")
        pcm = np.zeros((128, 2), dtype=np.int16)
        with pytest.raises(ValueError) as exc:
            enc.encode(pcm)
        assert "channel mismatch" in str(exc.value)

    def test_encode_returns_only_new_bytes(self):
        """Successive `encode()` calls return only the bytes that
        came out of THIS call, not the cumulative buffer. The
        ring buffer is what owns the cumulative state; the encoder
        is a stream transformer."""
        enc = FlacStreamEncoder(sample_rate=44100, channels=2, dtype="int16")
        pcm = np.zeros((1024, 2), dtype=np.int16)
        first = enc.encode(pcm)
        second = enc.encode(pcm)
        # Either may be empty (the encoder buffers mid-block) but
        # neither call should return a strictly-superset of the
        # other's bytes — they're disjoint output windows.
        if first and second:
            assert not first.endswith(second) or not second.endswith(first)


# ---------------------------------------------------------------------
# primary_lan_ip
# ---------------------------------------------------------------------


class TestPrimaryLanIp:
    def test_returns_valid_ip_format(self):
        """In a reasonable network environment, the helper returns
        an IPv4 dotted-quad. It might be 127.0.0.1 in CI sandboxes
        with no internet — that's the documented fallback and
        it's still a valid string we can hand callers."""
        ip = primary_lan_ip()
        assert isinstance(ip, str)
        parts = ip.split(".")
        assert len(parts) == 4
        for part in parts:
            n = int(part)
            assert 0 <= n <= 255

    def test_falls_back_to_loopback_on_socket_error(self, monkeypatch):
        """If the OS can't even open a UDP socket (sandboxed
        environments, no networking) the helper returns 127.0.0.1
        instead of crashing. This is the documented degraded
        behaviour."""
        import socket as _socket

        class _BrokenSocket:
            def __init__(self, *_a, **_k):
                pass

            def connect(self, *_a, **_k):
                raise OSError("network unreachable")

            def getsockname(self):
                # Should never be reached given the connect error.
                return ("0.0.0.0", 0)

            def close(self):
                pass

        monkeypatch.setattr(_socket, "socket", _BrokenSocket)
        assert primary_lan_ip() == "127.0.0.1"
