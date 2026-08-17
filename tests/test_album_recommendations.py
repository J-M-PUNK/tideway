"""Tests for the "For You" album-recommendations endpoint (#307).

The page is assembled from AOTY genre listings, AOTY's year chart, and
Last.fm's similar-artist graph. The risky parts are the ordering rules —
excluding saved albums before a row is capped, spanning genres and seeds
rather than letting one dominate, and the blended top row — so those get
the coverage, with the upstream surfaces stubbed.
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
    # "Fans Also Like" reads the top artists and the similar-artist graph
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
    monkeypatch.setattr(
        server.aoty_module, "recent_releases_by_genre", lambda slug, limit=60: []
    )
    monkeypatch.setattr(
        server.aoty_module, "top_albums_by_genre", lambda slug, limit=60: []
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


def test_favorited_albums_are_filtered_from_every_section(stub_auth, monkeypatch):
    """Saved albums must not come back as recommendations in any row.

    Originally only _album_recommendations excluded them, so the
    AOTY-backed rows handed the listener their own library — 11 of 18 in
    "Popular in Your Orbit" on a real profile. Every builder now excludes
    them through _dedupe_cap_albums, ahead of the row being capped."""
    import server

    monkeypatch.setattr(
        server.settings, "album_recommendations_enabled", True, raising=False
    )
    _no_lastfm(monkeypatch)
    _no_aoty(monkeypatch)

    owned = StubAlbum("99", "Already Saved", "Artist Z")
    _set_library(monkeypatch, fav_albums=[owned])

    # An AOTY-backed row offering the saved album plus a fresh one. It
    # runs its candidates through the shared tail exactly as the real
    # builders do, which is where the exclusion happens.
    def _fake_section(key, title, subtitle, listing, profile, taste_rank,
                      deep=False, owned=None, rotate=False):
        return {
            "key": key, "title": title, "subtitle": subtitle,
            "albums": server._dedupe_cap_albums(
                [
                    {"id": "99", "name": "Already Saved",
                     "artists": [{"name": "Artist Z"}], "available": True},
                    {"id": "100", "name": "Brand New",
                     "artists": [{"name": "Artist Y"}], "available": True},
                ],
                server._SECTION_SIZE, exclude=owned,
            ),
        }

    monkeypatch.setattr(server, "_aoty_section", _fake_section)
    out = server.recommendations_albums()
    served = [a["id"] for s in out["sections"] for a in s["albums"]]
    assert "99" not in served, "a saved album was recommended back to the user"
    assert "100" in served, "the unsaved album should still be offered"


def test_enabled_returns_genre_rows(stub_auth, monkeypatch):
    """The page is genre-scoped now: "New Releases For You" and "From Your
    Genres" both blend the listener's genres rather than re-ranking a
    global chart, and neither is restricted to the current year."""
    import server

    monkeypatch.setattr(
        server.settings, "album_recommendations_enabled", True, raising=False
    )
    _no_lastfm(monkeypatch)
    _no_aoty(monkeypatch)
    monkeypatch.setattr(
        server, "_taste_profile",
        lambda: {"connected": True, "artist_names": set(),
                 "genres": [("26-shoegaze", "Shoegaze", 1.0)],
                 "mainstream_ratio": 0.5},
    )
    monkeypatch.setattr(
        server.aoty_module, "top_albums_by_genre",
        lambda slug, limit=60: [{"artist": "Slowdive", "title": "Souvlaki"}],
    )
    monkeypatch.setattr(
        server.aoty_resolver, "resolve_listing",
        lambda listing: [
            {**it, "tidal_album": {"id": "7", "name": it["title"],
                                   "artists": [{"name": it["artist"]}],
                                   "available": True}}
            for it in listing
        ],
    )
    _set_library(monkeypatch)
    out = server.recommendations_albums()
    keys = [s["key"] for s in out["sections"]]
    assert "from_your_genres" in keys
    assert "made_for_you" not in keys
    assert not any(k.startswith("genre:") for k in keys), "year-scoped rows are gone"
    assert "deep_cuts" not in keys


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
    monkeypatch.setattr(
        server.aoty_module, "recent_releases_by_genre", lambda slug, limit=60: []
    )
    monkeypatch.setattr(
        server.aoty_module, "top_albums_by_genre", lambda slug, limit=60: []
    )
    _set_library(monkeypatch)

    sections = server._build_recommendation_sections()
    # No inferred genres, so both genre-blended rows come back empty and
    # drop; only the year-chart row survives.
    assert [s["key"] for s in sections] == ["popular"]


# ---------------------------------------------------------------------------
# Listener-overlap discovery ("Fans Also Like")
# ---------------------------------------------------------------------------


def test_fans_also_like_spans_seeds_when_one_floods(stub_auth, monkeypatch):
    """A seed sitting in a dense Last.fm neighbourhood (a game soundtrack,
    say) has every neighbour scoring high, so ranking purely by summed
    match let it fill most of the shelf. Picks must span the seeds."""
    import server

    monkeypatch.setattr(
        server.lastfm, "get_top_artists",
        lambda period, limit: [{"name": "Dense"}, {"name": "Sparse"}],
    )

    def _sims(name, limit=15):
        if name == "Dense":
            # Ten tightly-clustered neighbours, all scoring above Sparse's.
            return [{"name": f"D{i}", "match": 0.99} for i in range(10)]
        return [{"name": "S1", "match": 0.5}, {"name": "S2", "match": 0.4}]

    monkeypatch.setattr(server.lastfm, "get_similar_artists", _sims)
    monkeypatch.setattr(
        server.tidal, "search",
        lambda q, limit=25: {"artists": [
            StubArtist(q, albums=[StubAlbum(f"{q}-1", f"{q} LP", q)])
        ]},
    )

    sec = server._section_fans_also_like()
    seeds = [a["reason"] for a in sec["albums"]]
    sparse = [r for r in seeds if "Sparse" in r]
    # Ranking by score alone would put all ten Dense picks first and bury
    # Sparse entirely; the round-robin must surface it near the top.
    assert sparse, f"Sparse seed was crowded out entirely: {seeds}"
    assert "Sparse" in seeds[1], f"expected seeds to alternate, got {seeds[:4]}"


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


# ---------------------------------------------------------------------------
# Blended top row
# ---------------------------------------------------------------------------


def _row(key, title, ids):
    return {
        "key": key, "title": title, "subtitle": "",
        "albums": [
            {"id": i, "name": f"{i} album", "artists": [{"name": f"{i} artist"}]}
            for i in ids
        ],
    }


def test_blend_round_robins_across_rows():
    """No source may dominate. A row with many candidates must not crowd
    out a thinner one — that was the failure the seed round-robin fixed
    in Fans Also Like, and the same rule applies across rows."""
    import server

    rows = [
        _row("big", "Big Row", [f"b{i}" for i in range(10)]),
        _row("small", "Small Row", ["s1", "s2"]),
    ]
    blend = server._blend_top_row(rows)
    reasons = [a["reason"] for a in blend["albums"]]
    # First two picks alternate; the thin row is represented up front
    # rather than buried behind ten from the big row.
    assert reasons[0] == "Big Row"
    assert reasons[1] == "Small Row"
    assert reasons.count("Small Row") == 2


def test_blend_keeps_existing_reasons():
    """A card that already explains itself keeps its own wording; only
    rows whose albums carry no reason fall back to the row title."""
    import server

    rows = [_row("a", "Row A", ["1", "3", "5"]), _row("b", "Row B", ["2", "4", "6"])]
    rows[0]["albums"][0]["reason"] = "Fans of Someone also like"
    blend = server._blend_top_row(rows)
    by_id = {a["id"]: a["reason"] for a in blend["albums"]}
    assert by_id["1"] == "Fans of Someone also like"
    assert by_id["2"] == "Row B"


def test_blend_removes_its_picks_from_source_rows():
    """The same album must not appear twice on one page."""
    import server

    rows = [_row("a", "Row A", ["1", "2"]), _row("b", "Row B", ["3", "4"])]
    blend = server._blend_top_row(rows)
    blended = {a["id"] for a in blend["albums"]}
    remaining = {a["id"] for r in rows for a in r["albums"]}
    assert not (blended & remaining), "an album is on both the blend and its row"


def test_blend_skipped_when_only_one_row():
    """Blending one row just renames it, so don't."""
    import server

    assert server._blend_top_row([_row("a", "Row A", ["1", "2", "3"])]) is None


