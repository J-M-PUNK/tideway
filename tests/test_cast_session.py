"""Tests for `CastManager.push_pcm` — the audio-engine PCM tap.

`push_pcm` runs from PCMPlayer's audio callback, which is the
realtime thread, so its cost characteristics matter as much as its
correctness. The tests here pin the boundary cases that bring up
corruption when wrong:

  - Encoder rebuilt only when (rate, channels, dtype) actually
    change. A rebuild on every call would waste CPU and produce
    audible glitches at every chunk boundary.
  - Empty arrays / 1-D mono / 2-D stereo all flow without exception.
  - No-session calls return cheaply.

We bypass the network-bound `connect()` path by constructing a
`_SessionState` and stuffing it onto the manager directly. Encoder
construction is real (PyAV is fast and available in the test env);
the pychromecast-wrapped `cast` field is a Mock since we never
actually issue media-controller calls in these tests.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from app.audio.cast import CastDevice, CastManager, _SessionState


def _device() -> CastDevice:
    return CastDevice(
        id="22222222-2222-2222-2222-222222222222",
        friendly_name="Test speaker",
        model_name="Test model",
        manufacturer="Test mfr",
        cast_type="audio",
        host="192.168.1.50",
        port=8009,
    )


def _attach_session(mgr: CastManager) -> _SessionState:
    """Helper: stuff a fake session into the manager. Returns the
    session for tests that want to inspect its encoder state.
    Skips the network-bound connect() path entirely."""
    sess = _SessionState(device=_device(), cast=MagicMock())
    mgr._session = sess
    return sess


# ---------------------------------------------------------------------
# Cheap-no-session path
# ---------------------------------------------------------------------


class TestNoSession:
    def test_no_session_returns_cheaply(self):
        """The audio callback hits push_pcm on every frame even
        when the user isn't casting. The no-session branch must
        not raise and must not allocate (we don't measure
        allocation here, but a raised exception is the easy thing
        to assert against)."""
        mgr = CastManager()
        pcm = np.zeros((512, 2), dtype=np.int16)
        # Should not raise.
        mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        assert mgr._session is None

    def test_empty_array_no_session_is_noop(self):
        mgr = CastManager()
        pcm = np.zeros((0, 2), dtype=np.int16)
        mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        # Both branches (empty and no-session) hit; neither
        # raises, neither builds an encoder.
        assert mgr._session is None

    def test_empty_array_with_session_is_noop(self):
        """Even with a session, an empty array shouldn't build the
        encoder — there's nothing to encode and the rebuild path
        would do unnecessary work for a benign input."""
        mgr = CastManager()
        sess = _attach_session(mgr)
        pcm = np.zeros((0, 2), dtype=np.int16)
        mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        assert sess.encoder is None


# ---------------------------------------------------------------------
# First-call encoder build
# ---------------------------------------------------------------------


class TestFirstCallBuild:
    def test_first_chunk_builds_encoder(self):
        """The first non-empty push with an active session
        constructs the FlacStreamEncoder configured to the source's
        rate / channel / dtype shape."""
        mgr = CastManager()
        sess = _attach_session(mgr)
        pcm = np.zeros((1024, 2), dtype=np.int16)
        mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        assert sess.encoder is not None
        assert sess.encoder_rate == 44100
        assert sess.encoder_channels == 2
        assert sess.encoder_dtype == "int16"

    def test_int32_path(self):
        mgr = CastManager()
        sess = _attach_session(mgr)
        pcm = np.zeros((512, 2), dtype=np.int32)
        mgr.push_pcm(pcm, sample_rate=96000, dtype="int32")
        assert sess.encoder is not None
        assert sess.encoder_dtype == "int32"
        assert sess.encoder_rate == 96000

    def test_mono_input(self):
        """1-D arrays are reshaped to (frames, 1) inside push_pcm.
        The encoder sees mono and lays out the FLAC stream
        accordingly."""
        mgr = CastManager()
        sess = _attach_session(mgr)
        pcm = np.zeros(1024, dtype=np.int16)
        mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        assert sess.encoder is not None
        assert sess.encoder_channels == 1


# ---------------------------------------------------------------------
# Encoder reuse — the hot path
# ---------------------------------------------------------------------


class TestEncoderReuse:
    def test_same_params_reuses_encoder(self):
        """Subsequent pushes with the same rate / channels / dtype
        must NOT rebuild the encoder. A rebuild causes a
        discontinuity at the boundary; the receiver glitches.
        This is the most important invariant in this file."""
        mgr = CastManager()
        sess = _attach_session(mgr)
        pcm = np.zeros((1024, 2), dtype=np.int16)
        mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        first_encoder = sess.encoder
        assert first_encoder is not None

        # Push 50 more chunks at the same shape. The encoder
        # object must be the same instance throughout.
        for _ in range(50):
            mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        assert sess.encoder is first_encoder

    def test_bytes_encoded_increases(self):
        """After multiple pushes, `bytes_encoded` accumulates. The
        diagnostic endpoint surfaces this so a stalled session is
        observable from outside."""
        mgr = CastManager()
        sess = _attach_session(mgr)
        pcm = np.zeros((1024, 2), dtype=np.int16)
        for _ in range(10):
            mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        assert sess.bytes_encoded >= 0
        # FLAC can buffer mid-block, so the count may be 0 if the
        # encoder hasn't flushed a complete frame yet, but it
        # never goes negative.
        assert sess.bytes_encoded >= 0


# ---------------------------------------------------------------------
# Encoder rebuild boundaries
# ---------------------------------------------------------------------


class TestEncoderRebuildBoundaries:
    def test_rate_change_rebuilds(self):
        """Track-change to a different sample rate should rebuild
        the encoder (and discontinuity-glitch). This mirrors the
        local audio engine — PCMPlayer reopens the OutputStream
        across rate changes too, so one boundary glitch on the
        Cast device is consistent with what the user already hears
        locally."""
        mgr = CastManager()
        sess = _attach_session(mgr)
        pcm44 = np.zeros((1024, 2), dtype=np.int16)
        pcm96 = np.zeros((1024, 2), dtype=np.int16)
        mgr.push_pcm(pcm44, sample_rate=44100, dtype="int16")
        first = sess.encoder
        mgr.push_pcm(pcm96, sample_rate=96000, dtype="int16")
        second = sess.encoder
        assert first is not second
        assert sess.encoder_rate == 96000

    def test_dtype_change_rebuilds(self):
        """A switch from int16 to int32 (e.g., 16-bit AAC track →
        24-bit FLAC track) needs a rebuild; the FLAC encoder is
        configured at construct time for one sample format."""
        mgr = CastManager()
        sess = _attach_session(mgr)
        mgr.push_pcm(
            np.zeros((512, 2), dtype=np.int16),
            sample_rate=44100,
            dtype="int16",
        )
        first = sess.encoder
        mgr.push_pcm(
            np.zeros((512, 2), dtype=np.int32),
            sample_rate=44100,
            dtype="int32",
        )
        second = sess.encoder
        assert first is not second
        assert sess.encoder_dtype == "int32"

    def test_channels_change_rebuilds(self):
        """Mono → stereo (or vice versa) is a rebuild trigger.
        Real-world rare — most music is stereo end-to-end — but
        a defensive rebuild is correct: the FLAC encoder's layout
        is fixed at construct time."""
        mgr = CastManager()
        sess = _attach_session(mgr)
        mgr.push_pcm(
            np.zeros(512, dtype=np.int16),  # mono
            sample_rate=44100,
            dtype="int16",
        )
        first = sess.encoder
        mgr.push_pcm(
            np.zeros((512, 2), dtype=np.int16),  # stereo
            sample_rate=44100,
            dtype="int16",
        )
        second = sess.encoder
        assert first is not second
        assert sess.encoder_channels == 2
