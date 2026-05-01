"""Tests for `app.aoty_resolver.resolve_listing`.

The resolver's behavior matters more than its implementation: callers
hand it a chart listing and expect every entry back, each decorated
with a Tidal album dict or `None`. The cases worth pinning are the
defensive paths (no listing, missing artist/title, search exception,
serializer exception) and the cache contract (hit short-circuits the
fan-out, miss writes through, None is never persisted).

The fan-out itself uses a ThreadPoolExecutor of 3 workers per the
rate-limit posture comment in the module — testing that requires real
threads. Each test stubs the search / serializer / sleep with simple
fakes, so the threading is incidental.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import server
from app import aoty_resolver, lastfm_disk_cache


@pytest.fixture(autouse=True)
def _isolated_disk_cache(tmp_path, monkeypatch):
    """Per-test sqlite file. The resolver reads/writes through the
    lastfm_disk_cache module, so a single redirect of `_db_path` is
    enough — same pattern used by `test_lastfm_disk_cache`."""
    monkeypatch.setattr(
        lastfm_disk_cache, "_db_path", tmp_path / "lastfm_disk_cache.db"
    )


@pytest.fixture
def stub_server(monkeypatch):
    """Replace the symbols `resolve_listing` imports from `server`
    with simple stand-ins. Callers can override individual fakes by
    re-assigning attributes on the returned object."""

    class _Stub:
        searches: list[str] = []
        # Default: the search returns a single album whose name + artist
        # do NOT exact-match the requested artist/title — so the
        # "exact match wins" branch isn't taken unless the test sets
        # search_results explicitly.
        search_results: dict = {"albums": [_FakeAlbum("Top Hit", "Some Artist")]}
        serialize_call_count: int = 0
        sleep_call_count: int = 0
        explicit_pref: str = "explicit"

        def search(self, query: str, limit: int = 5):
            self.searches.append(query)
            return self.search_results

        def album_to_dict(self, album):
            self.serialize_call_count += 1
            return {"id": album.id, "name": album.name}

        def filter_explicit_dupes(self, items, pref, *, kind):
            return items

        def tidal_jitter_sleep(self):
            self.sleep_call_count += 1

    stub = _Stub()
    # Reset class-level state on every fixture instantiation so the
    # mutable defaults don't leak between tests.
    stub.searches = []
    stub.serialize_call_count = 0
    stub.sleep_call_count = 0
    monkeypatch.setattr(server, "tidal", stub, raising=False)
    monkeypatch.setattr(server, "album_to_dict", stub.album_to_dict)
    monkeypatch.setattr(
        server, "filter_explicit_dupes", stub.filter_explicit_dupes
    )
    monkeypatch.setattr(server, "tidal_jitter_sleep", stub.tidal_jitter_sleep)

    class _SettingsStub:
        explicit_content_preference = stub.explicit_pref

    monkeypatch.setattr(server, "settings", _SettingsStub(), raising=False)
    return stub


class _FakeAlbum:
    """Minimal stand-in for a Tidal Album object. Only exposes the
    attributes `resolve_listing` reads via getattr."""

    def __init__(self, name: str, artist: str, id_: str = "ID"):
        self.id = id_
        self.name = name
        self.artists = [_FakeArtist(artist)]


class _FakeArtist:
    def __init__(self, name: str):
        self.name = name


# --- shape contract --------------------------------------------------------


def test_empty_listing_returns_empty():
    """No work to do, no Tidal calls, no exceptions."""
    assert aoty_resolver.resolve_listing([]) == []


def test_missing_artist_returns_tidal_album_none(stub_server):
    """A listing entry without an artist string should pass through
    with `tidal_album=None` rather than triggering a search for the
    empty string."""
    out = aoty_resolver.resolve_listing([{"artist": "", "title": "X"}])
    assert len(out) == 1
    assert out[0]["tidal_album"] is None
    assert stub_server.searches == []


def test_missing_title_returns_tidal_album_none(stub_server):
    out = aoty_resolver.resolve_listing([{"artist": "X", "title": ""}])
    assert len(out) == 1
    assert out[0]["tidal_album"] is None
    assert stub_server.searches == []


def test_preserves_other_listing_fields(stub_server):
    """Decoration is additive — original entry keys come back intact."""
    entry = {
        "artist": "X",
        "title": "Y",
        "score": 84,
        "rank": 1,
        "must_hear": True,
    }
    out = aoty_resolver.resolve_listing([entry])
    assert out[0]["score"] == 84
    assert out[0]["rank"] == 1
    assert out[0]["must_hear"] is True


# --- match preference ------------------------------------------------------


def test_exact_artist_title_match_wins_over_top_hit(stub_server):
    """The first result is Tidal's "top hit"; the second is an exact
    case-insensitive match for the requested artist+title. The exact
    match should win."""
    stub_server.search_results = {
        "albums": [
            _FakeAlbum("Top Hit", "Some Artist", id_="TOP"),
            _FakeAlbum("Wanted Title", "Wanted Artist", id_="EXACT"),
        ]
    }
    out = aoty_resolver.resolve_listing(
        [{"artist": "Wanted Artist", "title": "Wanted Title"}]
    )
    assert out[0]["tidal_album"]["id"] == "EXACT"


def test_falls_back_to_top_hit_when_no_exact_match(stub_server):
    """If no result exact-matches the requested artist+title, the
    first (top-hit) result is used."""
    stub_server.search_results = {
        "albums": [
            _FakeAlbum("Something Else", "Different", id_="TOP"),
            _FakeAlbum("Other", "Yet Another", id_="OTHER"),
        ]
    }
    out = aoty_resolver.resolve_listing(
        [{"artist": "Wanted", "title": "Title"}]
    )
    assert out[0]["tidal_album"]["id"] == "TOP"


def test_match_is_case_insensitive(stub_server):
    """The exact-match check lowercases both sides — Tidal occasionally
    returns differently-cased titles."""
    stub_server.search_results = {
        "albums": [
            _FakeAlbum("WANTED TITLE", "wanted artist", id_="MATCH"),
        ]
    }
    out = aoty_resolver.resolve_listing(
        [{"artist": "Wanted Artist", "title": "Wanted Title"}]
    )
    assert out[0]["tidal_album"]["id"] == "MATCH"


def test_empty_search_results_yields_none(stub_server):
    """Tidal returned no albums for the query — the entry comes back
    with `tidal_album=None` and the listing is preserved."""
    stub_server.search_results = {"albums": []}
    out = aoty_resolver.resolve_listing(
        [{"artist": "X", "title": "Y", "rank": 7}]
    )
    assert out[0]["tidal_album"] is None
    assert out[0]["rank"] == 7


# --- cache contract --------------------------------------------------------


def test_cache_hit_short_circuits_search(stub_server):
    """A pre-populated cache entry means no Tidal search call and no
    rate-limit sleep on the second pass."""
    listing = [{"artist": "X", "title": "Y"}]
    aoty_resolver.resolve_listing(listing)
    first_search_count = len(stub_server.searches)
    first_sleep_count = stub_server.sleep_call_count

    # Second call with the same entry should hit the disk cache.
    aoty_resolver.resolve_listing(listing)
    assert len(stub_server.searches) == first_search_count
    assert stub_server.sleep_call_count == first_sleep_count


def test_cache_only_persists_successful_resolves(stub_server):
    """A None resolution (Tidal returned no match) must NOT be cached
    — caching None for 30 days would blank the album from the chart on
    a single transient hiccup. So the second call should re-search."""
    stub_server.search_results = {"albums": []}
    listing = [{"artist": "X", "title": "Y"}]
    aoty_resolver.resolve_listing(listing)
    assert len(stub_server.searches) == 1

    # Now Tidal "comes back" — the second call should retry the search
    # rather than serving cached None.
    stub_server.search_results = {
        "albums": [_FakeAlbum("Y", "X", id_="HIT")]
    }
    out = aoty_resolver.resolve_listing(listing)
    assert len(stub_server.searches) == 2
    assert out[0]["tidal_album"]["id"] == "HIT"


def test_cache_key_includes_explicit_preference(stub_server, monkeypatch):
    """A user toggling explicit-content preference between requests
    must not pull a stale cached resolution from the other branch.
    Two resolves of the same album under two preferences should run
    two searches."""
    listing = [{"artist": "X", "title": "Y"}]

    class ExplicitSettings:
        explicit_content_preference = "explicit"

    class CleanSettings:
        explicit_content_preference = "clean"

    monkeypatch.setattr(server, "settings", ExplicitSettings())
    aoty_resolver.resolve_listing(listing)
    monkeypatch.setattr(server, "settings", CleanSettings())
    aoty_resolver.resolve_listing(listing)

    assert len(stub_server.searches) == 2


# --- defensive paths -------------------------------------------------------


def test_search_exception_yields_none_and_does_not_cache(stub_server):
    """A Tidal client exception (network, abuse-detection 429, etc.)
    should surface as `tidal_album=None` and NOT poison the cache."""

    def boom(*_a, **_kw):
        raise RuntimeError("boom")

    stub_server.search = boom
    listing = [{"artist": "X", "title": "Y"}]
    out = aoty_resolver.resolve_listing(listing)
    assert out[0]["tidal_album"] is None

    # Cache must be empty — a working Tidal on the next call should
    # actually run the search again.
    stub_server.search = lambda *_a, **_kw: {
        "albums": [_FakeAlbum("Y", "X", id_="HIT")]
    }
    out = aoty_resolver.resolve_listing(listing)
    assert out[0]["tidal_album"] is not None


def test_serialize_exception_yields_none(stub_server):
    """`album_to_dict` raising (e.g. an unexpected attribute on the
    Tidal album object) must not propagate — the row comes back with
    `tidal_album=None`."""

    def boom(_a):
        raise RuntimeError("serialize-boom")

    with patch.object(server, "album_to_dict", boom):
        out = aoty_resolver.resolve_listing(
            [{"artist": "X", "title": "Y"}]
        )
    assert out[0]["tidal_album"] is None
