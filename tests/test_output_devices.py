"""Tests for name-based output-device resolution (issue #245).

`resolve_output_device` is the fix for playback breaking when the saved
PortAudio index drifts onto a different device. It maps a saved device
*name* to a live output-capable index, and reports when the saved
device is gone so the player can fall back to the system default
instead of opening an output stream on, say, a microphone.
"""
from app.audio import output_devices


# The exact device layout from the issue #245 diagnostic: the Bose is
# output-capable, index 1 is the MacBook mic (input only), and the
# built-in speakers are output-capable. A saved index of "1" used to
# resolve to the mic and crash sd.OutputStream.
_DEVICES = [
    {"name": "Bose Flex SE SoundLink", "max_output_channels": 2,
     "max_input_channels": 0, "hostapi": 0},
    {"name": "MacBook Pro Microphone", "max_output_channels": 0,
     "max_input_channels": 1, "hostapi": 0},
    {"name": "MacBook Pro Speakers", "max_output_channels": 2,
     "max_input_channels": 0, "hostapi": 0},
]


def _patch_devices(monkeypatch, devices):
    monkeypatch.setattr(
        output_devices.sd, "query_devices", lambda: devices
    )


def test_empty_name_is_system_default(monkeypatch):
    _patch_devices(monkeypatch, _DEVICES)
    assert output_devices.resolve_output_device("") == (None, True)


def test_output_device_resolves_to_its_current_index(monkeypatch):
    _patch_devices(monkeypatch, _DEVICES)
    assert output_devices.resolve_output_device(
        "MacBook Pro Speakers"
    ) == (2, True)
    assert output_devices.resolve_output_device(
        "Bose Flex SE SoundLink"
    ) == (0, True)


def test_input_only_device_is_not_available(monkeypatch):
    """The #245 crash: the saved slot now points at a microphone with
    zero output channels. It must report unavailable, not hand back an
    index we'd try to open an output stream on."""
    _patch_devices(monkeypatch, _DEVICES)
    assert output_devices.resolve_output_device(
        "MacBook Pro Microphone"
    ) == (None, False)


def test_legacy_numeric_id_matches_nothing(monkeypatch):
    """A pre-migration index string won't match any device name."""
    _patch_devices(monkeypatch, _DEVICES)
    assert output_devices.resolve_output_device("1") == (None, False)


def test_unplugged_device_is_not_available(monkeypatch):
    _patch_devices(monkeypatch, _DEVICES)
    assert output_devices.resolve_output_device(
        "USB DAC that got unplugged"
    ) == (None, False)


def test_query_failure_reports_unavailable(monkeypatch):
    def _boom():
        raise RuntimeError("PortAudio not initialized")

    monkeypatch.setattr(output_devices.sd, "query_devices", _boom)
    assert output_devices.resolve_output_device(
        "MacBook Pro Speakers"
    ) == (None, False)


def test_duplicate_name_prefers_wasapi_on_windows(monkeypatch):
    """When the same name appears under several host APIs (the Windows
    duplicate-enumeration case), prefer the WASAPI copy so we land on
    the entry the picker showed."""
    devices = [
        {"name": "Speakers", "max_output_channels": 2,
         "max_input_channels": 0, "hostapi": 0},   # MME
        {"name": "Speakers", "max_output_channels": 2,
         "max_input_channels": 0, "hostapi": 3},   # WASAPI
    ]
    _patch_devices(monkeypatch, devices)
    monkeypatch.setattr(
        output_devices, "wasapi_host_api_index", lambda: 3
    )
    assert output_devices.resolve_output_device("Speakers") == (1, True)