def test_blend_dedupes_across_rows():
    """An album offered by two sources appears once, attributed to
    whichever row reached it first."""
    import server

    rows = [
        _row("a", "Row A", ["dup", "1", "3"]),
        _row("b", "Row B", ["dup", "2", "4"]),
    ]
    blend = server._blend_top_row(rows)
    ids = [a["id"] for a in blend["albums"]]
    assert ids.count("dup") == 1


def test_owned_filter_runs_before_truncation():
    """Excluding saved albums after truncating spends the shelf's slots on
    albums that are then discarded. On a real library that left "Popular
    in Your Orbit" showing 3 of 18 — the chart was mostly albums already
    saved. The exclusion has to happen before the row is capped."""
    import server

    # 20 candidates, the first 15 already saved. A row of 5 can still be
    # filled entirely from the tail.
    albums = [
        {"id": str(i), "name": f"Album {i}", "artists": [{"name": f"Artist {i}"}]}
        for i in range(20)
    ]
    owned = {str(i) for i in range(15)}
    out = server._dedupe_cap_albums(albums, 5, exclude=owned)
    assert len(out) == 5, "row should fill from the unowned tail"
    assert all(a["id"] not in owned for a in out)


def test_owned_filter_is_optional():
    """Callers without an exclusion set get the old behaviour."""
    import server

    albums = [{"id": "1", "name": "A", "artists": [{"name": "X"}]}]
    assert len(server._dedupe_cap_albums(albums, 5)) == 1


