"""Tests for `_is_virtual_audio_device` — the helper that keeps
Microsoft Teams Audio, Zoom's virtual mic, BlackHole, Loopback, and
similar virtual / aggregate devices out of the output device picker.

PortAudio enumerates these alongside real hardware (CoreAudio doesn't
distinguish at the API level), but they're never what a music player's
user wants to pick — and macOS System Settings → Sound → Output
doesn't show them either.

Tests pin both the block-list (real virtual devices) AND the must-pass
list (real hardware) to guard against pattern overreach. The block
patterns must be specific enough that a Behringer USB DAC named
"Loopback Mixer Pro" doesn't get swept up because of the word
"loopback".
"""
from __future__ import annotations

from app.audio import player as player_module


def test_blocks_microsoft_teams_audio():
    assert player_module._is_virtual_audio_device("Microsoft Teams Audio")
    assert player_module._is_virtual_audio_device("microsoft teams audio")
    assert player_module._is_virtual_audio_device("MICROSOFT TEAMS AUDIO ")


def test_blocks_zoom_devices():
    assert player_module._is_virtual_audio_device("ZoomAudioDevice")
    assert player_module._is_virtual_audio_device("Zoom Audio Device")


def test_blocks_blackhole_variants():
    assert player_module._is_virtual_audio_device("BlackHole 2ch")
    assert player_module._is_virtual_audio_device("BlackHole 16ch")
    assert player_module._is_virtual_audio_device("blackhole")


def test_blocks_loopback_audio():
    assert player_module._is_virtual_audio_device("Loopback Audio")


def test_blocks_aggregate_and_multi_output():
    assert player_module._is_virtual_audio_device("Aggregate Device")
    assert player_module._is_virtual_audio_device("Multi-Output Device")


def test_blocks_other_virtuals():
    assert player_module._is_virtual_audio_device("Krisp Speaker")
    assert player_module._is_virtual_audio_device("Soundflower (2ch)")
    assert player_module._is_virtual_audio_device("OBS Virtual Camera Audio")
    assert player_module._is_virtual_audio_device("VB-Cable")


def test_passes_real_hardware():
    """Hardware devices must NOT match the filter. Easy regression to
    introduce by tightening a pattern too far (e.g. bare "audio"
    would match every real device)."""
    real_names = [
        "MacBook Pro Speakers",
        "AirPods Pro",
        "AirPods Pro 2",
        "Bose QuietComfort",
        "External Headphones",
        "USB Audio Device",
        "Sony WH-1000XM4",
        "Schiit Modi 3",
        "Realtek USB Audio",
        "Built-in Output",
        "HDMI",
        "DisplayPort",
        # Edge case: hardware named "Loopback Mixer" — the pattern is
        # "loopback audio" not bare "loopback" so this should pass.
        "Behringer Loopback Mixer Pro",
    ]
    for name in real_names:
        assert not player_module._is_virtual_audio_device(name), (
            f"unexpectedly filtered out real device: {name!r}"
        )


def test_handles_empty_safely():
    assert not player_module._is_virtual_audio_device("")
    assert not player_module._is_virtual_audio_device("   ")
