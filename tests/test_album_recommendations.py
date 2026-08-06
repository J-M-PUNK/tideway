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
    # Default: Tidal's For You page contributes nothing unless a test opts
    # in. Also teach the album predicate to recognize our stubs (real
    # tidalapi.Album instances need a live session to construct).
    monkeypatch.setattr(server.tidal.session, "for_you", lambda: StubPage([]))
    monkeypatch.setattr(server, "_is_tidal_album", lambda i: isinstance(i, StubAlbum))


class StubPage:
    def __init__(self, categories):
        self.categories = categories


class StubCategory:
    def __init__(self, title, items, subtitle=""):
        self.title = title
        self.subtitle = subtitle
        self.items = items


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


def test_for_you_album_modules_harvested(stub_auth, monkeypatch):
    import server

    _no_lastfm(monkeypatch)
    _set_library(monkeypatch)  # no favorites — only the For You source
    # Tidal's For You page: an album module (harvested, labelled by the
    # module title + subtitle) and a non-album item (ignored).
    page = StubPage([
        StubCategory(
            "Because you listened to",
            [StubAlbum("88", "Native Pick", "Artist Q"), object()],
            subtitle="Radiohead",
        ),
    ])
    monkeypatch.setattr(server.tidal.session, "for_you", lambda: page)
    out = server._album_recommendations()
    assert [a["id"] for a in out] == ["88"]
    assert out[0]["reason"] == "Because you listened to Radiohead"


def test_for_you_outranks_similar_graph(stub_auth, monkeypatch):
    """Tidal's native picks (base 150) should rank above the favorites
    similar-graph fallback (base 100)."""
    import server

    _no_lastfm(monkeypatch)
    seed = StubAlbum("1", "Seed", "Fav Artist", similar=[
        StubAlbum("2", "Graph Pick", "Artist B"),
    ])
    _set_library(monkeypatch, fav_albums=[seed])
    page = StubPage([
        StubCategory("Suggested new albums", [StubAlbum("3", "Native Pick", "Artist C")]),
    ])
    monkeypatch.setattr(server.tidal.session, "for_you", lambda: page)
    out = server._album_recommendations()
    ids = [a["id"] for a in out]
    assert ids.index("3") < ids.index("2")


def test_reissues_collapsed_by_title_and_artist(stub_auth, monkeypatch):
    """Same album under two Tidal ids (deluxe/explicit/regional) should
    appear once — the id-dedupe can't catch it, the title+artist one does."""
    import server

    _no_lastfm(monkeypatch)
    seed = StubAlbum("1", "Seed", "A", similar=[
        StubAlbum("100", "Power Ballad", "Same Artist"),
        StubAlbum("200", "power ballad", "Same Artist"),  # reissue, other id
    ])
    _set_library(monkeypatch, fav_albums=[seed])
    out = server._album_recommendations()
    assert len(out) == 1
    assert out[0]["name"] == "Power Ballad"


def test_per_reason_cap_diversifies(stub_auth, monkeypatch):
    """A single big seed/module can't fill the page — no reason exceeds
    the cap, and a smaller seed still gets represented (the live-library
    "40 game OSTs from one seed" failure)."""
    import server
    from collections import Counter

    _no_lastfm(monkeypatch)
    # One seed with a big pile of similars, each a distinct artist so the
    # per-artist cap doesn't mask the per-reason behavior.
    big = [StubAlbum(str(i), f"Album {i}", f"Artist {i}") for i in range(20)]
    seed_a = StubAlbum("100", "Seed A", "SA", similar=big)
    seed_b = StubAlbum("101", "Seed B", "SB", similar=[StubAlbum("999", "Other", "ZZ")])
    _set_library(monkeypatch, fav_albums=[seed_a, seed_b])

    out = server._album_recommendations()
    counts = Counter(a["reason"] for a in out)
    assert max(counts.values()) <= server._RECS_PER_REASON_CAP
    assert "Because you saved Seed B" in counts  # smaller seed survives


def test_for_you_failure_is_non_fatal(stub_auth, monkeypatch):
    """A broken For You fetch must not sink the whole endpoint — the
    similar-graph candidates should still come through."""
    import server

    _no_lastfm(monkeypatch)

    def _boom():
        raise RuntimeError("tidal for_you 500")

    monkeypatch.setattr(server.tidal.session, "for_you", _boom)
    seed = StubAlbum("1", "Seed", "A", similar=[StubAlbum("9", "Still Here", "B")])
    _set_library(monkeypatch, fav_albums=[seed])
    assert [a["id"] for a in server._album_recommendations()] == ["9"]