# ---------------------------------------------------------------------------
# Recency blending, tag specificity, and weekly rotation
# ---------------------------------------------------------------------------


def test_seed_artists_blend_recent_over_long_window(stub_auth, monkeypatch):
    """A single six-month window left the page effectively frozen. Recent
    listening has to outweigh the long window without erasing it."""
    import server

    windows = {
        "1month": [{"name": "NewObsession", "playcount": 100}],
        "6month": [{"name": "OldFavourite", "playcount": 1000},
                   {"name": "NewObsession", "playcount": 50}],
    }
    monkeypatch.setattr(
        server.lastfm, "get_top_artists", lambda p, limit: windows.get(p, [])
    )
    names = [e["name"] for e in server._seed_artists()]
    # Raw play counts would rank OldFavourite far ahead; normalising each
    # window against its own top artist is what lets the recent one lead.
    assert names[0] == "NewObsession"
    assert "OldFavourite" in names, "the long window is ballast, not discarded"


def test_seed_artists_survive_a_missing_window(stub_auth, monkeypatch):
    import server

    monkeypatch.setattr(
        server.lastfm, "get_top_artists",
        lambda p, limit: [{"name": "Only", "playcount": 5}] if p == "6month" else [],
    )
    assert [e["name"] for e in server._seed_artists()] == ["Only"]


def test_broad_tags_lose_to_specific_ones(stub_auth, monkeypatch):
    """Tag strength and breadth rise together, so ranking on strength alone
    just recovers whichever vague tag every artist carries. Discounting by
    global reach is what surfaces the sub-genre."""
    import server

    monkeypatch.setattr(server.lastfm, "status", lambda: {"connected": True})
    monkeypatch.setattr(
        server.lastfm, "get_top_artists",
        lambda p, limit: [{"name": "BoC", "playcount": 100}] if p == "1month" else [],
    )
    # "electronic" is tagged nearly as strongly as "idm" but applied to
    # vastly more artists globally.
    monkeypatch.setattr(
        server.lastfm, "get_artist_top_tags",
        lambda name, limit=8: [
            {"name": "electronic", "count": 95},
            {"name": "idm", "count": 90},
        ],
    )
    monkeypatch.setattr(
        server.lastfm, "get_chart_top_tags",
        lambda limit=250: [
            {"name": "electronic", "reach": 262672},
            {"name": "idm", "reach": 39205},
        ],
    )
    monkeypatch.setattr(server.lastfm, "get_chart_top_artists", lambda limit=500: [])
    monkeypatch.setattr(
        server, "_aoty_genre_slug_map",
        lambda: {"electronic": ("6-electronic", "Electronic"),
                 "idm": ("47-idm", "IDM")},
    )
    genres = [name for _slug, name, _w in server._taste_profile()["genres"]]
    assert genres[0] == "IDM", f"broad tag won: {genres}"


