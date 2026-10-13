"""Issue #354 fix B: trim the bounded file served to the renderer so it
begins at the desktop decoder's current position (resume-from-position).

The renderer is the DLNA playback clock and startles at byte 0 of the
bounded file. On a resume the desktop decoder is already N seconds in,
so byte 0 must be N — otherwise the decoder's EOF fires early and the
next track's SetAVTransportURI cuts the tail.

Native FLAC can't be trimmed by rewriting a header: `_run` drops the
leading frames and rewrites STREAMINFO's total_samples. These pin the
STREAMINFO field parser, the trim offset decision, and the demux trim
itself (with a synthetic container, since the real source is an fMP4
fetched from Tidal).
"""
from __future__ import annotations

import sys
import threading
import types
from fractions import Fraction
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock

import pytest

from app.audio.http_stream import (
    TrackFileSource,
    _streaminfo_total_samples,
)
from app.audio.upnp import UpnpManager


# ---------------------------------------------------------------------
# STREAMINFO helper
# ---------------------------------------------------------------------


def _streaminfo_with_total(total_samples: int) -> bytes:
    s = bytearray(34)
    s[13] = (s[13] & 0xF0) | ((total_samples >> 32) & 0x0F)
    s[14] = (total_samples >> 24) & 0xFF
    s[15] = (total_samples >> 16) & 0xFF
    s[16] = (total_samples >> 8) & 0xFF
    s[17] = total_samples & 0xFF
    return bytes(s)


def test_streaminfo_total_samples_roundtrip():
    assert _streaminfo_total_samples(_streaminfo_with_total(2646000)) == 2646000


def test_streaminfo_total_samples_zero_and_missing():
    assert _streaminfo_total_samples(_streaminfo_with_total(0)) == 0
    assert _streaminfo_total_samples(b"") == 0
    assert _streaminfo_total_samples(b"\x00" * 10) == 0


# ---------------------------------------------------------------------
# Trim offset decision
# ---------------------------------------------------------------------


def _manager(provider) -> UpnpManager:
    mgr = UpnpManager.__new__(UpnpManager)
    mgr._position_provider = provider
    return mgr


def test_resume_offset_zero_without_provider():
    assert _manager(None)._resume_offset_s() == 0.0


def test_resume_offset_zero_for_none_and_sub_second():
    assert _manager(lambda: None)._resume_offset_s() == 0.0
    assert _manager(lambda: 0.4)._resume_offset_s() == 0.0


def test_resume_offset_returns_seconds():
    assert _manager(lambda: 27.73)._resume_offset_s() == pytest.approx(27.73)


def test_resume_offset_absorbs_provider_error():
    def _boom():
        raise RuntimeError("player gone")

    assert _manager(_boom)._resume_offset_s() == 0.0


# ---------------------------------------------------------------------
# Demux trim (synthetic container)
# ---------------------------------------------------------------------


class _Packet:
    def __init__(self, pts: int, tag: int) -> None:
        self.pts = pts
        self._tag = tag

    def __bytes__(self) -> bytes:
        return bytes([self._tag]) * 4


class _FakeStream:
    type = "audio"
    time_base = Fraction(1, 44100)

    def __init__(self, duration_samples: int, extradata: bytes) -> None:
        self.duration = duration_samples
        self.codec_context = SimpleNamespace(
            sample_rate=44100,
            layout=SimpleNamespace(nb_channels=2),
            extradata=extradata,
        )


class _FakeContainer:
    def __init__(self, stream: _FakeStream, packets) -> None:
        self.streams = [stream]
        self._packets = packets

    def demux(self, _stream):
        return iter(self._packets)

    def close(self) -> None:
        pass


def _install_fake_av(monkeypatch, container: _FakeContainer) -> None:
    fake = types.ModuleType("av")
    fake.open = lambda *a, **k: container
    monkeypatch.setitem(sys.modules, "av", fake)


def _make_extradata(total_samples: int) -> bytes:
    # avcodec-style extradata: 0x80 + 3 length bytes + STREAMINFO
    return b"\x80" + b"\x00\x00\x00" + _streaminfo_with_total(total_samples)


def _first_frame_tag(path: str) -> int:
    with open(path, "rb") as f:
        data = f.read()
    # fLaC + 4-byte metadata block header + 34-byte STREAMINFO
    return data[42]


def test_trim_drops_leading_frames_and_rewrites_total_samples(monkeypatch):
    total = 44100 * 60
    stream = _FakeStream(
        duration_samples=total, extradata=_make_extradata(total)
    )
    packets = [_Packet(i * 44100, i) for i in range(15)]
    _install_fake_av(monkeypatch, _FakeContainer(stream, packets))

    ts = TrackFileSource(object(), start_s=10.0)
    ts.start()
    assert ts.ready.wait(timeout=5.0)
    assert not ts.failed

    with open(ts.path, "rb") as f:
        data = f.read()
    assert data[:4] == b"fLaC"
    # First kept frame is the one at 10s (tag 10).
    assert _first_frame_tag(ts.path) == 10
    # total_samples trimmed by the skipped 10s.
    assert _streaminfo_total_samples(data[8:42]) == total - 44100 * 10
    ts.close()


def test_no_trim_keeps_everything(monkeypatch):
    total = 44100 * 60
    stream = _FakeStream(
        duration_samples=total, extradata=_make_extradata(total)
    )
    packets = [_Packet(i * 44100, i) for i in range(5)]
    _install_fake_av(monkeypatch, _FakeContainer(stream, packets))

    ts = TrackFileSource(object(), start_s=0.0)
    ts.start()
    assert ts.ready.wait(timeout=5.0)
    assert not ts.failed

    with open(ts.path, "rb") as f:
        data = f.read()
    assert _first_frame_tag(ts.path) == 0
    assert _streaminfo_total_samples(data[8:42]) == total
    ts.close()
