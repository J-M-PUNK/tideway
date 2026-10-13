"""Changing a content filter must drop payloads filtered under the old value.

`hide_ai_content` is applied before album, mix and playlist detail
payloads, artist pages, Home and the other editorial pages, and the
release feed are cached; `explicit_content_preference` before artist
pages. Before this, flipping either one looked like it did nothing: the
downloader honoured the new value at once, but anything already opened
kept its old filtering for the full TTL — up to fifteen minutes for the
feed. That is how #439 read the AI toggle as missing.

These drive the real `PUT /api/settings` and assert on the caches
themselves, so a cache added later without being wired in fails here
rather than in a bug report.
"""
import copy
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Same isolation as test_settings_endpoint: settings written to a
    tmp file and the in-memory snapshot restored afterwards."""
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


def _seed_all_caches():
    """Put one live entry in every cache that holds filtered output."""
    import server

    now = time.monotonic()
    server._store_detail_cache("album:1", {"tracks": ["seeded"]})
    server._store_page_cache("name:home", {"categories": ["seeded"]})
    with server._artist_detail_cache_lock:
        server._artist_detail_cache["artist:1"] = (now, {"seeded": True})
    with server._feed_lock:
        server._feed_cache["at"] = now
        server._feed_cache["value"] = {"items": ["seeded"]}


def _cache_state() -> dict:
    import server

    return {
        "detail": server._lookup_detail_cache("album:1") is not None,
        "page": server._lookup_page_cache("name:home") is not None,
        "artist": "artist:1" in server._artist_detail_cache,
        "feed": server._feed_cache["value"] is not None,
    }


ALL_FILLED = {"detail": True, "page": True, "artist": True, "feed": True}
ALL_EMPTY = {"detail": False, "page": False, "artist": False, "feed": False}


def test_toggling_hide_ai_content_clears_every_filtered_cache(client):
    import server

    current = bool(server.settings.hide_ai_content)
    _seed_all_caches()
    assert _cache_state() == ALL_FILLED

    r = client.put("/api/settings", json={"hide_ai_content": not current})
    assert r.status_code == 200
    assert _cache_state() == ALL_EMPTY


def test_changing_explicit_preference_clears_the_caches(client):
    import server

    current = server.settings.explicit_content_preference
    other = "clean" if current != "clean" else "explicit"
    _seed_all_caches()

    r = client.put("/api/settings", json={"explicit_content_preference": other})
    assert r.status_code == 200
    assert _cache_state() == ALL_EMPTY


def test_resending_the_same_filter_value_keeps_the_caches(client):
    # The frontend PUTs whole sections at a time, so an unchanged value
    # arriving is routine. Clearing on that would throw away Home and
    # every open page for nothing.
    import server

    _seed_all_caches()
    r = client.put(
        "/api/settings", json={"hide_ai_content": bool(server.settings.hide_ai_content)}
    )
    assert r.status_code == 200
    assert _cache_state() == ALL_FILLED


def test_an_unrelated_setting_keeps_the_caches(client):
    _seed_all_caches()
    r = client.put("/api/settings", json={"force_volume": True})
    assert r.status_code == 200
    assert _cache_state() == ALL_FILLED
