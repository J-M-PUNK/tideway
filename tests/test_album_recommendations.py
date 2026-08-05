"""Tests for the "For You" album-recommendations endpoint (#307).

The ranking/blend logic in `_album_recommendations` is the risky part:
it composes Tidal `album.similar()` / `artist.get_similar()` with the
Last.fm similar-artist graph, dedupes, filters out already-favorited
albums, caps per-artist, and attaches a human "reason" to each pick.
These pin that behavior with stubbed Tidal/Last.fm surfaces.
"""
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _clean_recs_cache():
    import server

    with server._recs_cache_lock:
        server._recs_cache.clear()
    yield
    with server._recs_cache_lock:
        server._recs_cache.clear()


@pytest.fixture
def stub_auth(monkeypatch):
    import server

    monkeypatch.setattr(server, "_require_auth", lambda: None)
    # Serialize albums minimally so we don't need the full tidalapi surface.
    monkeypatch.setattr(
        server,
        "album_to_dict",
        lambda a: {
            "id": str(a.id),
            "name": a.name,
            "artists": [{"name": a.artists[0].name}],
            "available": a.available,
        },
    )


class StubAlbum:
    def __init__(self, id, name, artist, available=True, similar=None):
        self.id = id
        self.name = name
        self.artists = [SimpleNamespace(name=artist)]
        self.available = available
        self._similar = similar or []

    def similar(self):
        return self._similar


class StubArtist:
    def __init__(self, name, similar=None, albums=None):
        self.name = name
        self._similar = similar or []
        self._albums = albums or []

    def get_similar(self):
        return self._similar

    def get_albums(self, limit=None, offset=0):
        return self._albums[:limit] if limit else self._albums


def _set_library(monkeypatch, *, fav_artists=(), fav_albums=()):
    import server

    monkeypatch.setattr(server.tidal, "get_favorite_artists", lambda: list(fav_artists))
    monkeypatch.setattr(server.tidal, "get_favorite_albums", lambda: list(fav_albums))


def _no_lastfm(monkeypatch):
    import server

    monkeypatch.setattr(server.lastfm, "status", lambda: {"connected": False})


# ---------------------------------------------------------------------------
# Endpoint gating
# ---------------------------------------------------------------------------


def test_disabled_setting_returns_empty(stub_auth, monkeypatch):
    import server

    monkeypatch.setattr(
        server.settings, "album_recommendations_enabled", False, raising=False
    )
    out = server.recommendations_albums()
    assert out == {"enabled": False, "albums": []}


def test_enabled_wraps_albums(stub_auth, monkeypatch):
    import server

    monkeypatch.setattr(
        server.settings, "album_recommendations_enabled", True, raising=False
    )
    _no_lastfm(monkeypatch)
    seed = StubAlbum("1", "Seed", "Artist A", similar=[
        StubAlbum("2", "Rec One", "Artist B"),
    ])
    _set_library(monkeypatch, fav_albums=[seed])
    out = server.recommendations_albums()
    assert out["enabled"] is True
    assert [a["id"] for a in out["albums"]] == ["2"]
    assert out["albums"][0]["reason"] == "Because you saved Seed"


# ---------------------------------------------------------------------------
# Ranking / filtering
# ---------------------------------------------------------------------------


def test_owned_albums_are_filtered_out(stub_auth, monkeypatch):
    import server

    _no_lastfm(monkeypatch)
    # Seed's "similar" includes an album the user already owns (id 1) plus
    # a genuinely new one (id 9). Only the new one should surface.
    seed = StubAlbum("1", "Owned Seed", "Artist A", similar=[
        StubAlbum("1", "Owned Seed", "Artist A"),
        StubAlbum("9", "Fresh Pick", "Artist Z"),
    ])
    _set_library(monkeypatch, fav_albums=[seed])
    out = server._album_recommendations()
    ids = [a["id"] for a in out]
    assert "1" not in ids
    assert "9" in ids


def test_unavailable_albums_dropped(stub_auth, monkeypatch):
    import server

    _no_lastfm(monkeypatch)
    seed = StubAlbum("1", "Seed", "Artist A", similar=[
        StubAlbum("2", "Not Streamable", "Artist B", available=False),
        StubAlbum("3", "Streamable", "Artist C", available=True),
    ])
    _set_library(monkeypatch, fav_albums=[seed])
    ids = [a["id"] for a in server._album_recommendations()]
    assert ids == ["3"]


def test_per_artist_cap(stub_auth, monkeypatch):
    import server

    _no_lastfm(monkeypatch)
    # Five candidate albums all by the same artist; the cap is 2.
    same_artist = [
        StubAlbum(str(i), f"Album {i}", "One Artist") for i in range(10, 15)
    ]
    seed = StubAlbum("1", "Seed", "Seed Artist", similar=same_artist)
    _set_library(monkeypatch, fav_albums=[seed])
    out = server._album_recommendations()
    assert len(out) == server._RECS_PER_ARTIST_CAP


def test_dedupe_keeps_single_entry(stub_auth, monkeypatch):
    import server

    _no_lastfm(monkeypatch)
    # The same album reached from two different seeds should appear once.
    dup = StubAlbum("42", "Shared", "Artist X")
    seed1 = StubAlbum("1", "Seed One", "A", similar=[dup])
    seed2 = StubAlbum("2", "Seed Two", "B", similar=[dup])
    _set_library(monkeypatch, fav_albums=[seed1, seed2])
    out = server._album_recommendations()
    assert [a["id"] for a in out].count("42") == 1


def test_similar_artist_albums_are_included(stub_auth, monkeypatch):
    import server

    _no_lastfm(monkeypatch)
    similar_artist = StubArtist(
        "Neighbor", albums=[StubAlbum("7", "Neighbor LP", "Neighbor")]
    )
    fav_artist = StubArtist("Fav", similar=[similar_artist])
    _set_library(monkeypatch, fav_artists=[fav_artist])
    out = server._album_recommendations()
    assert [a["id"] for a in out] == ["7"]
    assert out[0]["reason"] == "Because you follow Fav"


def test_lastfm_blend_when_connected(stub_auth, monkeypatch):
    import server

    monkeypatch.setattr(server.lastfm, "status", lambda: {"connected": True})
    monkeypatch.setattr(
        server.lastfm, "get_top_artists", lambda period, limit: [{"name": "TopA"}]
    )
    monkeypatch.setattr(
        server.lastfm,
        "get_similar_artists",
        lambda name, limit=30: [{"name": "SimA", "match": 0.9}],
    )
    resolved = StubArtist("SimA", albums=[StubAlbum("55", "Lastfm Pick", "SimA")])
    monkeypatch.setattr(server.tidal, "search", lambda q, limit=25: {"artists": [resolved]})
    _set_library(monkeypatch)  # no Tidal favorites — only the Last.fm path

    out = server._album_recommendations()
    assert [a["id"] for a in out] == ["55"]
    assert out[0]["reason"] == "Because you listen to TopA"
