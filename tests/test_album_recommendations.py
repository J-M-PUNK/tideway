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


def test_favorited_albums_are_filtered_from_every_section(stub_auth, monkeypatch):
    """Only _album_recommendations used to exclude saved albums, so the
    AOTY-backed rows recommended the listener their own library back —
    11 of 18 in "Popular in Your Orbit" on a real profile. The filter
    belongs to section assembly so it covers every row."""
    import server

    monkeypatch.setattr(
        server.settings, "album_recommendations_enabled", True, raising=False
    )
    _no_lastfm(monkeypatch)
    _no_aoty(monkeypatch)

    owned = StubAlbum("99", "Already Saved", "Artist Z")
    _set_library(monkeypatch, fav_albums=[owned])

    # An AOTY-backed row that offers the saved album plus a fresh one.
    def _fake_section(key, title, subtitle, listing, profile, taste_rank, deep=False):
        return {
            "key": key, "title": title, "subtitle": subtitle,
            "albums": [
                {"id": "99", "name": "Already Saved",
                 "artists": [{"name": "Artist Z"}], "available": True},
                {"id": "100", "name": "Brand New",
                 "artists": [{"name": "Artist Y"}], "available": True},
            ],
        }

    monkeypatch.setattr(server, "_aoty_section", _fake_section)
    out = server.recommendations_albums()
    served = [a["id"] for s in out["sections"] for a in s["albums"]]
    assert "99" not in served, "a saved album was recommended back to the user"
    assert "100" in served, "the unsaved album should still be offered"


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
    seed = StubAlbum("1", "Seed", "SA", similar=[StubAlbum("m1", "Made Pick", "MB")])
    _set_library(monkeypatch, fav_albums=[seed])

    sections = server._build_recommendation_sections()
    # Genre rows absent (no inferred genres); order is made -> new -> popular.
    assert [s["key"] for s in sections] == ["made_for_you", "new_releases", "popular"]
    made = next(s for s in sections if s["key"] == "made_for_you")
    assert [a["id"] for a in made["albums"]] == ["m1"]


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
    _set_library(monkeypatch, fav_albums=[seed])
    assert [a["id"] for a in server._album_recommendations()] == ["8"]


def test_dismissed_artist_downweighted(stub_auth, monkeypatch):
    import server

    _no_lastfm(monkeypatch)
    monkeypatch.setattr(
        server, "_load_rec_feedback", lambda: {"albums": set(), "artists": {"artist z"}}
    )
    # Two picks at similar base score; the dismissed artist's should sink.
    seed = StubAlbum("1", "Seed", "S", similar=[
        StubAlbum("2", "By Z", "Artist Z"),
        StubAlbum("3", "By Q", "Artist Q"),
    ])
    _set_library(monkeypatch, fav_albums=[seed])
    ids = [a["id"] for a in server._album_recommendations()]
    assert ids.index("3") < ids.index("2")  # Q above the down-weighted Z


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
