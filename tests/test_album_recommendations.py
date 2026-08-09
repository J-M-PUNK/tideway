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
    # "Made For You" reads the top artists and the similar-artist graph
    # regardless of the connected flag (an empty result is the disconnected
    # case), so stub both rather than leaning on ambient credentials.
    monkeypatch.setattr(server.lastfm, "get_top_artists", lambda period, limit: [])
    monkeypatch.setattr(
        server.lastfm, "get_similar_artists", lambda name, limit=30: []
    )


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
        StubAlbum("3", "Rec Two", "Artist C"),
        StubAlbum("4", "Rec Three", "Artist D"),
    ])
    _set_library(monkeypatch, fav_albums=[seed])
    out = server.recommendations_albums()
    assert out["enabled"] is True
    made = [s for s in out["sections"] if s["key"] == "made_for_you"]
    assert made, "Made For You section should be present"
    assert [a["id"] for a in made[0]["albums"]] == ["2", "3", "4"]
    assert made[0]["albums"][0]["reason"] == "Because you saved Seed"


# ---------------------------------------------------------------------------
# "Made For You": library-seeded discovery
#
# Two pools, each ranked by corroboration across seeds: saved albums
# through Tidal `album.similar()`, followed artists through Last.fm's
# `artist.getSimilar`. Neither pool may rank against the other, so the row
# interleaves them.
# ---------------------------------------------------------------------------


def _made(monkeypatch, floor=1, **kw):
    """Build the row and return its albums. `floor` overrides the minimum
    that keeps the row on the page, so filtering tests aren't fighting it;
    the floor itself has its own test at the real value."""
    import server

    monkeypatch.setattr(server, "_MADE_MIN_ALBUMS", floor)
    _set_library(monkeypatch, **kw)
    return server._section_made_for_you()["albums"]


def _lastfm_graph(monkeypatch, graph, top=()):
    """Point the Last.fm surface at an in-memory similar-artist graph."""
    import server

    monkeypatch.setattr(server.lastfm, "status", lambda: {"connected": True})
    monkeypatch.setattr(
        server.lastfm, "get_top_artists",
        lambda period, limit: [{"name": n} for n in top],
    )
    monkeypatch.setattr(
        server.lastfm, "get_similar_artists",
        lambda name, limit=30: graph.get(name, []),
    )


def _tidal_artists(monkeypatch, artists):
    """Resolve a search query to a stub artist by name."""
    import server

    monkeypatch.setattr(
        server.tidal, "search",
        lambda q, limit=25: (
            {"artists": [artists[q]]} if q in artists else {"artists": []}
        ),
    )


def test_owned_albums_are_filtered_out(stub_auth, monkeypatch):
    import server

    _no_lastfm(monkeypatch)
    # Seed's "similar" includes an album the user already owns (id 1) plus
    # genuinely new ones. Only the new ones should surface.
    seed = StubAlbum("1", "Owned Seed", "Artist A", similar=[
        StubAlbum("1", "Owned Seed", "Artist A"),
        StubAlbum("9", "Fresh Pick", "Artist Z"),
    ])
    ids = [a["id"] for a in _made(monkeypatch, fav_albums=[seed])]
    assert ids == ["9"]


def test_unavailable_albums_dropped(stub_auth, monkeypatch):
    import server

    _no_lastfm(monkeypatch)
    seed = StubAlbum("1", "Seed", "Artist A", similar=[
        StubAlbum("2", "Not Streamable", "Artist B", available=False),
        StubAlbum("3", "Streamable", "Artist C", available=True),
    ])
    ids = [a["id"] for a in _made(monkeypatch, fav_albums=[seed])]
    assert ids == ["3"]


def test_per_artist_cap(stub_auth, monkeypatch):
    import server

    _no_lastfm(monkeypatch)
    # Five candidate albums all by the same artist; the cap is 2.
    same_artist = [
        StubAlbum(str(i), f"Album {i}", "One Artist") for i in range(10, 15)
    ]
    seed = StubAlbum("1", "Seed", "Seed Artist", similar=same_artist)
    out = _made(monkeypatch, fav_albums=[seed])
    assert len(out) == server._RECS_PER_ARTIST_CAP