def test_rotation_advances_weekly_and_holds_within_a_week(monkeypatch):
    import server

    pool = [f"a{i}" for i in range(60)]
    base = 1_000_000_000.0
    monkeypatch.setattr(server.time, "time", lambda: base)
    first = server._rotate(pool, 18)
    assert server._rotate(pool, 18) == first, "must not shift between calls"

    monkeypatch.setattr(server.time, "time", lambda: base + 7 * 86400)
    later = server._rotate(pool, 18)
    assert later != first, "row is frozen week to week"
    carried = len(set(later) & set(first))
    # Partial overlap: a full swap gives the row no continuity, none at all
    # means nothing carries over.
    assert 0 < carried < 18, f"expected partial turnover, carried {carried}"


def test_rotation_wraps_so_the_tail_is_reachable(monkeypatch):
    """Without wrapping, entries near the end of a pool would only surface
    in the weeks the offset happened to land there — and the last few
    never at all."""
    import server

    pool = [f"a{i}" for i in range(20)]
    seen = set()
    for week in range(12):
        monkeypatch.setattr(
            server.time, "time", lambda w=week: 1_000_000_000.0 + w * 7 * 86400
        )
        seen.update(server._rotate(pool, 6))
    assert seen == set(pool), f"unreachable entries: {sorted(set(pool) - seen)}"


def test_rotation_handles_pools_smaller_than_the_window(monkeypatch):
    import server

    monkeypatch.setattr(server.time, "time", lambda: 1_000_000_000.0)
    assert server._rotate(["a", "b"], 18) == ["a", "b"]
    assert server._rotate([], 18) == []


def test_reach_discount_is_steep_enough_to_beat_tag_strength(stub_auth, monkeypatch):
    """A logarithmic discount separated the broadest tag from an unlisted
    one by only ~1.5x, which a broad tag's higher count simply cancelled —
    so "rock" and "electronic" kept winning over the sub-genre. The
    discount has to outweigh that gap, not merely exist.
    """
    import server

    monkeypatch.setattr(server.lastfm, "status", lambda: {"connected": True})
    monkeypatch.setattr(
        server.lastfm, "get_top_artists",
        lambda p, limit: [{"name": "A", "playcount": 10}] if p == "1month" else [],
    )
    # The broad tag is applied *more* strongly than the specific one, which
    # is the realistic case and the one a weak discount gets wrong.
    monkeypatch.setattr(
        server.lastfm, "get_artist_top_tags",
        # Both are defining tags for this artist, so the solo-tag rule
        # keeps them and the reach discount is what decides the order.
        lambda name, limit=12: [
            {"name": "rock", "count": 100},
            {"name": "dariacore", "count": 90},
        ],
    )
    monkeypatch.setattr(
        server.lastfm, "get_chart_top_tags",
        lambda limit=250: [{"name": "rock", "reach": 403263}],
    )
    monkeypatch.setattr(server.lastfm, "get_chart_top_artists", lambda limit=500: [])
    monkeypatch.setattr(
        server, "_aoty_genre_slug_map",
        lambda: {"rock": ("7-rock", "Rock"), "dariacore": ("999-dariacore", "Dariacore")},
    )
    names = [n for _s, n, _w in server._taste_profile()["genres"]]
    assert names[0] == "Dariacore", f"broad tag still won: {names}"


def test_profile_ranks_deeper_than_any_row_shows(stub_auth, monkeypatch):
    """The profile is pure tag arithmetic with no fetches, so it ranks well
    past what the rows use. The main page and the drill-down then take the
    slice each can afford — keeping that budget decision out of the
    inference."""
    import server

    assert server._TASTE_GENRE_MAX > server._GENRE_HUB_MAX > server._GENRE_BLEND_SEEDS


