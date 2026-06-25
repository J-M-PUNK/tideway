"""PCMPlayer._output_latency_for picks the PortAudio output latency.

A fixed 100ms latency is right for wired/low-latency outputs (it's the
headroom the realtime callback needs against GIL contention) but starves
a Bluetooth device, whose A2DP pipeline buffers far deeper — the cause
of continuous crackle on BT output. The selector floors at 100ms but
honours a device's own deeper `default_high_output_latency` when it has
one. These pin that: floor for shallow devices, the deeper value for
Bluetooth-like ones, and a safe fallback when the query can't answer.
"""
from __future__ import annotations

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
