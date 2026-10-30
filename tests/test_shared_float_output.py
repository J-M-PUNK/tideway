"""Shared-mode streams on Windows / macOS run as float32.

WASAPI's and CoreAudio's mixers are float32, so integer output buys no
bit-perfectness in shared mode: PortAudio converts it to float32 in C,
exactly, before the mixer sees it. What integer output does cost is in
the realtime callback, whose volume / DSP stage then has to cast
int -> float -> int. numpy releases the GIL during those casts, so the
callback must win it back from request threads mid-block, which under
load starves the stream. These pin the policy, its wiring through the
places that configure a decoder for the open stream, and that the
float conversion at the source rate is exact.
"""
from __future__ import annotations

import sys

import av
import numpy as np
import pytest

from app.audio import player
from app.audio.decoder import Decoder
from app.audio.player import PCMPlayer


# --- policy ------------------------------------------------------------------


@pytest.mark.parametrize(
    "platform, exclusive, expected",
    [
        ("win32", False, True),
        ("darwin", False, True),
        ("win32", True, False),
        ("darwin", True, False),
        # PortAudio can reach an ALSA hw device directly on Linux, where
        # integer output can still matter; left as it was.
        ("linux", False, False),
        ("linux", True, False),
    ],
)
def test_float_output_policy(monkeypatch, platform, exclusive, expected):
    monkeypatch.setattr(sys, "platform", platform)
    assert PCMPlayer._float_output_for(exclusive) is expected


# --- wiring ------------------------------------------------------------------


class _FakeDecoder:
    """Records set_target_rate calls and mirrors Decoder's dtype rules."""

    def __init__(self, rate: int = 44100, sd_dtype: str = "int16"):
        self.sample_rate = rate
        self.output_sample_rate = rate
        self.channels = 2
        self.sounddevice_dtype = sd_dtype
        self.calls: list[tuple[int, bool]] = []

    def set_target_rate(self, rate, *, float_output=False):
        self.calls.append((rate, float_output))
        self.output_sample_rate = rate
        if float_output or rate != self.sample_rate:
            self.sounddevice_dtype = "float32"

    def next_pcm(self):
        return None

    def close(self):
        pass

    def cancel_source(self):
        pass


def _open(monkeypatch, *, platform, exclusive, fail_exclusive=False):
    monkeypatch.setattr(sys, "platform", platform)
    opened: list[dict] = []

    def fake_output_stream(**kwargs):
        if fail_exclusive and "extra_settings" in kwargs:
            raise RuntimeError("device refused the format")
        opened.append(kwargs)
        return object()

    monkeypatch.setattr(player.sd, "OutputStream", fake_output_stream)
    monkeypatch.setattr(
        player.sd,
        "query_devices",
        lambda device=None, kind=None: {
            "default_high_output_latency": 0.01,
            "default_samplerate": 44100.0,
            "hostapi": 0,
        },
    )
    monkeypatch.setattr(player.sd, "query_hostapis", lambda i=None: {"name": "fake"})
    monkeypatch.setattr(player, "resolve_output_device", lambda _id: (None, True))
    p = PCMPlayer(lambda: None)
    p._exclusive_mode = exclusive
    dec = _FakeDecoder()
    p._decoder = dec
    p._open_output_stream(44100, 2, "int16")
    return p, dec, opened


def test_shared_stream_on_windows_is_float32(monkeypatch):
    p, dec, opened = _open(monkeypatch, platform="win32", exclusive=False)
    assert dec.calls == [(44100, True)]
    assert opened[-1]["dtype"] == "float32"
    assert p._stream_sd_dtype == "float32"


def test_exclusive_stream_keeps_the_source_format(monkeypatch):
    p, dec, opened = _open(monkeypatch, platform="win32", exclusive=True)
    assert dec.calls == [(44100, False)]
    assert opened[-1]["dtype"] == "int16"


def test_shared_stream_on_linux_keeps_the_source_format(monkeypatch):
    p, dec, opened = _open(monkeypatch, platform="linux", exclusive=False)
    assert dec.calls == [(44100, False)]
    assert opened[-1]["dtype"] == "int16"


def test_exclusive_fallback_to_shared_switches_to_float32(monkeypatch):
    # The device refused exclusive mode, so the retry is a shared-mode
    # stream even though the user's setting still says exclusive.
    p, dec, opened = _open(
        monkeypatch, platform="win32", exclusive=True, fail_exclusive=True
    )
    assert dec.calls[-1] == (44100, True)
    assert opened[-1]["dtype"] == "float32"


def test_load_pipeline_matches_a_float_shared_stream(monkeypatch):
    # Path A builds the next track's decoder against the open stream so
    # the swap can keep it open; in shared mode that means float32 too.
    monkeypatch.setattr(sys, "platform", "win32")
    p = PCMPlayer(lambda: None)
    p._exclusive_mode = False
    dec = _FakeDecoder()
    monkeypatch.setattr(
        p, "_resolve_source", lambda tid, q: ("track.flac", 1.0, None, None, None)
    )
    monkeypatch.setattr(p, "_open_stream_source", lambda spec, pre: spec)
    monkeypatch.setattr(player, "Decoder", lambda source: dec)
    pipeline = p._build_load_pipeline("t1", None, match_rate=44100)
    pipeline.stop_flag.set()
    pipeline.thread.join(timeout=2)
    assert dec.calls == [(44100, True)]
    assert pipeline.sd_dtype == "float32"


# --- the conversion itself ---------------------------------------------------


def _write_int_flac(path, ints: np.ndarray, fmt: str, rate: int) -> None:
    """Write interleaved stereo integer samples losslessly to FLAC."""
    out = av.open(str(path), "w")
    st = out.add_stream("flac", rate=rate)
    st.format = fmt
    st.layout = "stereo"
    frame = av.AudioFrame.from_ndarray(ints.reshape(1, -1), format=fmt, layout="stereo")
    frame.sample_rate = rate
    for pkt in st.encode(frame):
        out.mux(pkt)
    for pkt in st.encode(None):
        out.mux(pkt)
    out.close()


def _drain(dec: Decoder) -> np.ndarray:
    chunks = []
    while True:
        c = dec.next_pcm()
        if c is None:
            break
        chunks.append(c)
    return np.concatenate(chunks, axis=0)


@pytest.mark.parametrize("bits", [16, 24])
def test_float_output_at_the_source_rate_is_exact(tmp_path, bits):
    """The float32 samples equal the integer samples scaled to [-1, 1),
    with nothing else changed: no headroom, no resampling, no dither.
    That is what PortAudio's own int -> float32 conversion produces for
    a float32 mixer, so the OS receives the same values as before."""
    rate = 44100
    rng = np.random.default_rng(1)
    n = 4096
    if bits == 16:
        ints = rng.integers(-32768, 32767, size=n * 2, dtype=np.int16)
        fmt, scale = "s16", 32768.0
    else:
        # 24-bit audio in an s32 container, the way libav frames it.
        ints = (rng.integers(-(2**23), 2**23 - 1, size=n * 2).astype(np.int32)) << 8
        fmt, scale = "s32", 2.0**31
    path = tmp_path / f"src{bits}.flac"
    _write_int_flac(path, ints, fmt, rate)

    passthrough = Decoder(str(path))
    expected = _drain(passthrough).astype(np.float64) / scale
    passthrough.close()

    dec = Decoder(str(path))
    dec.set_target_rate(rate, float_output=True)
    got = _drain(dec)
    dec.close()

    assert got.dtype == np.float32
    assert dec.is_resampling_internally is False
    assert got.shape == expected.shape
    assert np.array_equal(got.astype(np.float64), expected)
