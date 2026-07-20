"""Local album collections (#243): store logic + HTTP endpoints.

The store is pure disk I/O with no Tidal dependency, so these run
without a session. The endpoint tests reuse the offline-mode client
pattern so the local-access guard lets the unauthenticated calls
through.
"""
from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    import app.album_collections as ac

    monkeypatch.setattr(ac, "_FILE", tmp_path / "album_collections.json")
    yield


@pytest.fixture
def client(monkeypatch):
    import server

    original_settings = copy.deepcopy(server.settings)
    server.settings.offline_mode = True
    with TestClient(server.app) as c:
        yield c
    server.settings = original_settings


def _album(aid="100", name="Album", cover="c.jpg"):
    return {
        "kind": "album",
        "id": aid,
        "name": name,
        "cover": cover,
        "artists": [{"id": "9", "name": "Artist"}],
        "year": 2024,
        "extra_field_we_drop": "junk",
    }


# ---- store ----------------------------------------------------------


def test_create_and_list():
    import app.album_collections as ac

    s = ac.create_collection("Chill")
    assert s["name"] == "Chill" and s["count"] == 0
    rows = ac.list_collections()
    assert [r["id"] for r in rows] == [s["id"]]


def test_add_album_dedupes_and_normalizes():
    import app.album_collections as ac

    cid = ac.create_collection("X")["id"]
    assert ac.add_album(cid, _album("1")) is True
    # Second add of the same id is an idempotent no-op.
    assert ac.add_album(cid, _album("1")) is False
    detail = ac.get_collection(cid)
    assert len(detail["albums"]) == 1
    stored = detail["albums"][0]
    # Unknown fields are dropped; id is coerced to str; kind is set.
    assert "extra_field_we_drop" not in stored
    assert stored["id"] == "1" and stored["kind"] == "album"


def test_add_album_missing_id_returns_none():
    import app.album_collections as ac

    cid = ac.create_collection("X")["id"]
    assert ac.add_album(cid, {"name": "no id"}) is None


def test_add_album_unknown_collection_returns_none():
    import app.album_collections as ac

    assert ac.add_album("col_missing", _album("1")) is None


def test_remove_rename_delete():
    import app.album_collections as ac

    cid = ac.create_collection("X")["id"]
    ac.add_album(cid, _album("1"))
    assert ac.remove_album(cid, "1") is True
    assert ac.remove_album(cid, "1") is False  # already gone
    assert ac.rename_collection(cid, "Y") is True
    assert ac.get_collection(cid)["name"] == "Y"
    assert ac.delete_collection(cid) is True
    assert ac.get_collection(cid) is None
    assert ac.delete_collection(cid) is False


def test_summary_covers_capped_at_four():
    import app.album_collections as ac

    cid = ac.create_collection("X")["id"]
    for i in range(6):
        ac.add_album(cid, _album(str(i), cover=f"{i}.jpg"))
    summary = next(s for s in ac.list_collections() if s["id"] == cid)
    assert summary["count"] == 6
    assert len(summary["covers"]) == 4


def test_persists_across_reload(tmp_path, monkeypatch):
    import app.album_collections as ac

    cid = ac.create_collection("Keeps")["id"]
    ac.add_album(cid, _album("42"))
    # A fresh read from disk (no in-memory cache) still has it.
    reloaded = ac.get_collection(cid)
    assert reloaded and reloaded["albums"][0]["id"] == "42"


# ---- endpoints ------------------------------------------------------


def test_endpoint_full_lifecycle(client):
    r = client.post("/api/collections", json={"name": "Faves"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]

    assert client.get("/api/collections").json()[0]["id"] == cid

    add = client.post(
        f"/api/collections/{cid}/albums", json={"album": _album("7")}
    )
    assert add.status_code == 200 and add.json()["added"] is True

    detail = client.get(f"/api/collections/{cid}").json()
    assert detail["albums"][0]["id"] == "7"

    rm = client.delete(f"/api/collections/{cid}/albums/7")
    assert rm.status_code == 200

    assert client.patch(
        f"/api/collections/{cid}", json={"name": "Renamed"}
    ).status_code == 200
    assert client.delete(f"/api/collections/{cid}").status_code == 200
    assert client.get(f"/api/collections/{cid}").status_code == 404


def test_endpoint_empty_name_rejected(client):
    assert client.post("/api/collections", json={"name": "  "}).status_code == 400


def test_endpoint_add_to_missing_collection_404(client):
    r = client.post(
        "/api/collections/col_nope/albums", json={"album": _album("1")}
    )
    assert r.status_code == 404


def test_endpoint_add_album_without_id_400(client):
    cid = client.post("/api/collections", json={"name": "X"}).json()["id"]
    r = client.post(
        f"/api/collections/{cid}/albums", json={"album": {"name": "no id"}}
    )
    assert r.status_code == 400
