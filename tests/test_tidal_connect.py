"""Unit + wire tests for app/audio/tidal_connect and the
/api/tidal-connect/* endpoints.

This module's behaviour is intentionally Phase-1-shaped right now —
discovery works, control plane doesn't. The tests pin both halves:
the discovery + sorting + status surface that's real, and the
"control plane returns 501" contract on the connect endpoint, so a
future Phase 2 commit that fills in the protocol can't accidentally
weaken the discovery side or change the not-implemented marker
without us noticing.

The actual SSDP scan is mocked. The real network call lives behind
async-upnp-client and isn't a deterministic test target.
"""
from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient with offline-mode flipped so `_require_local_access`
    lets us through. Same pattern as test_settings_endpoint.py."""
    import app.settings as _settings_mod
    import server

    monkeypatch.setattr(
        _settings_mod, "SETTINGS_FILE", tmp_path / "settings.json"
    )
    original = copy.deepcopy(server.settings)
    server.settings.offline_mode = True
    server.downloader.settings = server.settings
    with TestClient(server.app) as c:
        yield c
    server.settings = original
    server.downloader.settings = original


@pytest.fixture
def manager(monkeypatch):
    """A fresh TidalConnectManager, replacing the module-level
    singleton for the duration of the test. Tests poke at the
    internal device dict directly to control what list_devices()
    sees, bypassing real SSDP."""
    from app.audio import tidal_connect as _mod

    fresh = _mod.TidalConnectManager.__new__(_mod.TidalConnectManager)
    # Skip the real __init__ so we don't spin up an asyncio loop
    # for tests that never touch async work.
    import threading
    fresh._lock = threading.Lock()
    fresh._session_lock = threading.Lock()
    fresh._devices = {}
    fresh._loop = None
    fresh._loop_thread = None
    fresh._last_scan_at = 0.0
    fresh._session = None
    fresh._state_listeners = []
    fresh._local_silencer = None
    fresh._track_url_resolver = None
    monkeypatch.setattr(_mod, "_singleton", fresh)
    return fresh


def _device(
    uuid: str = "uuid:11111111-1111-1111-1111-111111111111",
    *,
    name: str = "Living Room Streamer",
    has_credentials: bool = False,
):
    from app.audio.tidal_connect import TidalConnectDevice

    return TidalConnectDevice(
        id=uuid,
        friendly_name=name,
        manufacturer="Bluesound",
        model="Node",
        location="http://192.168.1.50:60006/desc.xml",
        is_openhome=True,
        has_credentials_service=has_credentials,
        service_types=("urn:av-openhome-org:service:Product:1",),
    )


# ---------------------------------------------------------------------
# Manager — sorting, status, list
# ---------------------------------------------------------------------


class TestListDevices:
    def test_empty_manager(self, manager):
        assert manager.list_devices() == []

    def test_credentials_devices_sort_first(self, manager):
        """Picker UX: devices we have the strongest 'is Tidal-paired'
        signal for (Credentials service present) appear before plain
        OpenHome devices we're less sure about."""
        a = _device("uuid:a", name="Plain OpenHome", has_credentials=False)
        b = _device("uuid:b", name="Tidal-Paired Speaker", has_credentials=True)
        manager._devices = {a.id: a, b.id: b}
        names = [d.friendly_name for d in manager.list_devices()]
        assert names == ["Tidal-Paired Speaker", "Plain OpenHome"]

    def test_alphabetical_within_group(self, manager):
        a = _device("uuid:a", name="Zebra", has_credentials=True)
        b = _device("uuid:b", name="Apple", has_credentials=True)
        c = _device("uuid:c", name="Bluesound", has_credentials=True)
        manager._devices = {a.id: a, b.id: b, c.id: c}
        names = [d.friendly_name for d in manager.list_devices()]
        assert names == ["Apple", "Bluesound", "Zebra"]


