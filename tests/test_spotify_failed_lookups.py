"""A Spotify lookup that fails is not cached as "no playcount".

The search helpers used to swallow errors and return an empty list, and
getTrack failures just dropped a candidate. A network blip, a rate limit
or a Spotify outage then looked exactly like "this track isn't on
Spotify", and the miss was cached for a day: every track on the page
stayed blank long after Spotify came back. A failed lookup now caches
nothing, so the next view asks again. A genuine miss, Spotify answering
with no match, is still cached as before.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app import spotify_public
from tests.test_spotify_resolution import _get_track_payload, _search_payload

ISRC = "USWB10400049"


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(spotify_public, "_db_path", tmp_path / "cache.db")
    spotify_public._db().close()


@pytest.fixture
def song(monkeypatch):
    song = MagicMock()
    monkeypatch.setattr(spotify_public, "_ensure_client", lambda: (song, MagicMock()))
    return song


def _cached_row(isrc: str = ISRC):
    conn = spotify_public._db()
    try:
        return conn.execute(
            "SELECT playcount, fetched_at FROM track_playcount WHERE isrc=?",
            (isrc,),
        ).fetchone()
    finally:
        conn.close()


def _down(*_a, **_k):
    raise ConnectionError("Spotify unreachable")


def test_failed_searches_cache_nothing_and_the_next_view_retries(cache, song):
    song.query_songs.side_effect = _down

    assert spotify_public.playcount_with_fallback(ISRC, "Dreams", "Fleetwood Mac") is None
    assert _cached_row() is None, "a network failure must not be cached as a miss"

    # Spotify is back: the next view asks again and gets the number.
    song.query_songs.side_effect = lambda q, limit=5: _search_payload(
        {"id": "T1", "name": "Dreams", "artist": "Fleetwood Mac"}
    )
    song.get_track_info.return_value = _get_track_payload(
        playcount=1_000, primary_artist_name="Fleetwood Mac"
    )
    assert spotify_public.playcount_with_fallback(ISRC, "Dreams", "Fleetwood Mac") == 1_000


def test_a_genuine_miss_is_still_cached(cache, song):
    song.query_songs.side_effect = lambda q, limit=5: _search_payload()

    assert spotify_public.playcount_with_fallback(ISRC, "Dreams", "Fleetwood Mac") is None
    row = _cached_row()
    assert row is not None and row[0] is None, "Spotify answered: cache the miss"


def test_a_failed_get_track_caches_nothing(cache, song):
    # Both searches work, but the matching candidate can't be read: it
    # might have been the canonical one, so neither a miss nor a lower
    # number from another candidate may stick.
    song.query_songs.side_effect = lambda q, limit=5: _search_payload(
        {"id": "T1", "name": "Dreams", "artist": "Fleetwood Mac"}
    )
    song.get_track_info.side_effect = _down

    assert spotify_public.playcount_with_fallback(ISRC, "Dreams", "Fleetwood Mac") is None
    assert _cached_row() is None


def test_a_partial_answer_is_shown_but_not_cached(cache, song):
    # The title search fails; the ISRC search finds a reissue. Show its
    # number now, but don't pin it for a week: the canonical version
    # with more plays may be in the search that failed.
    def query(q, limit=5):
        if q.startswith("isrc:"):
            return _search_payload({"id": "REISSUE", "name": "Dreams", "artist": "Fleetwood Mac"})
        raise ConnectionError("rate limited")

    song.query_songs.side_effect = query
    song.get_track_info.return_value = _get_track_payload(
        playcount=40_000, primary_artist_name="Fleetwood Mac"
    )

    assert spotify_public.playcount_with_fallback(ISRC, "Dreams", "Fleetwood Mac") == 40_000
    assert _cached_row() is None


def test_isrc_only_lookup_does_not_cache_a_failed_search(cache, song):
    song.query_songs.side_effect = _down

    assert spotify_public.playcount_by_isrc(ISRC) is None
    assert _cached_row() is None

    song.query_songs.side_effect = lambda q, limit=5: _search_payload()
    assert spotify_public.playcount_by_isrc(ISRC) is None
    row = _cached_row()
    assert row is not None and row[0] is None, "a real miss is still cached"
