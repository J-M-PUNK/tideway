"""Saved manual EQ profiles: create, rename, overwrite, delete, load.

Profiles are storage only. Loading one is the client posting its bands
and preamp to POST /api/player/eq, so the last tests here drive that
path to prove a saved profile round-trips into the live curve, and that
anything saved passes the same validation the live endpoint applies.
"""
from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

BASS = [{"type": "LSC", "freq": 105, "gain": 6.0, "q": 0.7, "enabled": True}]
VOCAL = [
    {"type": "PK", "freq": 2500, "gain": 3.0, "q": 1.2, "enabled": True},
    {"type": "PK", "freq": 300, "gain": -2.0, "q": 1.0, "enabled": True},
]
URL = "/api/player/eq/manual-profiles"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Settings written to a tmp file and the in-memory snapshot
    restored afterwards, as in test_settings_endpoint. EQ starts
    disabled so loading a curve never touches the audio engine unless
    a test opts in."""
    import app.settings as _settings_mod
    import server

    monkeypatch.setattr(_settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    original_settings = copy.deepcopy(server.settings)
    server.settings.offline_mode = True
    server.settings.eq_enabled = False
    server.settings.eq_manual_profiles = []
    server.downloader.settings = server.settings

    with TestClient(server.app) as c:
        yield c

    server.settings = original_settings
    server.downloader.settings = original_settings


def _create(client, name, bands=BASS, preamp=None):
    return client.post(URL, json={"name": name, "bands": bands, "preamp": preamp})


# --- create -----------------------------------------------------------------


def test_create_saves_and_lists_the_profile(client):
    r = _create(client, "Bass", preamp=-3.0)
    assert r.status_code == 200
    body = r.json()
    profile = body["profile"]
    assert profile["name"] == "Bass"
    assert profile["preamp"] == -3.0
    assert profile["bands"][0]["type"] == "LSC"
    assert profile["id"]
    assert [p["id"] for p in body["profiles"]] == [profile["id"]]

    # And it's served with the rest of the EQ state the editor loads.
    eq = client.get("/api/player/eq").json()
    assert [p["name"] for p in eq["manual_profiles"]] == ["Bass"]


def test_profiles_persist_to_disk(client):
    import app.settings as _settings_mod

    _create(client, "Bass")
    reloaded = _settings_mod.load_settings()
    assert [p["name"] for p in reloaded.eq_manual_profiles] == ["Bass"]


def test_create_keeps_order_and_unique_ids(client):
    a = _create(client, "Bass").json()["profile"]
    b = _create(client, "Vocal", bands=VOCAL).json()["profile"]
    assert a["id"] != b["id"]
    names = [p["name"] for p in client.get("/api/player/eq").json()["manual_profiles"]]
    assert names == ["Bass", "Vocal"]


def test_create_normalises_whitespace_in_the_name(client):
    r = _create(client, "  Late   night  ")
    assert r.json()["profile"]["name"] == "Late night"


@pytest.mark.parametrize("name", ["", "   ", "x" * 61])
def test_create_rejects_bad_names(client, name):
    assert _create(client, name).status_code == 400


def test_create_rejects_a_duplicate_name_case_insensitively(client):
    _create(client, "Bass")
    r = _create(client, "bass")
    assert r.status_code == 409
    # Names the profile as the user saved it, not as they just typed it.
    assert '"Bass"' in r.json()["detail"]


def test_create_rejects_a_curve_the_live_eq_would_reject(client):
    # A saved profile must never hold something that fails on load.
    bad = [{"type": "PK", "freq": 1000, "gain": 999.0, "q": 1.0, "enabled": True}]
    assert _create(client, "Loud", bands=bad).status_code == 400
    assert _create(client, "Loud", preamp=999.0).status_code == 400
    # A NaN gain would silence the audio on load. Standard JSON can't
    # carry NaN, so send the bare literal the way a hand-edited request
    # could.
    r = client.post(
        URL,
        content='{"name":"N","bands":[{"type":"PK","freq":1000,"gain":NaN,"q":1}]}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code in (400, 422)


def test_create_caps_the_number_of_profiles(client):
    import server

    for i in range(server._EQ_MANUAL_PROFILES_MAX):
        assert _create(client, f"P{i}").status_code == 200
    assert _create(client, "One too many").status_code == 400


def test_an_empty_curve_is_a_valid_profile(client):
    # Flat is a legitimate thing to save — "my reference" profile.
    r = _create(client, "Flat", bands=[])
    assert r.status_code == 200
    assert r.json()["profile"]["bands"] == []


# --- update -----------------------------------------------------------------


def test_rename(client):
    pid = _create(client, "Bass").json()["profile"]["id"]
    r = client.put(f"{URL}/{pid}", json={"name": "Bassy"})
    assert r.status_code == 200
    assert r.json()["profile"]["name"] == "Bassy"
    # A rename leaves the curve alone.
    assert r.json()["profile"]["bands"][0]["gain"] == 6.0


def test_rename_to_its_own_name_in_different_case_is_allowed(client):
    pid = _create(client, "bass").json()["profile"]["id"]
    assert client.put(f"{URL}/{pid}", json={"name": "Bass"}).status_code == 200


def test_rename_into_another_profiles_name_is_rejected(client):
    _create(client, "Bass")
    pid = _create(client, "Vocal", bands=VOCAL).json()["profile"]["id"]
    assert client.put(f"{URL}/{pid}", json={"name": "BASS"}).status_code == 409


def test_overwrite_curve_keeps_the_name(client):
    pid = _create(client, "Mine", preamp=-2.0).json()["profile"]["id"]
    r = client.put(f"{URL}/{pid}", json={"bands": VOCAL, "preamp": None})
    p = r.json()["profile"]
    assert p["name"] == "Mine"
    assert len(p["bands"]) == 2
    # An explicit null preamp clears it rather than being ignored.
    assert p["preamp"] is None


def test_updating_only_the_name_does_not_clear_the_preamp(client):
    pid = _create(client, "Mine", preamp=-2.0).json()["profile"]["id"]
    r = client.put(f"{URL}/{pid}", json={"name": "Still mine"})
    assert r.json()["profile"]["preamp"] == -2.0


def test_update_validates_like_create(client):
    pid = _create(client, "Mine").json()["profile"]["id"]
    bad = [{"type": "NOPE", "freq": 1000, "gain": 1.0, "q": 1.0, "enabled": True}]
    assert client.put(f"{URL}/{pid}", json={"bands": bad}).status_code == 400
    assert client.put(f"{URL}/{pid}", json={"preamp": 999.0}).status_code == 400
    assert client.put(f"{URL}/{pid}", json={"name": "  "}).status_code == 400
    assert client.put(f"{URL}/{pid}", json={}).status_code == 400


def test_update_unknown_profile_is_404(client):
    assert client.put(f"{URL}/nope", json={"name": "X"}).status_code == 404


# --- delete -----------------------------------------------------------------


def test_delete_removes_only_that_profile(client):
    a = _create(client, "Bass").json()["profile"]["id"]
    _create(client, "Vocal", bands=VOCAL)
    r = client.delete(f"{URL}/{a}")
    assert r.status_code == 200
    assert [p["name"] for p in r.json()["profiles"]] == ["Vocal"]


def test_delete_unknown_profile_is_404(client):
    assert client.delete(f"{URL}/nope").status_code == 404


def test_deleting_a_profile_leaves_the_applied_curve_alone(client):
    import server

    pid = _create(client, "Bass").json()["profile"]["id"]
    client.post("/api/player/eq", json={"bands": BASS, "preamp": None})
    client.delete(f"{URL}/{pid}")
    assert server.settings.eq_parametric_bands[0]["gain"] == 6.0


# --- loading ----------------------------------------------------------------


def test_loading_a_saved_profile_applies_its_curve(client):
    """Loading is the client posting the profile's curve to the live
    endpoint; prove the stored shape round-trips through it."""
    import server

    profile = _create(client, "Vocal", bands=VOCAL, preamp=-1.5).json()["profile"]
    r = client.post(
        "/api/player/eq", json={"bands": profile["bands"], "preamp": profile["preamp"]}
    )
    assert r.status_code == 200
    assert server.settings.eq_parametric_bands == profile["bands"]
    assert server.settings.eq_preamp == -1.5


def test_loading_pushes_to_the_engine_when_eq_is_on(client, monkeypatch):
    import server

    applied = []
    player = server._native_player()
    monkeypatch.setattr(
        player, "apply_equalizer", lambda bands, preamp=None: applied.append((bands, preamp))
    )
    server.settings.eq_enabled = True
    profile = _create(client, "Bass", preamp=-3.0).json()["profile"]
    client.post(
        "/api/player/eq", json={"bands": profile["bands"], "preamp": profile["preamp"]}
    )
    assert len(applied) == 1
    bands, preamp = applied[0]
    assert preamp == -3.0
    assert bands[0].to_dict()["gain"] == 6.0