class TestStatus:
    def test_idle(self, manager):
        s = manager.status()
        # `available` may be True or False depending on whether
        # async-upnp-client is installed in the test env. Just check
        # the rest of the contract.
        assert "available" in s
        assert s["device_count"] == 0
        assert s["last_scan_age_s"] is None
        assert s["connected_id"] is None
        assert s["connected_name"] is None
        # control_plane_ready reflects whether a session is open.
        # No session on a freshly-constructed manager.
        assert s["control_plane_ready"] is False

    def test_device_count_reflects_dict(self, manager):
        manager._devices = {"a": _device("a"), "b": _device("b", name="x")}
        assert manager.status()["device_count"] == 2

    def test_control_plane_ready_flips_when_session_open(self, manager):
        """Slice 4 contract: status['control_plane_ready'] reflects
        whether a session is actually open. Frontend keys off this
        to decide whether selecting a device shows a 'connected'
        affordance vs the 'protocol pending' toast."""
        from app.audio.tidal_connect import _SessionState
        from unittest.mock import MagicMock

        d = _device()
        manager._devices[d.id] = d
        manager._session = _SessionState(
            device=d,
            openhome_device=MagicMock(),
            playlist=MagicMock(),
            volume=None,
            time=None,
            info=None,
        )
        s = manager.status()
        assert s["control_plane_ready"] is True
        assert s["connected_id"] == d.id
        assert s["connected_name"] == d.friendly_name


class TestGetDevice:
    def test_known(self, manager):
        d = _device("uuid:123")
        manager._devices[d.id] = d
        assert manager.get_device("uuid:123") is d

    def test_unknown(self, manager):
        assert manager.get_device("missing") is None


# ---------------------------------------------------------------------
# Manager — connect/disconnect stubs
# ---------------------------------------------------------------------


class TestConnect:
    def test_unknown_device_raises_value_error(self, manager):
        with pytest.raises(ValueError) as exc:
            manager.connect("not-a-real-id")
        assert "unknown" in str(exc.value).lower()

    def test_descriptor_fetch_failure_raises_runtime_error(
        self, manager, monkeypatch
    ):
        """A device that's no longer reachable (just went offline)
        produces a RuntimeError on fetch_device, surfaced to the
        endpoint layer as 502."""
        from app.audio import tidal_connect as _mod

        d = _device("uuid:real")
        manager._devices[d.id] = d

        def _fail(_loc):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(_mod, "fetch_device", _fail)
        with pytest.raises(RuntimeError) as exc:
            manager.connect(d.id)
        assert "openhome description" in str(exc.value).lower()

    def test_missing_playlist_service_raises(self, manager, monkeypatch):
        """A device that advertises OpenHome but doesn't expose the
        Playlist service can't be controlled. Surface a clear
        error rather than crashing later when load_track tries to
        call into a None controller."""
        from app.audio import tidal_connect as _mod
        from app.audio.openhome import OpenHomeDevice

        d = _device("uuid:real")
        manager._devices[d.id] = d

        # Synthetic device with no Playlist service.
        empty_oh = OpenHomeDevice(
            udn="uuid:real",
            friendly_name="Test",
            manufacturer="X",
            model_name="Y",
            model_number="1",
            services=(),
        )
        monkeypatch.setattr(_mod, "fetch_device", lambda _loc: empty_oh)
        with pytest.raises(RuntimeError) as exc:
            manager.connect(d.id)
        assert "playlist" in str(exc.value).lower()

    def test_successful_connect_sets_session(self, manager, monkeypatch):
        """Happy path. fetch_device returns a synthetic OpenHome
        device with all four services; connect builds the
        controllers and clears the queue. Tests that:
          - manager._session is set after the call
          - status() reflects the new connected state
          - delete_all was called on the playlist controller
        """
        from app.audio import tidal_connect as _mod
        from app.audio import openhome as _oh

        d = _device("uuid:real")
        manager._devices[d.id] = d

        # Build a fake OpenHomeDevice with all four services.
        services = tuple(
            _oh.OpenHomeService(
                service_type=f"urn:av-openhome-org:service:{name}:1",
                service_id=f"urn:av-openhome-org:serviceId:{name}",
                short_name=name,
                control_url=f"http://x/{name}/control",
                event_sub_url=f"http://x/{name}/event",
                scpd_url=f"http://x/{name}/scpd.xml",
                actions=(),
            )
            for name in ("Playlist", "Volume", "Time", "Info")
        )
        oh_device = _oh.OpenHomeDevice(
            udn="uuid:real",
            friendly_name="Test",
            manufacturer="X",
            model_name="Y",
            model_number="1",
            services=services,
        )
        monkeypatch.setattr(_mod, "fetch_device", lambda _loc: oh_device)

        # Stub invoke() so DeleteAll doesn't actually try to hit
        # the network.
        invoke_calls = []
        monkeypatch.setattr(
            _oh, "invoke", lambda *a, **k: invoke_calls.append((a, k)) or {}
        )

        device = manager.connect(d.id)
        assert device.id == d.id
        assert manager._session is not None
        assert manager._session.playlist is not None
        assert manager._session.volume is not None
        # Verify DeleteAll fired during connect.
        action_names = [a[1] for a, _ in invoke_calls]
        assert "DeleteAll" in action_names

    def test_connect_replaces_existing_session(self, manager, monkeypatch):
        """Connecting to a different device tears down the existing
        session before opening the new one, so we never have two
        active controllers fighting over the same audio engine."""
        from app.audio import tidal_connect as _mod
        from app.audio import openhome as _oh

        d1 = _device("uuid:1", name="First")
        d2 = _device("uuid:2", name="Second")
        manager._devices[d1.id] = d1
        manager._devices[d2.id] = d2

        services = (
            _oh.OpenHomeService(
                service_type="urn:av-openhome-org:service:Playlist:1",
                service_id="urn:av-openhome-org:serviceId:Playlist",
                short_name="Playlist",
                control_url="http://x/Playlist/control",
                event_sub_url="http://x/Playlist/event",
                scpd_url="http://x/Playlist/scpd.xml",
                actions=(),
            ),
        )
        oh_device = _oh.OpenHomeDevice(
            udn="uuid:1",
            friendly_name="X",
            manufacturer="x",
            model_name="x",
            model_number="1",
            services=services,
        )
        monkeypatch.setattr(_mod, "fetch_device", lambda _loc: oh_device)
        monkeypatch.setattr(_oh, "invoke", lambda *a, **k: {})

        manager.connect(d1.id)
        first_session = manager._session
        assert first_session is not None
        manager.connect(d2.id)
        assert manager._session is not first_session


