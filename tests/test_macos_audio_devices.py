"""Tests for the CoreAudio-based output-device visibility query.

These tests verify the platform gate (returns None on non-darwin
without trying to dlopen) and exercise the live CoreAudio call on
macOS as a smoke test that the ctypes wiring still matches the
framework's ABI.

Per-device behavior (filters out virtual devices like Teams /
ZoomAudioDevice, includes real hardware) is tested in `test_player_*`
where the integration with `list_output_devices` is verified.
"""
from __future__ import annotations

import sys

import pytest

from app.audio import macos_audio_devices


def test_returns_none_on_non_darwin(monkeypatch):
    """On Linux / Windows the function must return None without
    attempting any dlopen — callers fall back to the unfiltered
    PortAudio list."""
    monkeypatch.setattr(macos_audio_devices.sys, "platform", "linux")
    assert macos_audio_devices.visible_output_device_names() is None


def test_returns_none_when_frameworks_dont_load(monkeypatch):
    """Edge case: someone on a frankenmac with the frameworks
    missing. Must return None, not raise. Easy regression to
    introduce by tightening the try/except."""
    monkeypatch.setattr(macos_audio_devices.sys, "platform", "darwin")
    monkeypatch.setattr(macos_audio_devices, "_ca", None)
    monkeypatch.setattr(macos_audio_devices, "_cf", None)
    monkeypatch.setattr(
        macos_audio_devices.ctypes.util, "find_library", lambda _name: None
    )
    assert macos_audio_devices.visible_output_device_names() is None


@pytest.mark.skipif(
    sys.platform != "darwin", reason="CoreAudio is macOS-only"
)
def test_live_query_returns_set_on_macos():
    """Smoke test: on a real Mac, the query must return SOMETHING
    (even a CI runner has at least one default audio device).
    Anything else means our ctypes wiring drifted from the
    CoreAudio ABI and needs investigation."""
    result = macos_audio_devices.visible_output_device_names()
    assert result is not None, (
        "CoreAudio query returned None on a Mac — ctypes wiring or "
        "framework dlopen broke"
    )
    assert isinstance(result, set), "must return a set of names"
    # Don't assert on contents — different machines have different
    # devices. We only care that the query worked at all.


@pytest.mark.skipif(
    sys.platform != "darwin", reason="CoreAudio is macOS-only"
)
def test_live_query_excludes_microphones():
    """Microphones return False on
    `kAudioDevicePropertyDeviceCanBeDefaultDevice` for the OUTPUT
    scope, so they must NOT appear in the visible-output set even
    though CoreAudio enumerates them at the device-id level. Most
    Macs have 'MacBook Pro Microphone' as a built-in input — if
    that ever shows up here, the ctypes scope constants drifted."""
    result = macos_audio_devices.visible_output_device_names()
    if result is None:
        pytest.skip("CoreAudio query unavailable on this machine")
    for name in result:
        assert "microphone" not in name.lower(), (
            f"microphone {name!r} leaked into the output picker — "
            "scope constant probably wrong"
        )
