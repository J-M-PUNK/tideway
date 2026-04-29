"""Wire-level tests for /api/cast/* endpoints.

These exercise the FastAPI handlers via TestClient with the
underlying CastManager state controlled directly. We don't reach
for a real Cast device or pychromecast handshake — those are not
deterministic in CI and aren't what these tests are for. The
contract this file pins is: handlers translate manager state into
the documented JSON shape, route errors to the documented HTTP
status codes, and respect the local-access guard.

Same fixture pattern as `test_settings_endpoint.py` — offline mode
flips on so `_require_local_access` lets the test client through,
and the on-disk settings are redirected to a tmp file.
"""
from __future__ import annotations

import copy
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.audio.cast import CastDevice


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.settings as _settings_mod
    import server

    monkeypatch.setattr(_settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    original_settings = copy.deepcopy(server.settings)
    server.settings.offline_mode = True
    server.downloader.settings = server.settings
    with TestClient(server.app) as c:
        yield c
    server.settings = original_settings
    server.downloader.settings = original_settings


@pytest.fixture
def manager(monkeypatch):
    """A fresh `CastManager` with no discovery running, replacing
    the module-level singleton for the duration of the test. Tests
    set fields on it directly to control what the endpoints see."""
    from app.audio import cast as _cast_mod

    fresh = _cast_mod.CastManager()
    monkeypatch.setattr(_cast_mod, "cast_manager", fresh)
    return fresh


def _device(uuid: str = "11111111-1111-1111-1111-111111111111") -> CastDevice:
    return CastDevice(
        id=uuid,
        friendly_name="Living Room speaker",
        model_name="Nest Mini",
        manufacturer="Google Inc.",
        cast_type="audio",
        host="192.168.1.42",
        port=8009,
    )


# ---------------------------------------------------------------------
# GET /api/cast/devices
# ---------------------------------------------------------------------


class TestCastDevicesEndpoint:
    def test_empty_state(self, client, manager):
        """No discovery, no devices — the endpoint still returns
        a well-formed payload. The frontend reads `status.available`
        and `status.running` to decide whether to render the
        Chromecast section, so an empty dict here is wrong."""
        res = client.get("/api/cast/devices")
        assert res.status_code == 200
        body = res.json()
        assert "status" in body
        assert "devices" in body
        assert body["devices"] == []
        assert body["status"]["device_count"] == 0
        assert body["status"]["connected_id"] is None

    def test_returns_discovered_devices(self, client, manager):
        """Devices the manager knows about appear in the response,
        translated to the documented JSON shape (NOT the internal
        CastDevice — host / port shouldn't leak to the frontend
        because they're a debug detail)."""
        d = _device()
        manager._devices[d.id] = d
        res = client.get("/api/cast/devices")
        assert res.status_code == 200
        body = res.json()
        assert len(body["devices"]) == 1
        item = body["devices"][0]
        assert item["id"] == d.id
        assert item["friendly_name"] == "Living Room speaker"
        assert item["model_name"] == "Nest Mini"
        assert item["cast_type"] == "audio"
        # Internal-only fields stay out of the wire shape.
        assert "host" not in item
        assert "port" not in item

    def test_status_includes_connected_state(self, client, manager):
        """When a session is active, `status.connected_id` and
        `status.connected_name` reflect it. The frontend uses these
        to put the radio mark on the right item and to show the
        casting indicator on the trigger icon."""
        from app.audio.cast import _SessionState

        d = _device()
        manager._devices[d.id] = d
        manager._session = _SessionState(device=d, cast=MagicMock())
        res = client.get("/api/cast/devices")
        body = res.json()
        assert body["status"]["connected_id"] == d.id
        assert body["status"]["connected_name"] == "Living Room speaker"


# ---------------------------------------------------------------------
# POST /api/cast/connect
# ---------------------------------------------------------------------


class TestCastConnectEndpoint:
    def test_success(self, client, manager, monkeypatch):
        """Happy path: the manager's connect() returns a CastDevice,
        the endpoint translates to a 200 with the device summary."""
        d = _device()
        manager._devices[d.id] = d
        monkeypatch.setattr(manager, "connect", lambda did: d)
        res = client.post(
            "/api/cast/connect", json={"device_id": d.id}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["device"]["id"] == d.id
        assert body["device"]["friendly_name"] == "Living Room speaker"
        assert body["device"]["cast_type"] == "audio"

    def test_unknown_device_returns_404(self, client, manager, monkeypatch):
        """If the manager raises ValueError (device id not in the
        discovery cache), the endpoint maps to 404 — the picker is
        showing stale data, frontend can re-poll and surface
        'device went offline' to the user."""
        def _raise(_did):
            raise ValueError("unknown cast device: bogus-id")

        monkeypatch.setattr(manager, "connect", _raise)
        res = client.post(
            "/api/cast/connect", json={"device_id": "bogus-id"}
        )
        assert res.status_code == 404
        assert "unknown" in res.json()["detail"].lower()

    def test_handshake_failure_returns_502(self, client, manager, monkeypatch):
        """Manager raises RuntimeError for connect timeouts, http
        bind failures, play_media rejections. All route to 502 so
        the frontend toast distinguishes 'server-side problem' from
        'device gone'."""
        def _raise(_did):
            raise RuntimeError("timed out waiting for device to be ready")

        monkeypatch.setattr(manager, "connect", _raise)
        res = client.post(
            "/api/cast/connect", json={"device_id": "any-id"}
        )
        assert res.status_code == 502
        assert "timed out" in res.json()["detail"].lower()

    def test_missing_device_id_is_validation_error(self, client, manager):
        """Malformed body — no `device_id` field — should produce
        FastAPI's standard 422 validation response, not pass into
        the manager and trip on a None id."""
        res = client.post("/api/cast/connect", json={})
        assert res.status_code == 422


# ---------------------------------------------------------------------
# POST /api/cast/disconnect
# ---------------------------------------------------------------------


class TestCastDisconnectEndpoint:
    def test_disconnect_calls_manager(self, client, manager, monkeypatch):
        called = {"count": 0}

        def _disconnect():
            called["count"] += 1

        monkeypatch.setattr(manager, "disconnect", _disconnect)
        res = client.post("/api/cast/disconnect")
        assert res.status_code == 200
        assert res.json() == {"ok": True}
        assert called["count"] == 1

    def test_disconnect_idempotent(self, client, manager, monkeypatch):
        """Calling disconnect twice is fine. The frontend may
        double-fire on rapid clicks (no debounce); the endpoint
        must not error out the second time."""
        monkeypatch.setattr(manager, "disconnect", lambda: None)
        res1 = client.post("/api/cast/disconnect")
        res2 = client.post("/api/cast/disconnect")
        assert res1.status_code == 200
        assert res2.status_code == 200