def test_saved_album_corroboration_outranks_position(stub_auth, monkeypatch):
    """An album several saves independently point at beats one that a
    single save ranks first. This is the fix for source-constant ranking:
    agreement across seeds decides placement, not a per-source base score.

    `shared` is never first in any list (0.5 x 3 = 1.5); `solo` is first in
    one and corroborated by nothing (1.0)."""
    import server

    _no_lastfm(monkeypatch)
    shared = StubAlbum("42", "Shared", "Artist X")
    solo = StubAlbum("7", "Solo", "Artist S")
    seeds = [
        StubAlbum("1", "Seed One", "A", similar=[solo, shared]),
        StubAlbum("2", "Seed Two", "B", similar=[
            StubAlbum("8", "Filler", "Artist F"), shared,
        ]),
        StubAlbum("3", "Seed Three", "C", similar=[
            StubAlbum("9", "Filler Two", "Artist G"), shared,
        ]),
    ]
    ids = [a["id"] for a in _made(monkeypatch, fav_albums=seeds)]
    assert ids.count("42") == 1, "corroborated album must not duplicate"
    assert ids[0] == "42"
    assert ids.index("42") < ids.index("7")


def test_followed_artists_resolve_through_lastfm(stub_auth, monkeypatch):
    """Followed artists reach candidates through Last.fm's listener-overlap
    graph, not Tidal's artist.get_similar()."""
    import server

    _lastfm_graph(monkeypatch, {"Fav": [{"name": "SimA", "match": 0.9}]})
    _tidal_artists(monkeypatch, {
        "SimA": StubArtist("SimA", albums=[
            StubAlbum("55", "Lastfm Pick", "SimA"),
            StubAlbum("56", "Lastfm Pick Two", "SimA"),
        ]),
    })
    out = _made(monkeypatch, fav_artists=[StubArtist("Fav")])
    assert [a["id"] for a in out] == ["55", "56"]
    assert out[0]["reason"] == "Because you follow Fav"


def test_lastfm_match_sums_across_follows(stub_auth, monkeypatch):
    """An artist several follows share outranks one a single follow points
    at, even when that single suggestion has the higher raw match.
    Shared: 0.4 x 3 = 1.2 beats Loner's lone 0.95."""
    import server

    _lastfm_graph(monkeypatch, {
        "FavOne": [{"name": "Shared", "match": 0.4}, {"name": "Loner", "match": 0.95}],
        "FavTwo": [{"name": "Shared", "match": 0.4}],
        "FavThree": [{"name": "Shared", "match": 0.4}],
    })
    _tidal_artists(monkeypatch, {
        "Shared": StubArtist("Shared", albums=[StubAlbum("1", "Shared LP", "Shared")]),
        "Loner": StubArtist("Loner", albums=[StubAlbum("2", "Loner LP", "Loner")]),
    })
    out = _made(monkeypatch, fav_artists=[
        StubArtist("FavOne"), StubArtist("FavTwo"), StubArtist("FavThree"),
    ])
    ids = [a["id"] for a in out]
    assert ids.count("1") == 1
    assert ids == ["1", "2"]


def test_artists_already_in_the_library_are_excluded(stub_auth, monkeypatch):
    """Discovery means new names. A candidate the user already follows,
    already saved, or already plays heavily is not a recommendation."""
    import server

    _lastfm_graph(
        monkeypatch,
        {"Fav": [
            {"name": "Heavy", "match": 0.9},   # already in heavy rotation
            {"name": "Fav", "match": 0.85},    # already followed
            {"name": "Saved", "match": 0.8},   # artist of a saved album
            {"name": "New", "match": 0.7},
        ]},
        top=["Heavy"],
    )
    _tidal_artists(monkeypatch, {
        "New": StubArtist("New", albums=[StubAlbum("3", "New LP", "New")]),
    })
    out = _made(
        monkeypatch,
        fav_artists=[StubArtist("Fav")],
        fav_albums=[StubAlbum("77", "Owned", "Saved")],
    )
    assert [a["id"] for a in out] == ["3"]


