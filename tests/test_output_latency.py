"""PCMPlayer picks the PortAudio output latency and callback block size.

A fixed 100ms latency is right for wired/low-latency outputs (it's the
headroom the realtime callback needs against GIL contention) but starves
a Bluetooth device, whose A2DP pipeline buffers far deeper — the cause
of continuous crackle on BT output. The selector floors at 100ms but
honours a device's own deeper `default_high_output_latency` when it has
one. These pin that: floor for shallow devices, the deeper value for
Bluetooth-like ones, and a safe fallback when the query can't answer.
"""
from __future__ import annotations

import sys

from app.audio import player
from app.audio.player import PCMPlayer


def test_floors_at_100ms_for_a_low_latency_device(monkeypatch):
    monkeypatch.setattr(
        player.sd,
        "query_devices",
        lambda device, kind=None: {"default_high_output_latency": 0.012},
    )
    assert PCMPlayer._output_latency_for(7) == 0.1


def test_uses_the_deeper_latency_for_a_bluetooth_device(monkeypatch):
    # A BT device reports a deep pipeline; honour it so the buffer
    # doesn't underrun.
    monkeypatch.setattr(
        player.sd,
        "query_devices",
        lambda device, kind=None: {"default_high_output_latency": 0.25},
    )
    assert PCMPlayer._output_latency_for(53) == 0.25


def test_falls_back_to_the_floor_when_the_query_fails(monkeypatch):
    def boom(device, kind=None):
        raise RuntimeError("no such device")

    monkeypatch.setattr(player.sd, "query_devices", boom)
    assert PCMPlayer._output_latency_for(None) == 0.1


def test_tolerates_a_missing_latency_field(monkeypatch):
    monkeypatch.setattr(
        player.sd,
        "query_devices",
        lambda device, kind=None: {"name": "device without a latency field"},
    )
    assert PCMPlayer._output_latency_for(3) == 0.1


# --- callback block size -------------------------------------------------
#
# Latency alone doesn't protect against sustained GIL contention: what
# matters there is how often the callback has to win the GIL. Left at
# PortAudio's default the callback runs every device period (10 ms on
# WASAPI shared) and, with a few request threads busy, can't win the GIL
# that often, so WASAPI plays silence without PortAudio flagging an
# underflow. The stream is opened with ~45 ms callbacks instead.


def test_block_is_about_45ms_at_common_rates():
    for rate, frames in (
        (44100, 2048),
        (48000, 2048),
        (88200, 4096),
        (96000, 4096),
        (176400, 8192),
        (192000, 8192),
    ):
        assert PCMPlayer._callback_block_frames(rate) == frames
        assert 0.035 <= frames / rate <= 0.05


class _FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _open_with(monkeypatch, *, exclusive=False, fail_exclusive=False):
    # Exclusive mode only builds extra_settings on Windows / macOS, so
    # pin the platform or the fallback path never runs on Linux CI.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(player.sd, "WasapiSettings", lambda **kw: object())
    opened: list[dict] = []

    def fake_output_stream(**kwargs):
        if fail_exclusive and "extra_settings" in kwargs:
            raise RuntimeError("device refused the format")
        opened.append(kwargs)
        return _FakeStream(**kwargs)

    monkeypatch.setattr(player.sd, "OutputStream", fake_output_stream)
    monkeypatch.setattr(
        player.sd,
        "query_devices",
        lambda device=None, kind=None: {
            "default_high_output_latency": 0.01,
            "default_samplerate": 48000.0,
            "hostapi": 0,
        },
    )
    monkeypatch.setattr(player.sd, "query_hostapis", lambda i=None: {"name": "fake"})
    monkeypatch.setattr(player, "resolve_output_device", lambda _id: (None, True))
    p = PCMPlayer(lambda: None)
    p._exclusive_mode = exclusive
    return p, opened


def test_stream_opens_with_the_callback_block(monkeypatch):
    p, opened = _open_with(monkeypatch)
    p._open_output_stream(44100, 2, "float32")
    assert opened[-1]["blocksize"] == 2048


def test_exclusive_fallback_recomputes_the_block_for_the_mixer_rate(monkeypatch):
    # Exclusive mode opens at the source rate; when the device refuses
    # it, the shared-mode retry runs at the mixer rate, and the block
    # has to be sized for that rate rather than the one that failed.
    class _Decoder:
        sample_rate = 192000
        channels = 2
        sounddevice_dtype = "float32"

        def __init__(self):
            self.output_sample_rate = 192000

        def set_target_rate(self, rate):
            self.output_sample_rate = rate

    p, opened = _open_with(monkeypatch, exclusive=True, fail_exclusive=True)
    p._decoder = _Decoder()
    p._open_output_stream(192000, 2, "float32")
    assert opened[-1]["samplerate"] == 48000
    assert opened[-1]["blocksize"] == PCMPlayer._callback_block_frames(48000)
