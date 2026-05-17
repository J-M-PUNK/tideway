"""Tests for the hi-res -> 16-bit / 44.1 kHz download downconvert.

Old iPods running Rockbox (and most legacy DAPs) can't decode
24-bit / >48 kHz FLAC in real time, so a "Max" download from Tidal
skips and locks the device up. With the setting on, the hi-res
download path runs the source through _transcode_to_cd_flac instead
of the bit-exact remux. These tests use real PyAV (a hard
dependency) end to end rather than mocks, so an API drift in the
resample/encode chain is caught.
"""
from __future__ import annotations

import av
import numpy as np
import pytest

from app.downloader import _audio_stream_is_hires, _transcode_to_cd_flac


def _write_flac(path, *, rate: int, bits: int, seconds: float = 0.5):
    """Synthesize a stereo sine FLAC at the given rate / bit depth.
    bits=16 -> s16 stream, bits=24 -> s32 stream (how libav frames
    24-bit FLAC)."""
    n = int(rate * seconds)
    t = np.arange(n) / rate
    left = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    right = (0.5 * np.sin(2 * np.pi * 660 * t)).astype(np.float32)
    inter = np.empty(n * 2, dtype=np.float32)
    inter[0::2] = left
    inter[1::2] = right
    out = av.open(str(path), "w")
    st = out.add_stream("flac", rate=rate)
    st.format = "s16" if bits <= 16 else "s32"
    st.layout = "stereo"
    frame = av.AudioFrame.from_ndarray(
        inter.reshape(1, -1), format="flt", layout="stereo"
    )
    frame.sample_rate = rate
    for pkt in st.encode(frame):
        out.mux(pkt)
    for pkt in st.encode(None):
        out.mux(pkt)
    out.close()


def _probe(path):
    c = av.open(str(path))
    try:
        a = c.streams.audio[0]
        rate = int(a.codec_context.sample_rate)
        fmt = a.format.name
        return rate, fmt
    finally:
        c.close()


def _decode_pcm(path) -> np.ndarray:
    c = av.open(str(path))
    try:
        a = c.streams.audio[0]
        chunks = [fr.to_ndarray().reshape(-1) for fr in c.decode(a)]
    finally:
        c.close()
    return np.concatenate(chunks) if chunks else np.array([])


@pytest.mark.parametrize(
    "rate,bits,expected",
    [
        (44100, 16, False),  # CD quality — leave alone
        (96000, 24, True),  # Tidal Max — downconvert
        (44100, 24, True),  # 24-bit @ CD rate still hi-res
        (96000, 16, True),  # high rate alone is enough
        (48000, 16, False),  # 48 kHz boundary is not hi-res
    ],
)
def test_hires_classification(tmp_path, rate, bits, expected):
    f = tmp_path / "src.flac"
    _write_flac(f, rate=rate, bits=bits)
    c = av.open(str(f))
    try:
        assert _audio_stream_is_hires(c.streams.audio[0]) is expected
    finally:
        c.close()


def test_transcode_hires_to_cd_flac(tmp_path):
    src = tmp_path / "hires.flac"
    dst = tmp_path / "cd.flac"
    _write_flac(src, rate=96000, bits=24, seconds=1.0)

    _transcode_to_cd_flac(src, dst)

    rate, fmt = _probe(dst)
    assert rate == 44100
    assert fmt == "s16"  # genuine 16-bit FLAC, not 24-bit
    pcm = _decode_pcm(dst).astype(np.float32) / 32768.0
    assert pcm.size > 0
    # Signal survived the resample (0.5 peak sines, allow dither/codec
    # headroom).
    assert 0.4 < float(np.max(np.abs(pcm))) < 0.6


def test_transcode_passthrough_is_bit_exact_for_cd_quality(tmp_path):
    src = tmp_path / "cd_in.flac"
    dst = tmp_path / "cd_out.flac"
    _write_flac(src, rate=44100, bits=16, seconds=0.5)

    _transcode_to_cd_flac(src, dst)

    # CD-quality in -> stream-copied out, not re-encoded: format and
    # rate unchanged and the decoded PCM is identical sample for
    # sample.
    assert _probe(dst) == (44100, "s16")
    assert np.array_equal(_decode_pcm(src), _decode_pcm(dst))