def test_resolved_artist_in_library_is_excluded(stub_auth, monkeypatch):
    """Tidal search can collapse a distinct name back onto a library artist
    (e.g. "John Mayer Trio" -> John Mayer). Judge the resolved artist, not
    the candidate string, so only the genuinely new name survives."""
    import server

    _lastfm_graph(monkeypatch, {"Fav": [
        {"name": "Fav Trio", "match": 0.95},
        {"name": "Real Find", "match": 0.6},
    ]})
    _tidal_artists(monkeypatch, {
        # Different candidate name, resolves back to the followed artist.
        "Fav Trio": StubArtist("Fav", albums=[StubAlbum("9", "Nope", "Fav")]),
        "Real Find": StubArtist(
            "Real Find", albums=[StubAlbum("10", "Yes", "Real Find")]
        ),
    })
    out = _made(monkeypatch, fav_artists=[StubArtist("Fav")])
    assert [a["id"] for a in out] == ["10"]


def test_pools_interleave(stub_auth, monkeypatch):
    """Saved-album and followed-artist picks measure different things, so
    the row alternates between them rather than letting one pool's scale
    dominate the top of the shelf."""
    import server

    _lastfm_graph(monkeypatch, {"Fav": [{"name": "SimA", "match": 0.9}]})
    _tidal_artists(monkeypatch, {
        "SimA": StubArtist("SimA", albums=[StubAlbum("a1", "Artist Pick", "SimA")]),
    })
    seed = StubAlbum("1", "Seed", "Seed Artist", similar=[
        StubAlbum("b1", "Album Pick One", "Artist B"),
        StubAlbum("b2", "Album Pick Two", "Artist C"),
    ])
    out = _made(monkeypatch, fav_albums=[seed], fav_artists=[StubArtist("Fav")])
    # One from each pool, then the deeper pool's remainder.
    assert [a["id"] for a in out] == ["a1", "b1", "b2"]
    assert out[0]["reason"] == "Because you follow Fav"
    assert out[1]["reason"] == "Because you saved Seed"


def test_row_drops_when_too_thin(stub_auth, monkeypatch):
    """Under the floor the shelf looks broken, so the row drops out instead
    of rendering a stub. Exercised at the real floor value."""
    import server

    _no_lastfm(monkeypatch)
    assert server._MADE_MIN_ALBUMS == 3
    picks = [StubAlbum("2", "One", "B"), StubAlbum("3", "Two", "C")]
    thin = StubAlbum("1", "Seed", "A", similar=picks)
    assert _made(monkeypatch, floor=3, fav_albums=[thin]) == []
    # One more candidate clears the floor.
    full = StubAlbum("1", "Seed", "A", similar=picks + [StubAlbum("4", "Three", "D")])
    assert len(_made(monkeypatch, floor=3, fav_albums=[full])) == 3


def test_reissues_collapsed_by_title_and_artist(stub_auth, monkeypatch):
    """Same album under two Tidal ids (deluxe/explicit/regional) should
    appear once — the id-dedupe can't catch it, the title+artist one does."""
    import server

    _no_lastfm(monkeypatch)
    seed = StubAlbum("1", "Seed", "A", similar=[
        StubAlbum("100", "Power Ballad", "Same Artist"),
        StubAlbum("200", "power ballad", "Same Artist"),  # reissue, other id
    ])
    out = _made(monkeypatch, fav_albums=[seed])
    assert [a["id"] for a in out] == ["100"]


def test_no_library_yields_no_row(stub_auth, monkeypatch):
    """With nothing saved and nobody followed there is no seed, so the row
    drops rather than falling back to listening history — that is what
    "Fans Also Like" is for."""
    import server

    _no_lastfm(monkeypatch)
    assert _made(monkeypatch) == []


def test_similar_failure_is_non_fatal(stub_auth, monkeypatch):
    """A seed whose similar() blows up must not sink the row."""
    import server

    _no_lastfm(monkeypatch)

    class Exploding(StubAlbum):
        def similar(self):
            raise RuntimeError("tidal similar 500")

    bad = Exploding("1", "Bad Seed", "A")
    good = StubAlbum("2", "Good Seed", "B", similar=[
        StubAlbum("9", "Still Here", "C"),
        StubAlbum("10", "Also Here", "D"),
        StubAlbum("11", "And Here", "E"),
    ])
    ids = [a["id"] for a in _made(monkeypatch, fav_albums=[bad, good])]
    assert ids == ["9", "10", "11"]


