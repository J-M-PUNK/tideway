"""The AOTY genre taxonomy survives a restart.

Measured on a real cold start, the taste profile every For You row
blocks on took 4515 ms, and 1297 ms of that was `_aoty_genre_slug_map`
harvesting AOTY's taxonomy. Its docstring says "cached ~a day" and the
TTL agrees — but the cache was a module-level dict, so the day only
ever lasted as long as the process. Quitting Tideway threw it away and
the next launch paid the 1297 ms again.

That is the same argument `app/lastfm_disk_cache.py` was written for:
"the in-memory dict dies on app restart, so anyone who quits Tideway
between visits paid the 18 seconds again." Nothing in this map is
per-user and the taxonomy barely moves, so it belongs on disk too.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def srv(tmp_path, monkeypatch):
    import server
    from app import lastfm_disk_cache

    lastfm_disk_cache._set_db_path_for_testing(tmp_path / "cache.db")
    # Clear the in-memory layer so the disk path is what gets exercised.
    with server._genre_map_lock:
        server._genre_map_cache["map"] = None
        server._genre_map_cache["at"] = 0.0
    yield server
    lastfm_disk_cache._set_db_path_for_testing(None)
    with server._genre_map_lock:
        server._genre_map_cache["map"] = None
        server._genre_map_cache["at"] = 0.0


def _stub_aoty(srv, monkeypatch, calls: list):
    def genre_index():
        calls.append("index")
        return [{"name": "Shoegaze", "slug": "26-shoegaze"}]

    monkeypatch.setattr(
        srv.aoty_module,
        "genre_index",
        genre_index,
    )
    monkeypatch.setattr(
        srv.aoty_module, "top_albums_of_year", lambda y, n: []
    )


def test_map_is_harvested_and_persisted(srv, monkeypatch):
    calls: list = []
    _stub_aoty(srv, monkeypatch, calls)

    out = srv._aoty_genre_slug_map()

    assert out["shoegaze"] == ("26-shoegaze", "Shoegaze")
    assert calls == ["index"]


def test_a_restart_reads_the_map_off_disk(srv, monkeypatch):
    """The point of the change: the second process does no AOTY work."""
    calls: list = []
    _stub_aoty(srv, monkeypatch, calls)
    srv._aoty_genre_slug_map()
    assert calls == ["index"]

    # Simulate a restart: memory gone, disk intact.
    with srv._genre_map_lock:
        srv._genre_map_cache["map"] = None
        srv._genre_map_cache["at"] = 0.0

    out = srv._aoty_genre_slug_map()

    assert out["shoegaze"] == ("26-shoegaze", "Shoegaze"), "tuple not restored"
    assert calls == ["index"], "harvested again instead of reading disk"


def test_an_empty_harvest_is_not_persisted(srv, monkeypatch):
    """An AOTY outage returns nothing. Caching that would serve an
    empty taxonomy for a day and quietly drop every genre row."""
    from app import lastfm_disk_cache

    monkeypatch.setattr(srv.aoty_module, "genre_index", lambda: [])
    monkeypatch.setattr(srv.aoty_module, "top_albums_of_year", lambda y, n: [])

    assert srv._aoty_genre_slug_map() == {}
    assert lastfm_disk_cache.get(srv._GENRE_MAP_DISK_KEY, 24 * 3600.0) is None


def test_a_corrupt_row_falls_through_to_a_harvest(srv, monkeypatch):
    """Entries that aren't (slug, display) pairs are ignored rather
    than returned as a broken map."""
    from app import lastfm_disk_cache

    lastfm_disk_cache.set(srv._GENRE_MAP_DISK_KEY, {"shoegaze": "not-a-pair"})
    calls: list = []
    _stub_aoty(srv, monkeypatch, calls)

    out = srv._aoty_genre_slug_map()

    assert out["shoegaze"] == ("26-shoegaze", "Shoegaze")
    assert calls == ["index"]
