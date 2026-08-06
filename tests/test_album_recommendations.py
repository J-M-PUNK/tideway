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

    def _reset():
        with server._recs_cache_lock:
            server._recs_cache.clear()
        with server._genre_map_lock:
            server._genre_map_cache["at"] = 0.0
            server._genre_map_cache["map"] = None

    _reset()
    yield
    _reset()


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


def _no_aoty(monkeypatch):
    """Neutralize the AOTY-backed sections so endpoint/assembly tests don't
    hit the network."""
    import server

    monkeypatch.setattr(server.aoty_module, "recent_releases", lambda limit=30: [])
    monkeypatch.setattr(server.aoty_module, "top_albums_of_year", lambda year, limit=50: [])
    monkeypatch.setattr(
        server.aoty_module, "top_albums_of_year_by_genre", lambda slug, year, limit=60: []
    )
    monkeypatch.setattr(server.aoty_module, "genre_index", lambda: [])


# ---------------------------------------------------------------------------
# Endpoint gating
# ---------------------------------------------------------------------------


def test_disabled_setting_returns_empty(stub_auth, monkeypatch):
    import server

    monkeypatch.setattr(
        server.settings, "album_recommendations_enabled", False, raising=False
    )
    out = server.recommendations_albums()
    assert out == {"enabled": False, "sections": []}


def test_enabled_returns_made_for_you_section(stub_auth, monkeypatch):
    import server

    monkeypatch.setattr(
        server.settings, "album_recommendations_enabled", True, raising=False
    )
    _no_lastfm(monkeypatch)
    _no_aoty(monkeypatch)
    seed = StubAlbum("1", "Seed", "Artist A", similar=[
        StubAlbum("2", "Rec One", "Artist B"),
    ])
    _set_library(monkeypatch, fav_albums=[seed])
    out = server.recommendations_albums()
    assert out["enabled"] is True
    made = [s for s in out["sections"] if s["key"] == "made_for_you"]
    assert made, "Made For You section should be present"
    assert [a["id"] for a in made[0]["albums"]] == ["2"]
    assert made[0]["albums"][0]["reason"] == "Because you saved Seed"


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


# ---------------------------------------------------------------------------
# Sectioned page: taste profile, AOTY ranking, assembly
# ---------------------------------------------------------------------------


def test_dedupe_cap_albums_collapses_and_caps():
    import server

    albums = [
        {"name": "X", "artists": [{"name": "A"}]},
        {"name": "x", "artists": [{"name": "A"}]},  # reissue of X -> dropped
        {"name": "Y", "artists": [{"name": "A"}]},
        {"name": "Z", "artists": [{"name": "A"}]},  # 3rd by A -> capped at 2
        {"name": "W", "artists": [{"name": "B"}]},
    ]
    assert [a["name"] for a in server._dedupe_cap_albums(albums, 10)] == ["X", "Y", "W"]


def test_taste_profile_empty_when_disconnected(stub_auth, monkeypatch):
    import server

    _no_lastfm(monkeypatch)
    assert server._taste_profile() == {
        "connected": False,
        "artist_names": set(),
        "genres": [],
    }


def test_taste_profile_infers_genres_from_tags(stub_auth, monkeypatch):
    import server

    monkeypatch.setattr(server.lastfm, "status", lambda: {"connected": True})
    monkeypatch.setattr(
        server.lastfm, "get_top_artists",
        lambda period, limit: [{"name": "Boards of Canada", "playcount": 500}],
    )
    monkeypatch.setattr(
        server.lastfm, "get_artist_top_tags",
        lambda name, limit=4: [
            {"name": "Electronic", "count": 100},
            {"name": "IDM", "count": 80},  # no AOTY genre -> dropped
        ],
    )
    monkeypatch.setattr(
        server, "_aoty_genre_slug_map",
        lambda: {"electronic": ("7-electronic", "Electronic"),
                 "rock": ("9-rock", "Rock")},
    )
    prof = server._taste_profile()
    assert prof["connected"] is True
    assert "boards of canada" in prof["artist_names"]
    slugs = [g[0] for g in prof["genres"]]
    assert slugs == ["7-electronic"]  # IDM has no AOTY genre -> dropped