def test_genre_page_reports_listing_offset_not_rendered_count(stub_auth, monkeypatch):
    """Paging has to advance by listing positions consumed.

    A window of N rarely renders N — saved albums and the per-artist cap
    take their cut — so a client paging by what it rendered re-requests
    rows the previous page already consumed, and "load more" crawls.
    """
    import server

    listing = [{"artist": f"A{i}", "title": f"T{i}"} for i in range(40)]
    monkeypatch.setattr(
        server.aoty_module, "top_albums_by_genre", lambda slug, limit=60: listing[:limit]
    )
    # Every other entry resolves; the rest drop out, so rendered < window.
    monkeypatch.setattr(
        server.aoty_resolver, "resolve_listing",
        lambda rows: [
            {**r, "tidal_album": (
                {"id": r["title"], "name": r["title"],
                 "artists": [{"name": r["artist"]}], "available": True}
                if int(r["title"][1:]) % 2 == 0 else None
            )}
            for r in rows
        ],
    )
    monkeypatch.setattr(server, "_rotation_offset", lambda *a, **kw: 0)

    page = server._genre_page("26-shoegaze", "Shoegaze", 0, 12)
    assert len(page["albums"]) < 12, "fixture should render fewer than the window"
    # The next page starts where this one stopped consuming, not where it
    # stopped rendering.
    assert page["next_offset"] == 12


def test_genre_drilldown_rotates_between_weeks(stub_auth, monkeypatch):
    """The drill-down was a fixed all-time ranking: the same album led a
    genre forever, which is exactly the staleness the main page rotation
    addressed but never reached this surface."""
    import server

    listing = [{"artist": f"A{i}", "title": f"T{i}"} for i in range(25)]
    monkeypatch.setattr(
        server.aoty_module, "top_albums_by_genre", lambda slug, limit=60: listing[:limit]
    )
    monkeypatch.setattr(
        server.aoty_resolver, "resolve_listing",
        lambda rows: [
            {**r, "tidal_album": {"id": r["title"], "name": r["title"],
                                  "artists": [{"name": r["artist"]}], "available": True}}
            for r in rows
        ],
    )
    base = 1_000_000_000.0
    monkeypatch.setattr(server.time, "time", lambda: base)
    week0 = [a["name"] for a in server._genre_page("26-x", "X", 0, 12)["albums"]]
    monkeypatch.setattr(server.time, "time", lambda: base + 7 * 86400)
    week1 = [a["name"] for a in server._genre_page("26-x", "X", 0, 12)["albums"]]
    assert week0[0] != week1[0], "genre still leads with the same album every week"


# ---------------------------------------------------------------------------
# Taste ranking inside a genre
# ---------------------------------------------------------------------------


def _listing_row(artist, score, votes=1000):
    return {"artist": artist, "title": f"{artist} LP", "score": score,
            "rating_count": votes}


def test_orbit_artists_outrank_strangers_at_similar_quality():
    """Without this a genre row is the same chart for everyone who shares
    the genre — the listener's own listening never enters into it."""
    import server

    rows = [_listing_row("Stranger", 88), _listing_row("InOrbit", 86)]
    ranked = server._rank_by_taste(rows, {"inorbit": 1.0}, set())
    assert ranked[0]["artist"] == "InOrbit"


def test_thin_rating_counts_do_not_beat_well_rated_albums():
    """AOTY lets a 95 from eleven people sit above an 88 from thousands.
    That's noise outranking evidence, so scores shrink toward a prior
    until the vote count earns them."""
    import server

    rows = [_listing_row("Obscure", 95, votes=11), _listing_row("Established", 88, votes=3000)]
    ranked = server._rank_by_taste(rows, {}, set())
    assert ranked[0]["artist"] == "Established"


def test_already_played_artists_are_demoted():
    """A row of albums by artists already in heavy rotation is a mirror,
    not a recommendation."""
    import server

    rows = [_listing_row("Played", 90), _listing_row("Fresh", 88)]
    orbit = {"played": 1.0, "fresh": 1.0}
    ranked = server._rank_by_taste(rows, orbit, {"played"})
    assert ranked[0]["artist"] == "Fresh"


def test_seeds_are_not_boosted_into_their_own_orbit(stub_auth, monkeypatch):
    """Adding the seeds to their own orbit put a listener's most-played
    artist at the head of every genre row that artist appears in."""
    import server

    monkeypatch.setattr(
        server.lastfm, "get_similar_artists", lambda name, limit=50: []
    )
    orbit = server._listening_orbit([{"name": "TopArtist", "score": 5.0}])
    assert "topartist" not in orbit, "seed boosted itself into its own orbit"


