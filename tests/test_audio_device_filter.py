"""Tests for `wasapi_host_api_index` — the helper that lets the
output-device picker hide Windows-disabled devices and the duplicate
MME / DirectSound / WDM-KS listings.

Real device enumeration depends on PortAudio's runtime view of the
system, which is platform-specific and isn't fixtureable. So we
monkeypatch `sys.platform` and `sd.query_hostapis` to exercise the
two paths the helper actually has."""
from __future__ import annotations

from unittest.mock import patch

from app.audio import output_devices as output_devices_module


def test_returns_none_on_non_windows():
    """The helper must short-circuit to None on macOS / Linux so the
    device picker shows everything PortAudio enumerates (which is
    correct behavior on those platforms — they have one host API)."""
    with patch.object(output_devices_module.sys, "platform", "darwin"):
        assert output_devices_module.wasapi_host_api_index() is None
    with patch.object(output_devices_module.sys, "platform", "linux"):
        assert output_devices_module.wasapi_host_api_index() is None


def test_finds_wasapi_index_on_windows():
    fake_host_apis = [
        {"name": "MME"},
        {"name": "Windows DirectSound"},
        {"name": "Windows WASAPI"},
        {"name": "Windows WDM-KS"},
    ]
    with patch.object(output_devices_module.sys, "platform", "win32"), \
         patch.object(output_devices_module.sd, "query_hostapis", return_value=fake_host_apis):
        assert output_devices_module.wasapi_host_api_index() == 2


def test_finds_wasapi_with_alt_casing():
    """sounddevice has reported both 'Windows WASAPI' and 'WASAPI'
    across versions. The helper uses a case-insensitive substring
    match so both forms work."""
    fake_host_apis = [
        {"name": "MME"},
        {"name": "wasapi"},  # lowercase, no prefix
    ]
    with patch.object(output_devices_module.sys, "platform", "win32"), \
         patch.object(output_devices_module.sd, "query_hostapis", return_value=fake_host_apis):
        assert output_devices_module.wasapi_host_api_index() == 1


def test_returns_none_when_wasapi_missing():
    """Older or stripped-down PortAudio builds may not include WASAPI.
    The helper returns None so `list_output_devices` falls through to
    the all-host-API listing (degraded but not broken)."""
    fake_host_apis = [
        {"name": "MME"},
        {"name": "Windows DirectSound"},
    ]
    with patch.object(output_devices_module.sys, "platform", "win32"), \
         patch.object(output_devices_module.sd, "query_hostapis", return_value=fake_host_apis):
        assert output_devices_module.wasapi_host_api_index() is None


def test_returns_none_on_query_exception():
    """If sd.query_hostapis raises (PortAudio in a weird state), the
    helper logs and returns None rather than crashing the picker."""
    def boom():
        raise RuntimeError("PortAudio crashed")

    with patch.object(output_devices_module.sys, "platform", "win32"), \
         patch.object(output_devices_module.sd, "query_hostapis", side_effect=boom):
        assert output_devices_module.wasapi_host_api_index() is None