def test_genre_slug_map_harvests_subgenres(monkeypatch):
    """The slug map must include sub-genres from album tags, not just the
    ~48 on genre.php — otherwise 'Best of Shoegaze' can never render."""
    import server

    monkeypatch.setattr(
        server.aoty_module, "genre_index",
        lambda: [{"slug": "6-electronic", "name": "Electronic"}],
    )
    monkeypatch.setattr(
        server.aoty_module, "top_albums_of_year",
        lambda year, limit=100: [
            {"genres": ["Shoegaze", "Slowcore"], "genre_slugs": ["26-shoegaze", "119-slowcore"]},
        ],
    )
    m = server._aoty_genre_slug_map()
    assert m["electronic"] == ("6-electronic", "Electronic")
    assert m["shoegaze"] == ("26-shoegaze", "Shoegaze")
    assert m["slowcore"] == ("119-slowcore", "Slowcore")


def _resolver_passthrough(monkeypatch):
    import server

    def _resolve(items):
        return [
            {**it, "tidal_album": {
                "id": it["id"], "name": it["title"],
                "artists": [{"name": it["artist"]}], "available": True}}
            for it in items
        ]

    monkeypatch.setattr(server.aoty_resolver, "resolve_listing", _resolve)


def test_aoty_section_ranks_by_score_and_genre(stub_auth, monkeypatch):
    import server

    _resolver_passthrough(monkeypatch)
    profile = {"connected": True, "artist_names": set(),
               "genres": [("7-electronic", "Electronic", 1.0)]}
    listing = [
        {"id": "a", "title": "High No Genre", "artist": "A", "score": 90, "genre_slugs": ["9-rock"]},
        {"id": "b", "title": "Lower In Genre", "artist": "B", "score": 70, "genre_slugs": ["7-electronic"]},
    ]
    sec = server._aoty_section("k", "T", "s", listing, profile, taste_rank=True)
    # b: 70*0.7 + 25 = 74 ; a: 90*0.7 = 63  -> b first
    assert [x["id"] for x in sec["albums"]] == ["b", "a"]


def test_aoty_section_without_taste_rank_keeps_order(stub_auth, monkeypatch):
    import server

    _resolver_passthrough(monkeypatch)
    listing = [
        {"id": "a", "title": "First", "artist": "A", "score": 10, "genre_slugs": []},
        {"id": "b", "title": "Second", "artist": "B", "score": 99, "genre_slugs": []},
    ]
    sec = server._aoty_section("k", "T", "s", listing, {"genres": []}, taste_rank=False)
    assert [x["id"] for x in sec["albums"]] == ["a", "b"]


def test_section_assembly_orders_and_drops_empty(stub_auth, monkeypatch):
    import server

    _no_lastfm(monkeypatch)  # no genres, no behavioral seeds
    _resolver_passthrough(monkeypatch)
    monkeypatch.setattr(
        server.aoty_module, "recent_releases",
        lambda limit=30: [{"id": "n1", "title": "New", "artist": "NA", "score": 80, "genre_slugs": []}],
    )
    monkeypatch.setattr(
        server.aoty_module, "top_albums_of_year",
        lambda year, limit=50: [{"id": "p1", "title": "Pop", "artist": "PA", "score": 95, "genre_slugs": []}],
    )
    monkeypatch.setattr(server.aoty_module, "genre_index", lambda: [])
    seed = StubAlbum("1", "Seed", "SA", similar=[StubAlbum("m1", "Made Pick", "MB")])
    _set_library(monkeypatch, fav_albums=[seed])

    sections = server._build_recommendation_sections()
    # Genre rows absent (no inferred genres); order is made -> new -> popular.
    assert [s["key"] for s in sections] == ["made_for_you", "new_releases", "popular"]
    made = next(s for s in sections if s["key"] == "made_for_you")
    assert [a["id"] for a in made["albums"]] == ["m1"]
