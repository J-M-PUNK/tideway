"""Tests for the OpenHome service-specific controllers (slice 3).

Each controller is a thin object around `invoke()` — its job is to
translate Python-native types to the strings SOAP wants and parse
out-args back into the right types. We mock invoke() at the module
level and assert that each method calls it with the correct
(action_name, args) pair, plus that out-arg parsing handles the
expected device responses.

When real hardware shows up, the same fixtures should hold; any
divergence pinpoints exactly which Python-to-SOAP translation
needs adjusting for that vendor.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.audio import openhome
from app.audio.openhome import (
    InfoController,
    OpenHomeDevice,
    OpenHomeService,
    PlaylistController,
    TimeController,
    VolumeController,
)


def _service(short_name: str) -> OpenHomeService:
    return OpenHomeService(
        service_type=f"urn:av-openhome-org:service:{short_name}:1",
        service_id=f"urn:av-openhome-org:serviceId:{short_name}",
        short_name=short_name,
        control_url=f"http://192.168.1.50/{short_name}/control",
        event_sub_url=f"http://192.168.1.50/{short_name}/event",
        scpd_url=f"http://192.168.1.50/{short_name}/scpd.xml",
        actions=(),
    )


def _device(*services: OpenHomeService) -> OpenHomeDevice:
    return OpenHomeDevice(
        udn="uuid:test",
        friendly_name="Test Device",
        manufacturer="Test",
        model_name="TestModel",
        model_number="1",
        services=tuple(services),
    )


# ---------------------------------------------------------------------
# PlaylistController
# ---------------------------------------------------------------------


class TestPlaylistController:
    def test_from_device_returns_none_when_service_missing(self):
        dev = _device()  # no Playlist service
        assert PlaylistController.from_device(dev) is None

    def test_from_device_returns_controller_when_service_present(self):
        dev = _device(_service("Playlist"))
        controller = PlaylistController.from_device(dev)
        assert controller is not None

    def test_play_invokes_no_args(self):
        controller = PlaylistController(_service("Playlist"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {}
            controller.play()
        mock_invoke.assert_called_once()
        args, _ = mock_invoke.call_args
        assert args[1] == "Play"

    def test_pause_invokes_pause_action(self):
        controller = PlaylistController(_service("Playlist"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {}
            controller.pause()
        args, _ = mock_invoke.call_args
        assert args[1] == "Pause"

    def test_stop_invokes_stop_action(self):
        controller = PlaylistController(_service("Playlist"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {}
            controller.stop()
        args, _ = mock_invoke.call_args
        assert args[1] == "Stop"

    def test_next_track_avoids_python_builtin_clash(self):
        """The method is named `next_track` not `next` because plain
        `next` shadows Python's `next()` builtin. Pin the name so a
        future rename doesn't quietly break callers."""
        controller = PlaylistController(_service("Playlist"))
        assert hasattr(controller, "next_track")
        assert not hasattr(controller, "next")
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {}
            controller.next_track()
        args, _ = mock_invoke.call_args
        assert args[1] == "Next"

    def test_previous_track(self):
        controller = PlaylistController(_service("Playlist"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {}
            controller.previous_track()
        args, _ = mock_invoke.call_args
        assert args[1] == "Previous"

    def test_insert_returns_new_id(self):
        """Insert returns the device's NewId out-arg as an int.
        Slice 4 stores it for SeekId / DeleteId targeting."""
        controller = PlaylistController(_service("Playlist"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {"NewId": "42"}
            new_id = controller.insert(
                after_id=0,
                uri="http://x/track.flac",
                metadata="<x/>",
            )
        assert new_id == 42

    def test_insert_passes_args_correctly(self):
        controller = PlaylistController(_service("Playlist"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {"NewId": "0"}
            controller.insert(
                after_id=5,
                uri="http://x/track.flac",
                metadata='<DIDL-Lite xmlns="..."/>',
            )
        args, _ = mock_invoke.call_args
        assert args[1] == "Insert"
        kwargs_dict = args[2]
        assert kwargs_dict["AfterId"] == "5"
        assert kwargs_dict["Uri"] == "http://x/track.flac"
        assert "DIDL-Lite" in kwargs_dict["Metadata"]

    def test_insert_handles_missing_new_id(self):
        """A firmware that returns a malformed Insert response
        without a NewId shouldn't crash; we return 0 as a sentinel."""
        controller = PlaylistController(_service("Playlist"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {}
            new_id = controller.insert(0, "http://x/y", "")
        assert new_id == 0

    def test_insert_handles_non_integer_new_id(self):
        """Defensive — if a buggy firmware returns NewId=foo we
        shouldn't propagate ValueError into the audio engine."""
        controller = PlaylistController(_service("Playlist"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {"NewId": "garbage"}
            new_id = controller.insert(0, "http://x/y", "")
        assert new_id == 0

    def test_seek_second_passes_int_as_string(self):
        controller = PlaylistController(_service("Playlist"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {}
            controller.seek_second(120)
        args, _ = mock_invoke.call_args
        assert args[2] == {"Value": "120"}

    def test_seek_second_truncates_floats(self):
        """SeekSecond is integer-only per spec. If a caller passes
        120.7 we round to 120 (int truncation), not 121 — Python's
        int() truncates toward zero, which matches what most
        clients do."""
        controller = PlaylistController(_service("Playlist"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {}
            controller.seek_second(120.7)
        args, _ = mock_invoke.call_args
        assert args[2] == {"Value": "120"}

    def test_delete_all_no_args(self):
        controller = PlaylistController(_service("Playlist"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {}
            controller.delete_all()
        args, _ = mock_invoke.call_args
        assert args[1] == "DeleteAll"

    def test_seek_id(self):
        controller = PlaylistController(_service("Playlist"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {}
            controller.seek_id(42)
        args, _ = mock_invoke.call_args
        assert args[1] == "SeekId"
        assert args[2] == {"Value": "42"}


# ---------------------------------------------------------------------
# VolumeController
# ---------------------------------------------------------------------


class TestVolumeController:
    def test_from_device_missing(self):
        dev = _device()
        assert VolumeController.from_device(dev) is None

    def test_from_device_present(self):
        dev = _device(_service("Volume"))
        assert VolumeController.from_device(dev) is not None

    def test_set_volume_scales_percentage_to_device_max(self):
        """A percentage 0-100 input must scale to the device's
        VolumeMax range. With VolumeMax=80, 50% should land on 40
        in device units."""
        controller = VolumeController(_service("Volume"))
        with patch.object(openhome, "invoke") as mock_invoke:
            # First call is VolumeMax lookup (returns 80), second
            # is SetVolume.
            mock_invoke.side_effect = [
                {"Value": "80"},
                {},
            ]
            controller.set_volume(50)
        # Check the SetVolume call (second one).
        set_call = mock_invoke.call_args_list[1]
        args, _ = set_call
        assert args[1] == "SetVolume"
        assert args[2] == {"Value": "40"}

    def test_set_volume_clamps_below_zero(self):
        controller = VolumeController(_service("Volume"))
        controller._cached_max = 100  # bypass lookup
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {}
            controller.set_volume(-5)
        args, _ = mock_invoke.call_args
        assert args[2] == {"Value": "0"}

    def test_set_volume_clamps_above_100(self):
        controller = VolumeController(_service("Volume"))
        controller._cached_max = 100
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {}
            controller.set_volume(150)
        args, _ = mock_invoke.call_args
        assert args[2] == {"Value": "100"}

    def test_volume_max_caches(self):
        """VolumeMax is constant per device. Cache the first
        result so set_volume / get_volume don't round-trip on
        every call."""
        controller = VolumeController(_service("Volume"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {"Value": "80"}
            assert controller.volume_max() == 80
            assert controller.volume_max() == 80
        # Should have invoked exactly once across two reads.
        assert mock_invoke.call_count == 1

    def test_volume_max_falls_back_to_100_on_error(self):
        """Some firmwares don't expose VolumeMax (it's not
        required by the spec). The wrapper falls back silently
        to 100 rather than raising or returning None."""
        controller = VolumeController(_service("Volume"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.side_effect = RuntimeError("no such action")
            assert controller.volume_max() == 100

    def test_get_volume_scales_back_to_percentage(self):
        controller = VolumeController(_service("Volume"))
        controller._cached_max = 50
        with patch.object(openhome, "invoke") as mock_invoke:
            # Volume action returns 25 (out of 50 max).
            mock_invoke.return_value = {"Value": "25"}
            assert controller.get_volume() == 50

    def test_set_mute_serializes_bool(self):
        controller = VolumeController(_service("Volume"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {}
            controller.set_mute(True)
        args, _ = mock_invoke.call_args
        assert args[2] == {"Value": "true"}

    def test_set_mute_false(self):
        controller = VolumeController(_service("Volume"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {}
            controller.set_mute(False)
        args, _ = mock_invoke.call_args
        assert args[2] == {"Value": "false"}

    def test_get_mute_parses_bool(self):
        controller = VolumeController(_service("Volume"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {"Value": "true"}
            assert controller.get_mute() is True
            mock_invoke.return_value = {"Value": "false"}
            assert controller.get_mute() is False

    def test_get_mute_handles_unexpected_value(self):
        """A firmware returning something other than true/false
        defaults to False rather than crashing."""
        controller = VolumeController(_service("Volume"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {"Value": "yes"}
            assert controller.get_mute() is False


# ---------------------------------------------------------------------
# TimeController
# ---------------------------------------------------------------------


class TestTimeController:
    def test_from_device_missing(self):
        assert TimeController.from_device(_device()) is None

    def test_time_returns_typed_dict(self):
        controller = TimeController(_service("Time"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {
                "Duration": "240",
                "Seconds": "37",
                "TrackCount": "5",
            }
            result = controller.time()
        assert result == {
            "duration": 240,
            "seconds": 37,
            "track_count": 5,
        }

    def test_time_handles_missing_fields(self):
        """Some firmwares omit fields when the value is zero. The
        wrapper defaults missing fields to 0 instead of raising."""
        controller = TimeController(_service("Time"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {"Duration": "180"}
            result = controller.time()
        assert result == {
            "duration": 180,
            "seconds": 0,
            "track_count": 0,
        }

    def test_time_handles_non_integer_values(self):
        controller = TimeController(_service("Time"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {
                "Duration": "garbage",
                "Seconds": "37",
                "TrackCount": "5",
            }
            result = controller.time()
        assert result["duration"] == 0
        assert result["seconds"] == 37


# ---------------------------------------------------------------------
# InfoController
# ---------------------------------------------------------------------


class TestInfoController:
    def test_from_device_missing(self):
        assert InfoController.from_device(_device()) is None

    def test_track_returns_uri_and_metadata(self):
        controller = InfoController(_service("Info"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {
                "Uri": "http://x/track.flac",
                "Metadata": '<DIDL-Lite>...</DIDL-Lite>',
            }
            result = controller.track()
        assert result["uri"] == "http://x/track.flac"
        assert "DIDL-Lite" in result["metadata"]

    def test_track_handles_missing_fields(self):
        controller = InfoController(_service("Info"))
        with patch.object(openhome, "invoke") as mock_invoke:
            mock_invoke.return_value = {}
            result = controller.track()
        assert result == {"uri": "", "metadata": ""}
