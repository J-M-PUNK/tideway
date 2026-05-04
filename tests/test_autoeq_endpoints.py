"""End-to-end tests for the AutoEQ import + delete endpoints.

The parser side of imports is exhaustively tested in
test_autoeq_loader.py; this module covers the wire-level
behaviour: path-traversal rejection, conflict-on-existing,
overwrite, atomic writes, the active-profile-clearing side effect
of delete, and refusal to delete bundled profiles.
"""
from __future__ import annotations

import copy
import shutil

import pytest
from fastapi.testclient import TestClient


# A tiny but valid AutoEQ ParametricEQ.txt — one preamp + two filters.
_VALID_PEQ = """\
Preamp: -3.5 dB
Filter 1: ON PK Fc 200 Hz Gain -3.0 dB Q 1.4
Filter 2: ON LSC Fc 105 Hz Gain 6.0 dB Q 0.7
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient with the AutoEQ cache redirected to a tmp dir so
    these tests don't pollute the user's actual cache, and the in-
    memory settings snapshot restored after each test so an active-
    profile change can't leak across runs."""
    import app.settings as _settings_mod
    import server
    from app.audio.autoeq import updater
    from app.audio.autoeq.index import INDEX

    monkeypatch.setattr(_settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")

    cache_root = tmp_path / "autoeq_cache" / "results"
    monkeypatch.setattr(updater, "cache_dir", lambda: cache_root)

    original_settings = copy.deepcopy(server.settings)
    server.settings.offline_mode = True
    server.downloader.settings = server.settings

    # Reload INDEX against an empty roots list so each test starts
    # with no profiles; the tests that want a profile import one
    # explicitly.
    INDEX.load_directories([cache_root])

    with TestClient(server.app) as c:
        yield c

    server.settings = original_settings
    server.downloader.settings = original_settings


def test_import_round_trips_to_disk_and_index(client, tmp_path):
    """Happy path. POST /api/eq/import-profile with valid PEQ
    content lands a file at the expected cache path AND surfaces
    the new profile via /api/eq/profiles, all without an app
    restart."""
    r = client.post(
        "/api/eq/import-profile",
        json={"headphone_name": "Sennheiser HD 600", "content": _VALID_PEQ},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["profile_id"] == "User imported/Sennheiser HD 600"

    # File should exist on disk under the User imported/ namespace.
    expected = (
        tmp_path
        / "autoeq_cache"
        / "results"
        / "User imported"
        / "Sennheiser HD 600"
        / "Sennheiser HD 600 ParametricEQ.txt"
    )
    assert expected.exists()

    # Index reload picked it up.
    listing = client.get("/api/eq/profiles").json()
    ids = [p["id"] for p in listing["profiles"]]
    assert "User imported/Sennheiser HD 600" in ids


def test_import_rejects_invalid_peq(client):
    """A malformed PEQ file should return 400 with a parser error
    that names the bad line, not 200-but-empty."""
    r = client.post(
        "/api/eq/import-profile",
        json={
            "headphone_name": "Bad Profile",
            "content": "this is not a valid line",
        },
    )
    assert r.status_code == 400
    assert "unrecognised line" in r.json()["detail"]


def test_import_rejects_path_traversal(client):
    """A headphone name containing slashes / .. / null bytes would
    let an attacker write outside the cache dir. Reject at the
    name-validation layer before any disk write."""
    for bad_name in ["../etc/passwd", "foo/bar", "foo\\bar", "foo\x00bar"]:
        r = client.post(
            "/api/eq/import-profile",
            json={"headphone_name": bad_name, "content": _VALID_PEQ},
        )
        assert r.status_code == 400, f"name={bad_name!r} got {r.status_code}"
        assert (
            "slashes" in r.json()["detail"] or "null" in r.json()["detail"]
        )


def test_import_409_on_existing_then_overwrite_works(client):
    """A second import with the same name returns 409. Same name
    + overwrite=true succeeds and replaces the file."""
    payload = {"headphone_name": "Test HP", "content": _VALID_PEQ}
    r1 = client.post("/api/eq/import-profile", json=payload)
    assert r1.status_code == 200

    r2 = client.post("/api/eq/import-profile", json=payload)
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"]

    payload["overwrite"] = True
    payload["content"] = "Preamp: -1.0 dB\nFilter 1: ON PK Fc 500 Hz Gain 1.0 dB Q 1\n"
    r3 = client.post("/api/eq/import-profile", json=payload)
    assert r3.status_code == 200

    # The newly-written content should be reflected in the index.
    detail = client.get("/api/eq/profiles/User%20imported/Test%20HP").json()
    assert detail["preamp_db"] == -1.0


def test_delete_imported_profile(client):
    """Importing then deleting should leave no trace in the index
    or on disk."""
    client.post(
        "/api/eq/import-profile",
        json={"headphone_name": "Throwaway", "content": _VALID_PEQ},
    )

    r = client.post(
        "/api/eq/delete-profile",
        json={"profile_id": "User imported/Throwaway"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["cleared_active"] is False  # wasn't active

    listing = client.get("/api/eq/profiles").json()
    ids = [p["id"] for p in listing["profiles"]]
    assert "User imported/Throwaway" not in ids


def test_delete_clears_active_when_target_was_active(client):
    """If the user deletes the profile that's currently loaded,
    the server clears `eq_active_profile_id` so the player stops
    referencing a stale id and the next bootstrap doesn't try to
    apply something that no longer exists."""
    import server

    client.post(
        "/api/eq/import-profile",
        json={"headphone_name": "Active One", "content": _VALID_PEQ},
    )
    client.post(
        "/api/eq/load-profile",
        json={"profile_id": "User imported/Active One"},
    )
    assert server.settings.eq_active_profile_id == "User imported/Active One"

    r = client.post(
        "/api/eq/delete-profile",
        json={"profile_id": "User imported/Active One"},
    )
    assert r.status_code == 200
    assert r.json()["cleared_active"] is True
    assert server.settings.eq_active_profile_id == ""


def test_delete_refuses_non_user_imported_profile(client, tmp_path):
    """Bundled profiles aren't user-removable through the UI; they
    live alongside source code and would just come back on
    reinstall. Deleting a profile whose id doesn't start with
    `User imported/` must 400 instead of silently doing nothing
    (or worse, deleting cache files we don't own)."""
    r = client.post(
        "/api/eq/delete-profile",
        json={"profile_id": "oratory1990/Sennheiser HD 600"},
    )
    assert r.status_code == 400
    assert "user-imported" in r.json()["detail"].lower()


def test_delete_404_when_profile_missing(client):
    """A delete request for a profile id that doesn't exist on
    disk shouldn't pretend success."""
    r = client.post(
        "/api/eq/delete-profile",
        json={"profile_id": "User imported/Never Existed"},
    )
    assert r.status_code == 404


def test_imports_are_atomic(client, tmp_path):
    """Successful import shouldn't leave a `.part` file behind —
    the tmp file is renamed into place, not left as a sibling.
    Catches a regression where the atomic-write helper would skip
    the rename and leave a half-named file the parser later chokes
    on."""
    client.post(
        "/api/eq/import-profile",
        json={"headphone_name": "Atomic Test", "content": _VALID_PEQ},
    )
    profile_dir = (
        tmp_path
        / "autoeq_cache"
        / "results"
        / "User imported"
        / "Atomic Test"
    )
    files = list(profile_dir.iterdir())
    suffixes = [f.suffix for f in files]
    assert ".part" not in suffixes, f"leftover .part file: {files}"
