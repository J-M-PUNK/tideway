"""Unit tests for app/audio/cast.CastManager — discovery state,
sorting, status reporting, and the pychromecast browser callbacks.

These cover the parts that don't need a real Cast device or a live
pychromecast handshake: dict mutation under the discovery lock,
sort order for the picker UX, the status endpoint payload shape,
and the auto-disconnect path that fires when a connected device
disappears from mDNS.

The tests construct a fresh `CastManager` per test rather than use
the module-level singleton, so cross-test bleed (one test leaving
behind discovered devices that another then sees) can't happen.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.audio.cast import CastDevice, CastManager, is_audio_only


def _info(
    uuid: str,
    *,
    name: str = "Living Room speaker",
    model: str = "Google Nest Mini",
    manufacturer: str = "Google Inc.",
    cast_type: str = "audio",
    host: str = "192.168.1.20",
    port: int = 8009,
):
    """Build a SimpleNamespace that quacks like pychromecast's
    CastInfo for the fields we read in `_upsert`. SimpleNamespace
    is enough — we only attribute-access it, never type-check
    against the real class."""
    return SimpleNamespace(
        uuid=UUID(uuid),
        friendly_name=name,
        model_name=model,
        manufacturer=manufacturer,
        cast_type=cast_type,
        host=host,
        port=port,
    )


def _device(
    uuid: str,
    *,
    name: str = "device",
    cast_type: str = "audio",
) -> CastDevice:
    """Pre-built CastDevice for tests that bypass `_upsert` and
    poke devices into the manager dict directly."""
    return CastDevice(
        id=uuid,
        friendly_name=name,
        model_name="model",
        manufacturer="mfr",
        cast_type=cast_type,
        host="192.168.1.10",
        port=8009,
    )


# ---------------------------------------------------------------------
# is_audio_only
# ---------------------------------------------------------------------


class TestIsAudioOnly:
    def test_audio_speaker(self):
        d = _device("a", cast_type="audio")
        assert is_audio_only(d) is True

    def test_speaker_group(self):
        d = _device("a", cast_type="group")
        assert is_audio_only(d) is True

    def test_cast_built_in_tv(self):
        d = _device("a", cast_type="cast")
        assert is_audio_only(d) is False

    def test_unknown_type_treated_as_video(self):
        """Defensive default: anything we don't explicitly call
        out as audio sorts below speakers in the picker. Better to
        push an unknown speaker down than to push a TV up."""
        d = _device("a", cast_type="something-new")
        assert is_audio_only(d) is False


# ---------------------------------------------------------------------
# Manager state and sorting
# ---------------------------------------------------------------------


class TestListDevices:
    def test_empty_manager(self):
        mgr = CastManager()
        assert mgr.list_devices() == []

    def test_audio_first_then_video(self):
        """Picker UX: speakers above TVs. Within each group, sort
        alphabetically by friendly name (lowercased) so the user's
        eye doesn't have to bounce between case-folded entries."""
        mgr = CastManager()
        mgr._devices = {
            "id-tv": _device("id-tv", name="Living Room TV", cast_type="cast"),
            "id-speaker-b": _device("id-speaker-b", name="Bedroom", cast_type="audio"),
            "id-speaker-a": _device("id-speaker-a", name="Kitchen", cast_type="audio"),
        }
        names = [d.friendly_name for d in mgr.list_devices()]
        # Two audio devices in alphabetical order, then the TV.
        assert names == ["Bedroom", "Kitchen", "Living Room TV"]

    def test_alphabetical_case_insensitive(self):
        mgr = CastManager()
        mgr._devices = {
            "id1": _device("id1", name="zebra", cast_type="audio"),
            "id2": _device("id2", name="Apple", cast_type="audio"),
            "id3": _device("id3", name="banana", cast_type="audio"),
        }
        names = [d.friendly_name for d in mgr.list_devices()]
        assert names == ["Apple", "banana", "zebra"]


# ---------------------------------------------------------------------
# Status payload
# ---------------------------------------------------------------------


class TestStatus:
    def test_idle_state(self):
        """Just-constructed manager: discovery hasn't run, no
        devices, no session."""
        mgr = CastManager()
        s = mgr.status()
        assert s["available"] in (True, False)  # depends on dep install
        assert s["running"] is False
        assert s["device_count"] == 0
        assert s["last_event_age_s"] is None
        assert s["connected_id"] is None
        assert s["connected_name"] is None
        assert s["bytes_encoded"] == 0
        assert s["media_loaded"] is False

    def test_device_count_reflects_dict(self):
        mgr = CastManager()
        mgr._devices = {
            "id1": _device("id1"),
            "id2": _device("id2", name="other"),
        }
        s = mgr.status()
        assert s["device_count"] == 2

    def test_last_event_age_populated_after_callback(self):
        """`_last_event_at` is set inside the callback path. After
        a discovery event, status should expose a non-None float
        age that's small (we just set it)."""
        mgr = CastManager()
        mgr._browser = MagicMock()
        mgr._browser.devices = {UUID("11111111-1111-1111-1111-111111111111"): _info(
            "11111111-1111-1111-1111-111111111111"
        )}
        mgr._on_add(UUID("11111111-1111-1111-1111-111111111111"), None)
        s = mgr.status()
        assert s["last_event_age_s"] is not None
        assert s["last_event_age_s"] >= 0.0
        assert s["last_event_age_s"] < 5.0  # we just set it


# ---------------------------------------------------------------------
# Discovery callbacks
# ---------------------------------------------------------------------