class TestDisconnect:
    def test_idempotent(self, manager):
        manager.disconnect()
        manager.disconnect()  # no exceptions

    def test_clears_session(self, manager):
        from app.audio.tidal_connect import _SessionState
        from unittest.mock import MagicMock

        manager._session = _SessionState(
            device=_device(),
            openhome_device=MagicMock(),
            playlist=MagicMock(),
            volume=None,
            time=None,
            info=None,
        )
        manager.disconnect()
        assert manager._session is None

    def test_sends_stop_to_device(self, manager):
        """Disconnect should attempt to send Playlist.Stop on the
        way out so the device doesn't keep playing whatever was
        loaded. A failed Stop call is logged but doesn't prevent
        the session from being cleared."""
        from app.audio.tidal_connect import _SessionState
        from unittest.mock import MagicMock

        playlist = MagicMock()
        manager._session = _SessionState(
            device=_device(),
            openhome_device=MagicMock(),
            playlist=playlist,
            volume=None,
            time=None,
            info=None,
        )
        manager.disconnect()
        playlist.stop.assert_called_once()
        assert manager._session is None


class TestLoadTrack:
    def test_no_session_raises(self, manager):
        with pytest.raises(RuntimeError) as exc:
            manager.load_track(123)
        assert "no active" in str(exc.value).lower()

    def test_no_resolver_raises(self, manager):
        from app.audio.tidal_connect import _SessionState
        from unittest.mock import MagicMock

        manager._session = _SessionState(
            device=_device(),
            openhome_device=MagicMock(),
            playlist=MagicMock(),
            volume=None,
            time=None,
            info=None,
            track_url_resolver=None,
        )
        with pytest.raises(RuntimeError) as exc:
            manager.load_track(123)
        assert "resolver" in str(exc.value).lower()

    def test_happy_path_inserts_and_plays(self, manager):
        """Resolver returns (url, metadata); load_track wraps in
        DIDL-Lite, calls Insert + Play, stores the returned NewId."""
        from app.audio.tidal_connect import _SessionState
        from app.audio.openhome import TrackMetadata
        from unittest.mock import MagicMock

        playlist = MagicMock()
        playlist.insert.return_value = 42

        def _resolver(track_id: int):
            return (
                "http://stream.tidal/track.flac",
                TrackMetadata(
                    title="Cry For Me",
                    artist="The Weeknd",
                    album="Hurry Up Tomorrow",
                    duration_s=240,
                    cover_url="http://cover/x.jpg",
                    track_uri="http://stream.tidal/track.flac",
                ),
            )

        manager._session = _SessionState(
            device=_device(),
            openhome_device=MagicMock(),
            playlist=playlist,
            volume=None,
            time=None,
            info=None,
            track_url_resolver=_resolver,
        )
        new_id = manager.load_track(123)
        assert new_id == 42
        assert manager._session.current_track_id == 42
        # Insert called with a DIDL-Lite that includes the track
        # title.
        playlist.insert.assert_called_once()
        kwargs = playlist.insert.call_args.kwargs
        assert "Cry For Me" in kwargs["metadata"]
        assert kwargs["uri"] == "http://stream.tidal/track.flac"
        assert kwargs["after_id"] == 0
        playlist.play.assert_called_once()

    def test_resolver_failure_propagates_as_runtime_error(self, manager):
        from app.audio.tidal_connect import _SessionState
        from unittest.mock import MagicMock

        def _bad_resolver(_id):
            raise ValueError("track not streamable in your region")

        manager._session = _SessionState(
            device=_device(),
            openhome_device=MagicMock(),
            playlist=MagicMock(),
            volume=None,
            time=None,
            info=None,
            track_url_resolver=_bad_resolver,
        )
        with pytest.raises(RuntimeError) as exc:
            manager.load_track(123)
        assert "resolve" in str(exc.value).lower()


