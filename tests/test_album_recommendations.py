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
                      deep=False, owned=None):
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