# ---------------------------------------------------------------------------
# Tidal's own "For You" album modules, each rendered as its own section
# ---------------------------------------------------------------------------


def test_for_you_album_modules_harvested(stub_auth, monkeypatch):
    import server

    # An album module (harvested, labelled by title + subtitle) alongside a
    # non-album item, which is ignored.
    page = StubPage([
        StubCategory(
            "Because you listened to",
            [StubAlbum(str(i), f"Native {i}", f"Artist {i}") for i in range(4)]
            + [object()],
            subtitle="Radiohead",
        ),
    ])
    monkeypatch.setattr(server.tidal.session, "for_you", lambda: page)
    sections = server._for_you_album_sections()
    assert len(sections) == 1
    assert sections[0]["title"] == "Because you listened to Radiohead"
    assert [a["id"] for a in sections[0]["albums"]] == ["0", "1", "2", "3"]


def test_for_you_thin_modules_skipped(stub_auth, monkeypatch):
    """Fewer than four albums is a broken-looking shelf, so the module is
    skipped rather than rendered."""
    import server

    page = StubPage([
        StubCategory("Suggested new albums", [StubAlbum("1", "Only One", "A")]),
    ])
    monkeypatch.setattr(server.tidal.session, "for_you", lambda: page)
    assert server._for_you_album_sections() == []


def test_for_you_failure_is_non_fatal(stub_auth, monkeypatch):
    """A broken For You fetch yields no sections rather than raising."""
    import server

    def _boom():
        raise RuntimeError("tidal for_you 500")

    monkeypatch.setattr(server.tidal.session, "for_you", _boom)
    assert server._for_you_album_sections() == []


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
    monkeypatch.setattr(server.lastfm, "get_chart_top_artists", lambda limit=500: [])
    prof = server._taste_profile()
    assert prof["connected"] is True
    assert "boards of canada" in prof["artist_names"]
    slugs = [g[0] for g in prof["genres"]]
    assert slugs == ["7-electronic"]  # IDM has no AOTY genre -> dropped


def test_mainstream_ratio_from_chart_overlap(stub_auth, monkeypatch):
    import server

    monkeypatch.setattr(server.lastfm, "status", lambda: {"connected": True})
    monkeypatch.setattr(
        server.lastfm, "get_top_artists",
        lambda period, limit: [{"name": "Taylor Swift"}, {"name": "Some Nobody"}],
    )
    monkeypatch.setattr(server.lastfm, "get_artist_top_tags", lambda name, limit=4: [])
    monkeypatch.setattr(server, "_aoty_genre_slug_map", lambda: {})
    monkeypatch.setattr(
        server.lastfm, "get_chart_top_artists",
        lambda limit=500: [{"name": "Taylor Swift"}],  # 1 of 2 is mainstream
    )
    prof = server._taste_profile()
    assert prof["mainstream_ratio"] == 0.5


def test_obscure_listener_weights_genre_over_popularity(stub_auth, monkeypatch):
    import server

    _resolver_passthrough(monkeypatch)
    listing = [
        {"id": "hit", "title": "Chart Hit", "artist": "A", "score": 95, "genre_slugs": ["9-pop"]},
        {"id": "niche", "title": "Genre Fit", "artist": "B", "score": 60, "genre_slugs": ["26-shoegaze"]},
    ]
    niche = {"genres": [("26-shoegaze", "Shoegaze", 1.0)], "mainstream_ratio": 0.0}
    mainstream = {"genres": [("26-shoegaze", "Shoegaze", 1.0)], "mainstream_ratio": 1.0}
    niche_ids = [a["id"] for a in server._aoty_section("k", "T", "s", listing, niche, True)["albums"]]
    main_ids = [a["id"] for a in server._aoty_section("k", "T", "s", listing, mainstream, True)["albums"]]
    assert niche_ids[0] == "niche"  # obscure taste -> genre fit wins
    assert main_ids[0] == "hit"     # mainstream taste -> popularity wins