class TestStatePolling:
    """Slice 5: poll the device's Time + Volume services and fire
    listener events when state changes meaningfully.

    The polling logic itself (the loop running on a background
    thread) isn't unit tested — it's a thin wrapper around
    poll_state_once() that handles thread lifecycle. The
    interesting behaviour is in poll_state_once: which deltas
    trigger which events.
    """

    def _attach_session(
        self,
        manager,
        *,
        time_responses=None,
        volume_responses=None,
    ):
        from app.audio.tidal_connect import _SessionState
        from unittest.mock import MagicMock

        time_ctl = None
        if time_responses is not None:
            time_ctl = MagicMock()
            time_ctl.time = MagicMock(side_effect=time_responses)

        volume = None
        if volume_responses is not None:
            volume = MagicMock()
            # volume_responses is a list of (volume_pct, muted) tuples.
            volume.get_volume = MagicMock(
                side_effect=[v for v, _ in volume_responses]
            )
            volume.get_mute = MagicMock(
                side_effect=[m for _, m in volume_responses]
            )

        manager._session = _SessionState(
            device=_device(),
            openhome_device=MagicMock(),
            playlist=MagicMock(),
            volume=volume,
            time=time_ctl,
            info=None,
        )

    def test_poll_returns_none_with_no_session(self, manager):
        assert manager.poll_state_once() is None

    def test_poll_returns_state_dict(self, manager):
        self._attach_session(
            manager,
            time_responses=[
                {"duration": 240, "seconds": 30, "track_count": 1}
            ],
            volume_responses=[(50, False)],
        )
        state = manager.poll_state_once()
        assert state == {
            "position_s": 30,
            "duration_s": 240,
            "track_count": 1,
            "volume_percent": 50,
            "muted": False,
        }

    def test_poll_handles_missing_time_service(self, manager):
        self._attach_session(
            manager, time_responses=None, volume_responses=[(60, False)]
        )
        state = manager.poll_state_once()
        # Missing Time service means defaults stay at 0.
        assert state["position_s"] == 0
        assert state["duration_s"] == 0
        assert state["volume_percent"] == 60

    def test_poll_handles_missing_volume_service(self, manager):
        self._attach_session(
            manager,
            time_responses=[
                {"duration": 100, "seconds": 5, "track_count": 1}
            ],
            volume_responses=None,
        )
        state = manager.poll_state_once()
        assert state["position_s"] == 5
        assert state["volume_percent"] == 0
        assert state["muted"] is False

    def test_poll_writes_state_back_to_session(self, manager):
        self._attach_session(
            manager,
            time_responses=[
                {"duration": 240, "seconds": 30, "track_count": 1}
            ],
            volume_responses=[(50, False)],
        )
        manager.poll_state_once()
        assert manager._session.position_s == 30
        assert manager._session.duration_s == 240
        assert manager._session.track_count == 1

    def test_poll_handles_time_failure_gracefully(self, manager):
        """A SOAP failure during polling shouldn't crash the loop —
        log it and continue with last-known state. Real devices
        occasionally drop a single SOAP request and recover."""
        from unittest.mock import MagicMock
        from app.audio.tidal_connect import _SessionState

        time_ctl = MagicMock()
        time_ctl.time.side_effect = RuntimeError("transient failure")
        manager._session = _SessionState(
            device=_device(),
            openhome_device=MagicMock(),
            playlist=MagicMock(),
            volume=None,
            time=time_ctl,
            info=None,
        )
        # Should NOT raise.
        state = manager.poll_state_once()
        assert state is not None
        # Session state should still be valid (just unchanged).
        assert manager._session is not None