class TestDiscoveryCallbacks:
    def test_on_add_inserts_device(self):
        """`_on_add` should pull info out of `browser.devices` and
        translate it into a `CastDevice` cached in the manager.
        This is the path that fires for every newly-discovered
        device."""
        mgr = CastManager()
        uuid = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        mgr._browser = MagicMock()
        mgr._browser.devices = {
            uuid: _info(str(uuid), name="Kitchen", model="Nest Mini")
        }
        mgr._on_add(uuid, None)
        devices = mgr.list_devices()
        assert len(devices) == 1
        assert devices[0].id == str(uuid)
        assert devices[0].friendly_name == "Kitchen"
        assert devices[0].model_name == "Nest Mini"

    def test_on_update_replaces_existing_record(self):
        """`_on_update` arrives when a device's metadata changes
        (rename, IP change). The cached record should be updated
        in place rather than producing a duplicate."""
        mgr = CastManager()
        uuid = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        mgr._browser = MagicMock()
        mgr._browser.devices = {uuid: _info(str(uuid), name="Old name")}
        mgr._on_add(uuid, None)
        # User renamed the device in Google Home.
        mgr._browser.devices = {uuid: _info(str(uuid), name="New name")}
        mgr._on_update(uuid, None)
        devices = mgr.list_devices()
        assert len(devices) == 1
        assert devices[0].friendly_name == "New name"

    def test_on_remove_drops_device(self):
        mgr = CastManager()
        uuid = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
        mgr._devices[str(uuid)] = _device(str(uuid))
        mgr._on_remove(uuid, None, None)
        assert mgr.list_devices() == []

    def test_on_remove_disconnects_active_session(self):
        """If the device that just disappeared is the one we're
        connected to, the manager auto-disconnects rather than
        keeping the encoder pumping into a session whose target is
        gone. This covers the Wi-Fi blip / device-power-off cases
        where the user shouldn't have to manually re-pick local
        output."""
        from app.audio.cast import _SessionState

        mgr = CastManager()
        uuid = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
        device = _device(str(uuid))
        mgr._devices[str(uuid)] = device
        # Stuff a fake session in. The Cast object is a Mock so
        # disconnect() can poke at its media_controller / disconnect
        # attributes without crashing.
        mgr._session = _SessionState(device=device, cast=MagicMock())
        mgr._on_remove(uuid, None, None)
        # The session should have been cleared.
        assert mgr._session is None

    def test_on_remove_unknown_device_is_noop(self):
        """A remove callback for a UUID we never saw shouldn't
        crash. pychromecast's listener can fire updates we don't
        have a corresponding `add` for if discovery raced startup."""
        mgr = CastManager()
        uuid = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
        mgr._on_remove(uuid, None, None)  # should not raise
        assert mgr.list_devices() == []

    def test_upsert_with_missing_browser_info_skips(self):
        """If `browser.devices` doesn't have the UUID (race during
        teardown), `_upsert` should silently skip rather than
        raise."""
        mgr = CastManager()
        mgr._browser = MagicMock()
        mgr._browser.devices = {}
        mgr._upsert(UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"))
        assert mgr.list_devices() == []

    def test_upsert_handles_missing_friendly_name(self):
        """Some devices come up with empty friendly_name fields.
        The CastDevice should fall back to a placeholder rather
        than producing an empty-string label in the picker."""
        mgr = CastManager()
        uuid = UUID("99999999-9999-9999-9999-999999999999")
        mgr._browser = MagicMock()
        # Empty friendly_name and model_name; the device should
        # still surface with a sensible default name.
        info = SimpleNamespace(
            uuid=uuid,
            friendly_name="",
            model_name="",
            manufacturer="",
            cast_type="audio",
            host="192.168.1.99",
            port=8009,
        )
        mgr._browser.devices = {uuid: info}
        mgr._upsert(uuid)
        devices = mgr.list_devices()
        assert len(devices) == 1
        assert devices[0].friendly_name == "Cast device"


# ---------------------------------------------------------------------
# Listener bus
# ---------------------------------------------------------------------


class TestListenerBus:
    def test_add_listener_returns_unsub(self):
        mgr = CastManager()
        events: list = []

        unsub = mgr.add_listener(lambda d: events.append(d))
        mgr._notify_listeners(None)
        assert events == [None]

        unsub()
        mgr._notify_listeners(None)
        # After unsub, no new events.
        assert events == [None]

    def test_listener_failure_doesnt_kill_others(self):
        """A buggy subscriber raising shouldn't stop the rest of
        the bus from firing. The cast notification path runs from
        connect/disconnect, which the user just clicked — failing
        silently to one listener is fine, but failing visibly via
        a stack-trace through the whole notify loop would be a
        bug."""
        mgr = CastManager()
        called = []
        mgr.add_listener(lambda d: (_ for _ in ()).throw(RuntimeError("boom")))
        mgr.add_listener(lambda d: called.append(d))
        mgr._notify_listeners(None)
        assert called == [None]


# ---------------------------------------------------------------------
# is_active hot path
# ---------------------------------------------------------------------


class TestIsActive:
    def test_false_when_no_session(self):
        """The audio callback hits this on every frame. It must
        return False cheaply when no session exists — the common
        case for users not casting."""
        mgr = CastManager()
        assert mgr.is_active() is False

    def test_true_when_session_present(self):
        from app.audio.cast import _SessionState

        mgr = CastManager()
        mgr._session = _SessionState(
            device=_device("xx"),
            cast=MagicMock(),
        )
        assert mgr.is_active() is True


# ---------------------------------------------------------------------
# get_device
# ---------------------------------------------------------------------


class TestGetDevice:
    def test_known_device(self):
        mgr = CastManager()
        d = _device("known-id")
        mgr._devices[d.id] = d
        assert mgr.get_device("known-id") is d

    def test_unknown_device_returns_none(self):
        mgr = CastManager()
        assert mgr.get_device("does-not-exist") is None