def test_deep_mode_favors_lesser_known(stub_auth, monkeypatch):
    import server

    _resolver_passthrough(monkeypatch)
    profile = {"genres": [("26-shoegaze", "Shoegaze", 1.0)], "mainstream_ratio": 0.0}
    listing = [
        {"id": "canon", "title": "Canon", "artist": "A", "score": 98, "genre_slugs": ["26-shoegaze"]},
        {"id": "gem", "title": "Hidden Gem", "artist": "B", "score": 72, "genre_slugs": ["26-shoegaze"]},
    ]
    ids = [a["id"] for a in server._aoty_section("k", "T", "s", listing, profile, False, deep=True)["albums"]]
    assert ids[0] == "gem"  # both in-genre; deep mode prefers the lower-popularity one


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
    # Enough picks to clear the row's minimum, or it drops off the page.
    seed = StubAlbum("1", "Seed", "SA", similar=[
        StubAlbum("m1", "Made Pick", "MB"),
        StubAlbum("m2", "Made Pick Two", "MC"),
        StubAlbum("m3", "Made Pick Three", "MD"),
    ])
    _set_library(monkeypatch, fav_albums=[seed])

    sections = server._build_recommendation_sections()
    # Genre rows absent (no inferred genres); order is made -> new -> popular.
    assert [s["key"] for s in sections] == ["made_for_you", "new_releases", "popular"]
    made = next(s for s in sections if s["key"] == "made_for_you")
    assert [a["id"] for a in made["albums"]] == ["m1", "m2", "m3"]


# ---------------------------------------------------------------------------
# "Not interested" feedback loop
# ---------------------------------------------------------------------------


def test_filter_dismissed_drops_by_id():
    import server

    albums = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    assert [a["id"] for a in server._filter_dismissed(albums, {"2"})] == ["1", "3"]


def test_dismissed_album_excluded_from_core(stub_auth, monkeypatch):
    import server

    _no_lastfm(monkeypatch)
    monkeypatch.setattr(
        server, "_load_rec_feedback", lambda: {"albums": {"9"}, "artists": set()}
    )
    seed = StubAlbum("1", "Seed", "A", similar=[
        StubAlbum("9", "Dismissed", "Z"),
        StubAlbum("8", "Kept", "Q"),
    ])
    assert [a["id"] for a in _made(monkeypatch, fav_albums=[seed])] == ["8"]


def test_dismissed_artist_excluded(stub_auth, monkeypatch):
    """"Not interested" on an album drops that artist's other picks from
    the row entirely rather than merely nudging them down the order."""
    import server

    _no_lastfm(monkeypatch)
    monkeypatch.setattr(
        server, "_load_rec_feedback", lambda: {"albums": set(), "artists": {"artist z"}}
    )
    seed = StubAlbum("1", "Seed", "S", similar=[
        StubAlbum("2", "By Z", "Artist Z"),
        StubAlbum("3", "By Q", "Artist Q"),
    ])
    assert [a["id"] for a in _made(monkeypatch, fav_albums=[seed])] == ["3"]


def test_dismissed_artist_excluded_from_followed_pool(stub_auth, monkeypatch):
    """The same exclusion applies to the Last.fm side of the row."""
    import server

    monkeypatch.setattr(
        server, "_load_rec_feedback", lambda: {"albums": set(), "artists": {"nope"}}
    )
    _lastfm_graph(monkeypatch, {"Fav": [
        {"name": "Nope", "match": 0.9},
        {"name": "Yes", "match": 0.5},
    ]})
    _tidal_artists(monkeypatch, {
        "Nope": StubArtist("Nope", albums=[StubAlbum("1", "No", "Nope")]),
        "Yes": StubArtist("Yes", albums=[StubAlbum("2", "Yes", "Yes")]),
    })
    out = _made(monkeypatch, fav_artists=[StubArtist("Fav")])
    assert [a["id"] for a in out] == ["2"]


def test_dismiss_endpoint_records(stub_auth, monkeypatch):
    import server

    calls = []
    monkeypatch.setattr(
        server, "_dismiss_recommendation",
        lambda album_id, artist: calls.append((album_id, artist)),
    )
    out = server.recommendations_dismiss(
        server.DismissRecommendationRequest(album_id="55", artist="Some Artist")
    )
    assert out == {"ok": True}
    assert calls == [("55", "Some Artist")]