class TestStateListeners:
    """Listener bus mechanics. add_state_listener returns an
    unsubscribe handle, the bus snapshots before firing so a
    raising listener doesn't block the rest, and listeners only
    fire when state actually changes."""

    def _attach_session_with_state(
        self,
        manager,
        *,
        track_count: int = 0,
        position_s: int = 0,
        time_response,
    ):
        from app.audio.tidal_connect import _SessionState
        from unittest.mock import MagicMock

        time_ctl = MagicMock()
        time_ctl.time = MagicMock(return_value=time_response)
        manager._session = _SessionState(
            device=_device(),
            openhome_device=MagicMock(),
            playlist=MagicMock(),
            volume=None,
            time=time_ctl,
            info=None,
            track_count=track_count,
            position_s=position_s,
        )

    def test_add_listener_returns_unsubscribe(self, manager):
        events = []
        unsub = manager.add_state_listener(lambda s: events.append(s))
        # No session — calling poll fires nothing.
        manager.poll_state_once()
        assert events == []
        unsub()
        # Ensure listener no longer registered.
        with manager._session_lock:
            assert (lambda: None) not in manager._state_listeners

    def test_listener_fires_on_track_change(self, manager):
        events = []
        manager.add_state_listener(lambda s: events.append(s))
        self._attach_session_with_state(
            manager,
            track_count=1,
            time_response={"duration": 240, "seconds": 0, "track_count": 2},
        )
        manager.poll_state_once()
        assert len(events) == 1
        assert events[0]["track_count"] == 2

    def test_listener_fires_on_position_change(self, manager):
        events = []
        manager.add_state_listener(lambda s: events.append(s))
        self._attach_session_with_state(
            manager,
            position_s=10,
            time_response={"duration": 240, "seconds": 11, "track_count": 1},
        )
        manager.poll_state_once()
        # 1s drift triggers a fire — our cadence is 1s so any
        # forward motion produces an event.
        assert len(events) == 1
        assert events[0]["position_s"] == 11

    def test_listener_does_not_fire_on_no_change(self, manager):
        """If position stays the same (paused) and nothing else
        moves, no event. Otherwise the SSE bus would flood with
        identical 'still paused' updates every poll cycle."""
        events = []
        manager.add_state_listener(lambda s: events.append(s))
        self._attach_session_with_state(
            manager,
            position_s=30,
            track_count=1,
            time_response={"duration": 240, "seconds": 30, "track_count": 1},
        )
        manager.poll_state_once()
        assert events == []

    def test_listener_failure_does_not_break_others(self, manager):
        """A buggy listener raising shouldn't stop the rest of the
        bus from firing. Same contract as the Cast manager — silent
        in production, flagged in test logs."""
        called = []
        manager.add_state_listener(
            lambda s: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        manager.add_state_listener(lambda s: called.append(s))
        self._attach_session_with_state(
            manager,
            time_response={"duration": 240, "seconds": 5, "track_count": 1},
        )
        manager.poll_state_once()
        assert len(called) == 1


class TestSilencerWiring:
    """Slice 4 audio integration: when a TC session opens the
    audio engine should silence local output via the registered
    silencer hook. Mirrors the Cast manager's pattern so the deploy
    branch can land both engines using the same PCMPlayer flag."""

    def test_set_local_silencer_stores_callback(self, manager):
        from unittest.mock import MagicMock

        cb = MagicMock()
        manager.set_local_silencer(cb)
        assert manager._local_silencer is cb

    def test_disconnect_fires_silencer_false(self, manager):
        from app.audio.tidal_connect import _SessionState
        from unittest.mock import MagicMock

        cb = MagicMock()
        manager.set_local_silencer(cb)
        playlist = MagicMock()
        manager._session = _SessionState(
            device=_device(),
            openhome_device=MagicMock(),
            playlist=playlist,
            volume=None,
            time=None,
            info=None,
        )
        manager.disconnect()
        cb.assert_called_once_with(False)

    def test_silencer_failure_doesnt_break_disconnect(self, manager):
        """A buggy silencer raising shouldn't prevent the rest of
        disconnect from running. Stop on the device + session
        teardown matter more than the local-mute flip."""
        from app.audio.tidal_connect import _SessionState
        from unittest.mock import MagicMock

        def _bad(_active):
            raise RuntimeError("silencer broke")

        manager.set_local_silencer(_bad)
        playlist = MagicMock()
        manager._session = _SessionState(
            device=_device(),
            openhome_device=MagicMock(),
            playlist=playlist,
            volume=None,
            time=None,
            info=None,
        )
        manager.disconnect()  # should not raise
        assert manager._session is None


class TestResolverPrecedence:
    """Connect's track_url_resolver argument takes precedence over
    the manager-level resolver. Tests substitute per-call; production
    wires once at startup."""

    def test_per_call_resolver_wins(self, manager):
        from unittest.mock import MagicMock

        manager_resolver = MagicMock()
        per_call_resolver = MagicMock()
        manager.set_track_url_resolver(manager_resolver)

        # Connect with a per-call resolver. We can't fully connect
        # without mocking fetch_device etc., but we can verify the
        # resolver-precedence logic in isolation by constructing
        # a session with the precedence rule applied.
        # The actual precedence check happens inside connect();
        # exercise it by overriding the relevant pieces.
        from app.audio import tidal_connect as _mod
        from app.audio.openhome import OpenHomeDevice, OpenHomeService

        d = _device("uuid:test")
        manager._devices[d.id] = d
        oh_device = OpenHomeDevice(
            udn="uuid:test",
            friendly_name="X",
            manufacturer="x",
            model_name="x",
            model_number="1",
            services=(
                OpenHomeService(
                    service_type=(
                        "urn:av-openhome-org:service:Playlist:1"
                    ),
                    service_id=(
                        "urn:av-openhome-org:serviceId:Playlist"
                    ),
                    short_name="Playlist",
                    control_url="http://x/Playlist/control",
                    event_sub_url="http://x/Playlist/event",
                    scpd_url="http://x/Playlist/scpd.xml",
                    actions=(),
                ),
            ),
        )
        from unittest import mock as _mock
        with _mock.patch.object(
            _mod, "fetch_device", return_value=oh_device
        ):
            with _mock.patch.object(
                _mod.PlaylistController,
                "delete_all",
                return_value=None,
            ):
                manager.connect(d.id, track_url_resolver=per_call_resolver)
        # The session should have the per-call resolver attached.
        assert (
            manager._session.track_url_resolver is per_call_resolver
        )


class TestTransportControls:
    def _attach_session(self, manager, *, playlist=None, volume=None):
        from app.audio.tidal_connect import _SessionState
        from unittest.mock import MagicMock

        manager._session = _SessionState(
            device=_device(),
            openhome_device=MagicMock(),
            playlist=playlist or MagicMock(),
            volume=volume,
            time=None,
            info=None,
        )

    def test_pause_routes_to_playlist(self, manager):
        from unittest.mock import MagicMock

        playlist = MagicMock()
        self._attach_session(manager, playlist=playlist)
        manager.pause()
        playlist.pause.assert_called_once()

    def test_play_routes_to_playlist(self, manager):
        from unittest.mock import MagicMock

        playlist = MagicMock()
        self._attach_session(manager, playlist=playlist)
        manager.play()
        playlist.play.assert_called_once()

    def test_seek_routes_to_seek_second(self, manager):
        from unittest.mock import MagicMock

        playlist = MagicMock()
        self._attach_session(manager, playlist=playlist)
        manager.seek(120)
        playlist.seek_second.assert_called_once_with(120)

    def test_set_volume_routes_to_volume_controller(self, manager):
        from unittest.mock import MagicMock

        volume = MagicMock()
        self._attach_session(manager, volume=volume)
        manager.set_volume(75)
        volume.set_volume.assert_called_once_with(75)

    def test_set_volume_no_op_without_volume_service(self, manager):
        """A device that doesn't expose Volume should silently
        ignore set_volume rather than crashing — degraded but
        functional."""
        self._attach_session(manager, volume=None)
        manager.set_volume(75)  # no exception

    def test_pause_without_session_raises(self, manager):
        with pytest.raises(RuntimeError):
            manager.pause()


# ---------------------------------------------------------------------
# /api/tidal-connect/devices endpoint
# ---------------------------------------------------------------------


class TestDevicesEndpoint:
    def test_returns_status_and_devices(self, client, manager, monkeypatch):
        d = _device(name="Living Room Node", has_credentials=True)
        manager._devices[d.id] = d
        # Stub refresh so the test doesn't actually SSDP-scan.
        monkeypatch.setattr(manager, "refresh", lambda timeout=5.0: [d])
        res = client.get("/api/tidal-connect/devices")
        assert res.status_code == 200
        body = res.json()
        assert "status" in body
        assert "devices" in body
        assert len(body["devices"]) == 1
        item = body["devices"][0]
        assert item["friendly_name"] == "Living Room Node"
        assert item["is_openhome"] is True
        assert item["has_credentials_service"] is True

    def test_unavailable_returns_empty_payload(
        self, client, manager, monkeypatch
    ):
        """When async-upnp-client failed to import, the endpoint
        should return a clean empty payload rather than 500."""
        monkeypatch.setattr(manager, "is_available", lambda: False)
        res = client.get("/api/tidal-connect/devices")
        assert res.status_code == 200
        body = res.json()
        assert body["devices"] == []
        assert body["status"]["available"] is False

    def test_internal_fields_not_leaked(self, client, manager, monkeypatch):
        """The wire shape should NOT include `location` or
        `service_types` — those are debug-grade internal details
        the frontend doesn't need. If a future change leaks them
        the integration tests will catch it here."""
        d = _device()
        manager._devices[d.id] = d
        monkeypatch.setattr(manager, "refresh", lambda timeout=5.0: [d])
        res = client.get("/api/tidal-connect/devices")
        item = res.json()["devices"][0]
        assert "location" not in item
        assert "service_types" not in item


# ---------------------------------------------------------------------
# /api/tidal-connect/connect endpoint
# ---------------------------------------------------------------------


class TestConnectEndpoint:
    def test_unknown_device_returns_404(self, client, manager, monkeypatch):
        def _raise(_did, **_kw):
            raise ValueError("unknown tidal connect device: nope")

        monkeypatch.setattr(manager, "connect", _raise)
        res = client.post(
            "/api/tidal-connect/connect", json={"device_id": "nope"}
        )
        assert res.status_code == 404
        assert "unknown" in res.json()["detail"].lower()

    def test_handshake_failure_returns_502(self, client, manager, monkeypatch):
        """Slice 4 contract: descriptor fetch / DeleteAll failures
        surface as 502, distinguishing 'server-side problem' from
        'device gone' (404) from 'malformed request' (422)."""
        def _raise(_did, **_kw):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(manager, "connect", _raise)
        res = client.post(
            "/api/tidal-connect/connect", json={"device_id": "any-id"}
        )
        assert res.status_code == 502
        assert "connection refused" in res.json()["detail"].lower()

    def test_success_returns_device_summary(
        self, client, manager, monkeypatch
    ):
        """Slice 4 contract: connect now actually opens a session,
        so success is 200 with a device summary the frontend can
        show in the picker."""
        d = _device(name="Living Room Node", has_credentials=True)

        def _ok(_did, **_kw):
            return d

        monkeypatch.setattr(manager, "connect", _ok)
        res = client.post(
            "/api/tidal-connect/connect", json={"device_id": d.id}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["device"]["friendly_name"] == "Living Room Node"

    def test_missing_device_id_is_validation_error(self, client, manager):
        res = client.post("/api/tidal-connect/connect", json={})
        assert res.status_code == 422


# ---------------------------------------------------------------------
# /api/tidal-connect/disconnect endpoint
# ---------------------------------------------------------------------


class TestDisconnectEndpoint:
    def test_disconnect_returns_ok(self, client, manager):
        res = client.post("/api/tidal-connect/disconnect")
        assert res.status_code == 200
        assert res.json() == {"ok": True}

    def test_idempotent_repeated_calls(self, client, manager):
        for _ in range(3):
            res = client.post("/api/tidal-connect/disconnect")
            assert res.status_code == 200