def test_orbit_is_normalised_so_weights_stay_meaningful():
    """Affinities scale with how heavily seeds are played; without
    normalising, the orbit term would swamp the others for a heavy
    listener and vanish for a light one."""
    import server

    class _LF:
        @staticmethod
        def get_similar_artists(name, limit=50):
            return [{"name": "Neighbour", "match": 0.9}]

    import types
    orig = server.lastfm
    server.lastfm = types.SimpleNamespace(get_similar_artists=_LF.get_similar_artists)
    try:
        light = server._listening_orbit([{"name": "A", "score": 0.1}])
        heavy = server._listening_orbit([{"name": "A", "score": 900.0}])
    finally:
        server.lastfm = orig
    assert max(light.values()) == max(heavy.values()) == 1.0


def test_ranking_survives_missing_scores():
    """AOTY ships rows with no score during release week."""
    import server

    rows = [{"artist": "NoScore", "title": "X"}, _listing_row("Scored", 80)]
    assert len(server._rank_by_taste(rows, {}, set())) == 2


def test_one_artists_passing_tag_is_not_a_genre(stub_auth, monkeypatch):
    """A tag carried by a single artist, and not even that artist's
    defining one, was enough to put a genre on the page. Quadeca is
    tagged "emo rap" at 59 out of 100 — a footnote next to their "art
    pop" and "folktronica" at 100 — and that alone made emo rap a genre
    for someone who doesn't listen to it. Worse, the niche bonus favours
    exactly these rarely-used tags.
    """
    import server

    monkeypatch.setattr(server.lastfm, "status", lambda: {"connected": True})
    monkeypatch.setattr(
        server.lastfm, "get_top_artists",
        lambda p, limit: [{"name": "Quadeca", "playcount": 100}] if p == "1month" else [],
    )
    monkeypatch.setattr(
        server.lastfm, "get_artist_top_tags",
        lambda name, limit=12: [
            {"name": "art pop", "count": 100},
            {"name": "emo rap", "count": 59},
        ],
    )
    monkeypatch.setattr(server.lastfm, "get_chart_top_tags", lambda limit=250: [])
    monkeypatch.setattr(server.lastfm, "get_chart_top_artists", lambda limit=500: [])
    monkeypatch.setattr(server.lastfm, "get_similar_artists", lambda n, limit=50: [])
    monkeypatch.setattr(
        server, "_aoty_genre_slug_map",
        lambda: {"art pop": ("141-art-pop", "Art Pop"),
                 "emo rap": ("306-emo-rap", "Emo Rap")},
    )
    names = [n for _s, n, _w in server._taste_profile()["genres"]]
    assert "Art Pop" in names, "the artist's defining tag should survive"
    assert "Emo Rap" not in names, f"a footnote tag became a genre: {names}"


def test_a_solo_tag_survives_when_it_defines_the_artist(stub_auth, monkeypatch):
    """The rule must not simply demand two artists — breadth and backer
    count rise together, so that readmits "electronic" while dropping
    genuinely specific genres like folktronica that rest on one artist."""
    import server

    monkeypatch.setattr(server.lastfm, "status", lambda: {"connected": True})
    monkeypatch.setattr(
        server.lastfm, "get_top_artists",
        lambda p, limit: [{"name": "Quadeca", "playcount": 100}] if p == "1month" else [],
    )
    monkeypatch.setattr(
        server.lastfm, "get_artist_top_tags",
        lambda name, limit=12: [{"name": "folktronica", "count": 100}],
    )
    monkeypatch.setattr(server.lastfm, "get_chart_top_tags", lambda limit=250: [])
    monkeypatch.setattr(server.lastfm, "get_chart_top_artists", lambda limit=500: [])
    monkeypatch.setattr(server.lastfm, "get_similar_artists", lambda n, limit=50: [])
    monkeypatch.setattr(
        server, "_aoty_genre_slug_map",
        lambda: {"folktronica": ("104-folktronica", "Folktronica")},
    )
    names = [n for _s, n, _w in server._taste_profile()["genres"]]
    assert names == ["Folktronica"]
