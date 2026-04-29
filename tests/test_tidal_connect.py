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
    fresh._devices = {}
    fresh._loop = None
    fresh._loop_thread = None
    fresh._last_scan_at = 0.0
    fresh._session_id = None
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
        assert s["control_plane_ready"] is False

    def test_device_count_reflects_dict(self, manager):
        manager._devices = {"a": _device("a"), "b": _device("b", name="x")}
        assert manager.status()["device_count"] == 2

    def test_control_plane_pinned_false(self, manager):
        """Until Phase 2 protocol work lands, status MUST report
        control_plane_ready=False so the frontend toast can warn
        users that connect won't actually play music. A regression
        here would let the picker silently appear functional when
        it isn't."""
        assert manager.status()["control_plane_ready"] is False


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


class TestConnectStub:
    def test_unknown_device_raises_value_error(self, manager):
        with pytest.raises(ValueError) as exc:
            manager.connect("not-a-real-id")
        assert "unknown" in str(exc.value).lower()

    def test_known_device_raises_not_implemented(self, manager):
        """The control plane is gated on Phase 2 protocol scoping.
        Until then, connect on a real device must produce a clear
        NotImplementedError that the endpoint maps to 501. A
        regression that silently returned would let the picker
        appear functional when no audio is being routed."""
        d = _device("uuid:real")
        manager._devices[d.id] = d
        with pytest.raises(NotImplementedError) as exc:
            manager.connect(d.id)
        # Message should reference what's actually missing so a
        # user reading the toast can self-diagnose.
        assert "control plane" in str(exc.value).lower()


class TestDisconnect:
    def test_idempotent(self, manager):
        manager.disconnect()
        manager.disconnect()  # no exceptions

    def test_clears_session_id(self, manager):
        manager._session_id = "fake-session"
        manager.disconnect()
        assert manager._session_id is None


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
        def _raise(_did):
            raise ValueError("unknown tidal connect device: nope")

        monkeypatch.setattr(manager, "connect", _raise)
        res = client.post(
            "/api/tidal-connect/connect", json={"device_id": "nope"}
        )
        assert res.status_code == 404
        assert "unknown" in res.json()["detail"].lower()

    def test_known_device_returns_501(self, client, manager, monkeypatch):
        """Phase 1 contract: the protocol layer isn't implemented
        yet, and 501 Not Implemented is what the endpoint must
        return. A frontend toast keys off this status code to show
        'Tidal Connect routing pending' rather than a generic
        500."""
        def _raise(_did):
            raise NotImplementedError(
                "Tidal Connect control plane isn't implemented yet."
            )

        monkeypatch.setattr(manager, "connect", _raise)
        res = client.post(
            "/api/tidal-connect/connect", json={"device_id": "any-id"}
        )
        assert res.status_code == 501
        assert "control plane" in res.json()["detail"].lower()

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
