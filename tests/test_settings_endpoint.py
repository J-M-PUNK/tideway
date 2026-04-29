"""FastAPI-level tests for `PUT /api/settings`.

The allowlist-stripping bug that bit us yesterday — where the
frontend sent `exclusive_mode: true`, the PUT returned 200, but the
value was silently dropped because `SettingsPayload` didn't declare
it — is exactly the kind of regression that only a wire-level test
catches. This module tests that every documented setting survives
a round-trip through the endpoint.
"""
import copy

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient with on-disk settings redirected to a tmp file and
    the in-memory settings snapshot restored after each test so the
    real app state isn't mutated across tests or across runs."""
    import app.settings as _settings_mod
    import server

    # Send writes at a tmp file instead of the user-data settings.json.
    monkeypatch.setattr(_settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")

    # Offline mode flips on so `_require_local_access` (the guard in
    # front of the settings endpoints) doesn't reject unauthenticated
    # tests. Snapshot + restore the whole Settings instance so flipping
    # fields in a test can't leak.
    original_settings = copy.deepcopy(server.settings)
    server.settings.offline_mode = True
    server.downloader.settings = server.settings

    with TestClient(server.app) as c:
        yield c

    # Restore snapshot on the module-level `settings` reference.
    server.settings = original_settings
    server.downloader.settings = original_settings


# --- allowlist regression guards ---------------------------------------

def test_put_exclusive_mode_persists(client):
    """We shipped a bug where `exclusive_mode` was silently stripped.
    This is the direct regression guard."""
    r = client.put("/api/settings", json={"exclusive_mode": True})

    assert r.status_code == 200
    assert r.json()["exclusive_mode"] is True

    # Confirm the value survives on a subsequent GET too, not just the
    # PUT's echoed response.
    r2 = client.get("/api/settings")
    assert r2.json()["exclusive_mode"] is True


def test_put_force_volume_persists(client):
    r = client.put("/api/settings", json={"force_volume": True})
    assert r.status_code == 200
    assert r.json()["force_volume"] is True


def test_put_start_minimized_persists(client):
    r = client.put("/api/settings", json={"start_minimized": True})
    assert r.status_code == 200
    assert r.json()["start_minimized"] is True


def test_put_explicit_content_preference_persists(client):
    for value in ("clean", "both", "explicit"):
        r = client.put(
            "/api/settings", json={"explicit_content_preference": value}
        )
        assert r.status_code == 200, r.text
        assert r.json()["explicit_content_preference"] == value


def test_put_continue_playing_after_queue_ends_persists(client):
    """The queue-end auto-radio toggle must survive PUT both ways. The
    Settings dataclass + SettingsPayload mirror is the foot-gun this
    file exists to guard against, and this field is what powers the
    queue-end flow in usePlayer."""
    for value in (False, True):
        r = client.put(
            "/api/settings", json={"continue_playing_after_queue_ends": value}
        )
        assert r.status_code == 200, r.text
        assert r.json()["continue_playing_after_queue_ends"] is value


def test_continue_playing_after_queue_ends_defaults_to_true():
    """The toggle defaults on so the new install gets Spotify-style
    autoplay out of the box. Pin this to catch a refactor that flips
    the default back to False (which would silently turn off the
    feature for everyone with a fresh settings.json)."""
    from app.settings import Settings

    assert Settings().continue_playing_after_queue_ends is True


def test_put_unknown_field_is_silently_ignored(client):
    """Pydantic's default behaviour: unknown fields are dropped, not
    rejected. This matches the app's existing UX — frontend-to-backend
    mismatch during rollout shouldn't 400."""
    r = client.put(
        "/api/settings",
        json={"exclusive_mode": True, "totally_made_up_setting": "value"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["exclusive_mode"] is True
    assert "totally_made_up_setting" not in body


def test_put_concurrent_downloads_validated(client):
    """Server must clamp concurrent_downloads; a >50 or <1 value is
    rejected with 400 rather than accepted and then tripping the worker
    pool at request time."""
    r = client.put("/api/settings", json={"concurrent_downloads": 0})
    assert r.status_code == 400

    r = client.put("/api/settings", json={"concurrent_downloads": 10000})
    assert r.status_code == 400


def test_put_multiple_fields_in_one_request(client):
    """Typical settings-page autosave sends multiple fields at once.
    All must apply atomically."""
    r = client.put(
        "/api/settings",
        json={
            "exclusive_mode": True,
            "force_volume": True,
            "notify_on_track_change": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["exclusive_mode"] is True
    assert body["force_volume"] is True
    assert body["notify_on_track_change"] is True