def test_dismiss_persists_and_reloads(stub_auth, monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "_rec_feedback_file", lambda: tmp_path / "fb.json")
    server._dismiss_recommendation("100", "Cool Band")
    fb = server._load_rec_feedback()
    assert "100" in fb["albums"]
    assert "cool band" in fb["artists"]


# ---------------------------------------------------------------------------
# Listener-overlap discovery ("Fans Also Like")
# ---------------------------------------------------------------------------


def test_fans_also_like_builds_from_similar(stub_auth, monkeypatch):
    import server

    monkeypatch.setattr(
        server.lastfm, "get_top_artists",
        lambda period, limit: [{"name": "Boards of Canada"}],
    )
    monkeypatch.setattr(
        server.lastfm, "get_similar_artists",
        lambda name, limit=15: [
            {"name": "Bibio", "match": 0.9},
            {"name": "Tycho", "match": 0.7},
        ],
    )

    def _search(q, limit=25):
        return {"artists": [StubArtist(q, albums=[
            StubAlbum(f"{q}-1", f"{q} LP", q),
            StubAlbum(f"{q}-2", f"{q} EP", q),
        ])]}

    monkeypatch.setattr(server.tidal, "search", _search)
    sec = server._section_fans_also_like()
    assert sec["title"] == "Fans Also Like"
    assert len(sec["albums"]) >= 3
    # Every pick is attributed to the seed whose fans overlap.
    assert sec["albums"][0]["reason"] == "Fans of Boards of Canada also like"


def test_fans_also_like_excludes_own_top_artists(stub_auth, monkeypatch):
    """A similar-artist result that's already one of the user's top artists
    is discovery noise — don't recommend back what they already play."""
    import server

    monkeypatch.setattr(
        server.lastfm, "get_top_artists",
        lambda period, limit: [{"name": "Aphex Twin"}, {"name": "Bibio"}],
    )
    # Aphex Twin's fans also like Bibio — but Bibio is a top artist already.
    monkeypatch.setattr(
        server.lastfm, "get_similar_artists",
        lambda name, limit=15: (
            [{"name": "Bibio", "match": 0.9}] if name == "Aphex Twin" else []
        ),
    )
    searched = []

    def _search(q, limit=25):
        searched.append(q)
        return {"artists": [StubArtist(q, albums=[StubAlbum(f"{q}-1", f"{q} LP", q)])]}

    monkeypatch.setattr(server.tidal, "search", _search)
    sec = server._section_fans_also_like()
    assert "Bibio" not in searched
    assert sec["albums"] == []


def test_fans_also_like_excludes_resolved_own_artist(stub_auth, monkeypatch):
    """Tidal search can collapse a distinct similar-artist name onto an
    artist the user already plays (e.g. "John Mayer Trio" -> John Mayer).
    The exclusion must look at the *resolved* artist, not just the candidate
    name, or the row hands back more of what they already listen to."""
    import server

    monkeypatch.setattr(
        server.lastfm, "get_top_artists",
        lambda period, limit: [{"name": "John Mayer"}],
    )
    monkeypatch.setattr(
        server.lastfm, "get_similar_artists",
        lambda name, limit=15: [{"name": "John Mayer Trio", "match": 0.8}],
    )
    # Searching the candidate resolves back to the seed artist himself.
    monkeypatch.setattr(
        server.tidal, "search",
        lambda q, limit=25: {"artists": [
            StubArtist("John Mayer", albums=[StubAlbum("9", "Sob Rock", "John Mayer")])
        ]},
    )
    sec = server._section_fans_also_like()
    assert sec["albums"] == []


def test_fans_also_like_empty_when_no_overlap(stub_auth, monkeypatch):
    import server

    monkeypatch.setattr(
        server.lastfm, "get_top_artists",
        lambda period, limit: [{"name": "Brand New Artist"}],
    )
    monkeypatch.setattr(
        server.lastfm, "get_similar_artists", lambda name, limit=15: []
    )
    sec = server._section_fans_also_like()
    assert sec["albums"] == []
