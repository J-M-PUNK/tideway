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
    _FLAC_CRC16_POLY,
    _FLAC_CRC8_POLY,
    TrackFileSource,
    _flac_coded_number,
    _flac_crc,
    _parse_flac_frame_header,
    _renumber_flac_frame,
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


def _flac_frame(number: int, payload: bytes) -> bytes:
    """A FLAC frame header numbered `number` around `payload`, with valid
    checksums. 4096 sample blocks, 44.1 kHz, stereo, 16 bit."""
    header = bytearray(b"\xff\xf8\xc9\x18") + _flac_coded_number(number)
    header.append(_flac_crc(header, _FLAC_CRC8_POLY, 8))
    frame = bytes(header) + payload
    return frame + _flac_crc(frame, _FLAC_CRC16_POLY, 16).to_bytes(2, "big")


class _Packet:
    def __init__(self, pts: int, tag: int) -> None:
        self.pts = pts
        self._tag = tag

    def __bytes__(self) -> bytes:
        return _flac_frame(self._tag, bytes([self._tag]) * 4)


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


def _first_frame(path: str) -> bytes:
    with open(path, "rb") as f:
        data = f.read()
    # fLaC + 4-byte metadata block header + 34-byte STREAMINFO
    return data[42:]


def _first_frame_tag(path: str) -> int:
    frame = _first_frame(path)
    return frame[_parse_flac_frame_header(frame)[2] + 1]


def test_trim_drops_leading_frames_and_rewrites_total_samples(monkeypatch):
    # Test Changelog:
    # - 2026-11-20 Will Pratt: Packets are now real FLAC frames, and the
    #   first kept frame must be numbered 0. It used to keep its number
    #   from the full track, which made Sonos stop at once.
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
    # First kept frame is the one at 10s (tag 10), renumbered to 0.
    assert _first_frame_tag(ts.path) == 10
    assert _parse_flac_frame_header(_first_frame(ts.path))[0] == 0
    # total_samples trimmed by the skipped 10s.
    assert _streaminfo_total_samples(data[8:42]) == total - 44100 * 10
    ts.close()


def test_no_trim_keeps_everything(monkeypatch):
    # Test Changelog:
    # - 2026-11-20 Will Pratt: Packets are now real FLAC frames, since
    #   trimmed files parse each frame header.
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


# ---------------------------------------------------------------------
# Frame renumbering
# ---------------------------------------------------------------------


def _encoded_flac(frame_count: int) -> tuple[bytes, list[bytes]]:
    """Encode noise to FLAC with PyAV. Returns the stream header bytes
    and the frames in order."""
    import io

    import av
    import numpy as np

    out_buf = io.BytesIO()
    out = av.open(out_buf, "w", format="flac")
    stream = out.add_stream("flac", rate=44100, layout="stereo")
    stream.format = "s16"
    rng = np.random.default_rng(7)
    for i in range(frame_count):
        samples = rng.integers(-3000, 3000, size=(1, 4608 * 2)).astype("int16")
        frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="stereo")
        frame.sample_rate = 44100
        frame.pts = i * 4608
        for packet in stream.encode(frame):
            out.mux(packet)
    for packet in stream.encode(None):
        out.mux(packet)
    out.close()
    data = out_buf.getvalue()
    frames = [bytes(p) for p in av.open(io.BytesIO(data)).demux(audio=0) if p.size]
    return data[: data.index(frames[0])], frames


def _decoded(data: bytes) -> bytes:
    import io

    import av

    return b"".join(
        bytes(f.planes[0]) for f in av.open(io.BytesIO(data)).decode(audio=0)
    )


def _checksums_valid(frame: bytes) -> bool:
    header_length = _parse_flac_frame_header(frame)[2]
    return (
        _flac_crc(frame[:header_length], _FLAC_CRC8_POLY, 8) == frame[header_length]
        and _flac_crc(frame[:-2], _FLAC_CRC16_POLY, 16)
        == int.from_bytes(frame[-2:], "big")
    )


def test_flac_coded_number_round_trips_every_length():
    # Test Changelog:
    # - 2026-11-20 Will Pratt: Added with frame renumbering for trimmed files.
    for value in (0, 0x7F, 0x80, 0x7FF, 0x800, 0xFFFF, 0x10000, 2**31 - 1, 2**36 - 1):
        frame = _flac_frame(value, b"\x00" * 4)
        assert _parse_flac_frame_header(frame)[0] == value
    with pytest.raises(ValueError):
        _flac_coded_number(2**36)


def test_parse_flac_frame_header_rejects_non_flac():
    # Test Changelog:
    # - 2026-11-20 Will Pratt: Added with frame renumbering for trimmed files.
    with pytest.raises(ValueError):
        _parse_flac_frame_header(b"\x00" * 16)
    with pytest.raises(ValueError):
        _parse_flac_frame_header(b"\xff\xf8\xc9\x18\x80\x00\x00\x00")


def test_renumber_flac_frame_keeps_checksums_valid():
    # Test Changelog:
    # - 2026-11-20 Will Pratt: Added. The CRC-16 is updated from the old one
    #   rather than recomputed, so check it against a full recompute,
    #   including numbers that change the header length.
    _header, frames = _encoded_flac(6)
    assert all(_checksums_valid(f) for f in frames)
    for frame in frames:
        for number in (0, 1, 200, 70000, 2**31 - 1):
            renumbered = _renumber_flac_frame(frame, number)
            assert _parse_flac_frame_header(renumbered)[0] == number
            assert _checksums_valid(renumbered)


def test_renumbered_trim_decodes_to_the_same_audio():
    # Test Changelog:
    # - 2026-11-20 Will Pratt: Added. Renumbering must not change a single
    #   decoded sample of the trimmed audio.
    header, frames = _encoded_flac(12)
    kept = frames[5:]
    first = _parse_flac_frame_header(kept[0])[0]
    renumbered = [
        _renumber_flac_frame(f, _parse_flac_frame_header(f)[0] - first)
        for f in kept
    ]
    assert _parse_flac_frame_header(renumbered[0])[0] == 0
    assert _decoded(header + b"".join(renumbered)) == _decoded(
        header + b"".join(kept)
    )
