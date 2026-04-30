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


def test_default_concurrent_downloads_is_one():
    """Serial download is the safest baseline against Tidal's per-
    account rate-limit. A bumped default (e.g. someone setting it to
    3 for "feels faster") would silently expose every fresh install
    to the abuse threshold. Pin the conservative default so a
    change has to be a deliberate edit to this test."""
    from app.settings import Settings

    assert Settings().concurrent_downloads == 1


def test_default_download_rate_limit_is_20_mbps():
    """20 MB/s is the new default — fast enough to feel instant on a
    healthy connection, slow enough to look like steady streaming
    rather than a bulk scrape. Pin the value because it's the kind
    of setting that's tempting to "just bump" in a refactor."""
    from app.settings import Settings

    assert Settings().download_rate_limit_mbps == 20


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


# ---------------------------------------------------------------------------
# Settings <-> SettingsPayload sync guard
#
# When a new field lands on the `Settings` dataclass it has to land
# on `SettingsPayload` too (or be explicitly excluded below) — Pydantic
# drops unknown fields from PUT bodies, so a missing mirror means
# values silently never reach the dataclass-construction step. The
# allowlist-stripping bug at the top of this file came from exactly
# this gap. The three tests below catch the regression at field-add
# time so it can't reach a release.
# ---------------------------------------------------------------------------


# Fields on `Settings` that are *intentionally* not on
# `SettingsPayload` because they're managed by their own dedicated
# endpoints. Add to this set when you add a Settings field with
# its own endpoint; remove when you wire a field through
# PUT /api/settings. Each entry lists which endpoint owns the field.
_FIELDS_MANAGED_ELSEWHERE: dict[str, str] = {
    # Equalizer state — set via POST /api/player/eq + the preset
    # endpoint. The general settings PUT can't go through the
    # PCMPlayer's coefficient-rebuild path, so it stays out.
    "eq_enabled": "/api/player/eq",
    "eq_bands": "/api/player/eq",
    "eq_preamp": "/api/player/eq",
    # Output device — set via POST /api/player/output-device,
    # which has to coordinate with stream replacement.
    "audio_output_device": "/api/player/output-device",
    # Spotify client_id — set via the Spotify importer page's
    # own setup flow.
    "spotify_client_id": "/api/spotify/client-id",
}


def test_every_settings_field_is_in_payload_or_explicitly_excluded():
    """Every field on the `Settings` dataclass must either:
      - exist on `SettingsPayload` (so PUT /api/settings accepts it), or
      - appear in `_FIELDS_MANAGED_ELSEWHERE` (so it's intentionally
        excluded because it has its own endpoint).

    A field that's on neither is the foot-gun: PUTs silently drop
    it and the toggle never works.
    """
    import dataclasses
    from app.settings import Settings
    from server import SettingsPayload

    settings_fields = {f.name for f in dataclasses.fields(Settings)}
    payload_fields = set(SettingsPayload.model_fields.keys())
    excluded = set(_FIELDS_MANAGED_ELSEWHERE.keys())

    missing = settings_fields - payload_fields - excluded
    assert not missing, (
        f"Settings fields not in SettingsPayload and not excluded: "
        f"{sorted(missing)}. Either add them to SettingsPayload in "
        f"server.py or list them in _FIELDS_MANAGED_ELSEWHERE here "
        f"with the dedicated endpoint that owns them."
    )


def test_no_payload_fields_unknown_to_settings():
    """Reverse direction: every `SettingsPayload` field must exist on
    `Settings`. A typo in SettingsPayload would otherwise be silently
    accepted by the PUT — the user would see a "200 OK" but the
    field would never persist because the dataclass wouldn't know
    about it."""
    import dataclasses
    from app.settings import Settings
    from server import SettingsPayload

    settings_fields = {f.name for f in dataclasses.fields(Settings)}
    payload_fields = set(SettingsPayload.model_fields.keys())

    stale = payload_fields - settings_fields
    assert not stale, (
        f"SettingsPayload has fields that don't exist on Settings: "
        f"{sorted(stale)}. Either rename them to match Settings or "
        f"remove them from SettingsPayload."
    )


def test_excluded_fields_are_actually_on_settings():
    """Sanity check on `_FIELDS_MANAGED_ELSEWHERE` itself — if a
    field gets renamed on Settings and we forget to update this
    list, we'd be granting an exclusion to a field that no longer
    exists, masking a real sync gap. Catch that here."""
    import dataclasses
    from app.settings import Settings

    settings_fields = {f.name for f in dataclasses.fields(Settings)}
    stale = set(_FIELDS_MANAGED_ELSEWHERE.keys()) - settings_fields
    assert not stale, (
        f"_FIELDS_MANAGED_ELSEWHERE references fields that don't "
        f"exist on Settings: {sorted(stale)}. Update the exclusion "
        f"list to match the dataclass."
    )
