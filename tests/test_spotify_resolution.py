"""Tests for Spotify track + artist resolution.

Two real bugs from production:

1. Stream count for famous tracks (Thriller, Bohemian Rhapsody, etc)
   was wildly low. Root cause: Spotify has many entries for the same
   recording — original release, anniversary edition, deluxe reissue,
   regional catalogs — and each entry has its own ISRC. Tidal's
   metadata sometimes carries the ISRC of a less-played reissue, and
   the lookup searches `isrc:<X>` which by definition only sees that
   one ISRC's editions. The canonical 1.6B-play version has a
   different ISRC and never enters the candidate pool.

2. Monthly listeners didn't render for some artists. Root cause: the
   Tidal-to-Spotify artist mapping picked the primary Spotify artist
   on the sample track's record. When that track is a collab where
   the Tidal artist is a guest, the primary Spotify artist is the
   host — so we'd resolve "Jim's monthly listeners" via a track
   primarily by Bob, and silently render Bob's stats (or, if the
   name didn't match anything else later, none at all).

The tests here pin both fixes in place by mocking SpotAPI's GraphQL
responses with realistic shapes.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from app import spotify_public


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    db_path = tmp_path / "spotify_public_cache.db"
    monkeypatch.setattr(spotify_public, "_db_path", db_path)
    conn = spotify_public._db()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Helpers to build fake Spotify GraphQL payloads.
# ---------------------------------------------------------------------------


def _search_payload(*candidates: dict) -> dict:
    """Build a queryV2 search response. Each candidate is
    `{"id": "<spotify_track_id>", "name": "<title>"}`."""
    return {
        "data": {
            "searchV2": {
                "tracksV2": {
                    "items": [
                        {
                            "item": {
                                "data": {
                                    "uri": f"spotify:track:{c['id']}",
                                    "name": c.get("name", ""),
                                }
                            }
                        }
                        for c in candidates
                    ]
                }
            }
        }
    }


def _get_track_payload(
    *,
    playcount: int,
    primary_artist_id: str = "ART",
    primary_artist_name: str = "",
) -> dict:
    """Build a getTrack response — playcount on trackUnion, primary
    artist URI in `firstArtist.items[0]`."""
    return {
        "data": {
            "trackUnion": {
                "playcount": playcount,
                "firstArtist": {
                    "items": [
                        {
                            "uri": f"spotify:artist:{primary_artist_id}",
                            "profile": {"name": primary_artist_name},
                        }
                    ]
                },
            }
        }
    }


def _artist_overview_payload(monthly_listeners: int, name: str) -> dict:
    return {
        "data": {
            "artistUnion": {
                "profile": {"name": name},
                "stats": {
                    "monthlyListeners": monthly_listeners,
                    "followers": 0,
                    "worldRank": 0,
                    "topCities": {"items": []},
                },
            }
        }
    }


@pytest.fixture
def fake_clients(monkeypatch):
    """Replace `_ensure_client` with mocks. Tests configure
    `song.query_songs.side_effect`, `song.get_track_info.side_effect`,
    and `artist.get_artist.side_effect` per their scenario.
    """
    song = MagicMock()
    artist = MagicMock()
    monkeypatch.setattr(spotify_public, "_ensure_client", lambda: (song, artist))
    return song, artist


# ---------------------------------------------------------------------------
# Bug 1: Thriller's playcount is wrong because Tidal's ISRC points at
# a reissue with a different ISRC than the canonical 1.6B-play release.
# ---------------------------------------------------------------------------


def test_thriller_canonical_playcount_wins_over_reissue_isrc(
    isolated_cache, fake_clients
):
    song, _artist = fake_clients

    # Tidal hands us this ISRC — a hypothetical 2008 reissue with 40M plays.
    reissue_isrc = "USSM10812345"

    # Spotify's ISRC search returns ONLY the reissue (different ISRCs
    # are not aliased; they live on different Spotify track ids).
    isrc_search = _search_payload(
        {"id": "REISSUE_TRK_ID", "name": "Thriller"},
    )

    # Title+artist search returns multiple entries, including the
    # canonical 1.6B-play original. All are "Thriller" by "Michael
    # Jackson" so the strict name+artist filter accepts all of them.
    title_artist_search = _search_payload(
        {"id": "REISSUE_TRK_ID", "name": "Thriller"},
        {"id": "CANONICAL_TRK_ID", "name": "Thriller"},
        {"id": "ANNIV25_TRK_ID", "name": "Thriller"},
    )

    def _query(q, limit=5):
        if q.startswith("isrc:"):
            return isrc_search
        return title_artist_search

    song.query_songs.side_effect = _query

    # Per-track getTrack: reissue is the ISRC's only hit at 40M;
    # canonical is at 1.6B; 25th anniv is at 80M.
    def _get(track_id):
        if track_id == "REISSUE_TRK_ID":
            return _get_track_payload(
                playcount=40_000_000,
                primary_artist_name="Michael Jackson",
            )
        if track_id == "CANONICAL_TRK_ID":
            return _get_track_payload(
                playcount=1_600_000_000,
                primary_artist_name="Michael Jackson",
            )
        if track_id == "ANNIV25_TRK_ID":
            return _get_track_payload(
                playcount=80_000_000,
                primary_artist_name="Michael Jackson",
            )
        raise AssertionError(f"unexpected track_id {track_id}")

    song.get_track_info.side_effect = _get

    pc = spotify_public.playcount_with_fallback(
        reissue_isrc, "Thriller", "Michael Jackson"
    )

    assert pc == 1_600_000_000, (
        f"expected canonical 1.6B playcount, got {pc} — "
        "fallback isn't preferring the higher-playcount canonical "
        "match when the ISRC search succeeds with a low number"
    )


def test_canonical_only_picks_matching_artist(isolated_cache, fake_clients):
    """A title+artist search can return tracks with the same TITLE
    by different artists (covers, soundalikes, regional re-records).
    The fallback must only count candidates whose primary artist
    matches the Tidal artist."""
    song, _ = fake_clients

    isrc_search = _search_payload(
        {"id": "OBSCURE_ID", "name": "Hello"},
    )
    title_artist_search = _search_payload(
        {"id": "ADELE_HELLO", "name": "Hello"},
        {"id": "LIONEL_HELLO", "name": "Hello"},  # Lionel Richie's
    )

    def _query(q, limit=5):
        if q.startswith("isrc:"):
            return isrc_search
        return title_artist_search

    song.query_songs.side_effect = _query

    def _get(track_id):
        if track_id == "OBSCURE_ID":
            return _get_track_payload(
                playcount=10_000, primary_artist_name="Adele"
            )
        if track_id == "ADELE_HELLO":
            return _get_track_payload(
                playcount=2_000_000_000, primary_artist_name="Adele"
            )
        if track_id == "LIONEL_HELLO":
            return _get_track_payload(
                playcount=400_000_000, primary_artist_name="Lionel Richie"
            )
        raise AssertionError(track_id)

    song.get_track_info.side_effect = _get

    pc = spotify_public.playcount_with_fallback(
        "USOBSCURE", "Hello", "Adele"
    )

    assert pc == 2_000_000_000, "Adele's Hello should win, not Lionel's"


def test_canonical_caches_under_original_isrc(isolated_cache, fake_clients):
    """Once the canonical playcount is found via the title+artist
    fallback, subsequent lookups under the original ISRC must come
    from cache without re-hitting Spotify."""
    song, _ = fake_clients

    isrc_search = _search_payload({"id": "REISSUE_ID", "name": "Thriller"})
    title_artist_search = _search_payload(
        {"id": "REISSUE_ID", "name": "Thriller"},
        {"id": "CANONICAL_ID", "name": "Thriller"},
    )

    def _query(q, limit=5):
        return isrc_search if q.startswith("isrc:") else title_artist_search

    song.query_songs.side_effect = _query

    def _get(track_id):
        if track_id == "REISSUE_ID":
            return _get_track_payload(
                playcount=40_000_000, primary_artist_name="Michael Jackson"
            )
        if track_id == "CANONICAL_ID":
            return _get_track_payload(
                playcount=1_600_000_000, primary_artist_name="Michael Jackson"
            )
        raise AssertionError(track_id)

    song.get_track_info.side_effect = _get

    first = spotify_public.playcount_with_fallback(
        "USTHRILLER", "Thriller", "Michael Jackson"
    )
    assert first == 1_600_000_000

    pre_calls = song.query_songs.call_count + song.get_track_info.call_count

    # Second lookup should be all cache.
    second = spotify_public.playcount_with_fallback(
        "USTHRILLER", "Thriller", "Michael Jackson"
    )
    assert second == 1_600_000_000

    post_calls = song.query_songs.call_count + song.get_track_info.call_count
    assert post_calls == pre_calls, (
        f"second call hit Spotify {post_calls - pre_calls} extra times — "
        "cache isn't sticking under the original ISRC"
    )


# ---------------------------------------------------------------------------
# Bug 2: artist resolution lands on the wrong Spotify artist when the
# sample ISRC is a track where the Tidal artist is a feature, not the
# primary.
# ---------------------------------------------------------------------------


def test_artist_resolution_prefers_name_matching_isrc(
    isolated_cache, fake_clients
):
    """Tidal artist 'Jim' has two top tracks. The first ISRC is for a
    collab where Bob is primary on Spotify, the second is a Jim-only
    track. With a list of sample ISRCs the resolver must walk past
    the first when its primary artist doesn't match Jim, and land on
    the second."""
    song, artist = fake_clients

    feat_isrc = "USFEAT00001"
    solo_isrc = "USSOLO00001"

    def _query(q, limit=5):
        if q == f"isrc:{feat_isrc}":
            return _search_payload({"id": "FEAT_TRK_ID", "name": "Crossover"})
        if q == f"isrc:{solo_isrc}":
            return _search_payload({"id": "SOLO_TRK_ID", "name": "Anthem"})
        raise AssertionError(q)

    song.query_songs.side_effect = _query

    def _get(track_id):
        if track_id == "FEAT_TRK_ID":
            return _get_track_payload(
                playcount=5_000_000,
                primary_artist_id="BOB_ARTIST_ID",
                primary_artist_name="Bob",
            )
        if track_id == "SOLO_TRK_ID":
            return _get_track_payload(
                playcount=2_000_000,
                primary_artist_id="JIM_ARTIST_ID",
                primary_artist_name="Jim",
            )
        raise AssertionError(track_id)

    song.get_track_info.side_effect = _get

    artist.get_artist.return_value = _artist_overview_payload(
        monthly_listeners=12_345_678, name="Jim"
    )

    stats = spotify_public.artist_stats_v2(
        tidal_artist_id="42",
        tidal_artist_name="Jim",
        sample_isrcs=[feat_isrc, solo_isrc],
    )

    assert stats is not None
    assert stats.spotify_artist_id == "JIM_ARTIST_ID", (
        f"resolver picked {stats.spotify_artist_id!r} but should have "
        "rejected Bob's id and walked to the next ISRC"
    )
    assert stats.monthly_listeners == 12_345_678
    assert stats.name == "Jim"


def test_artist_resolution_falls_back_to_first_when_no_match(
    isolated_cache, fake_clients
):
    """If none of the sample ISRCs surface a primary artist whose name
    matches the Tidal name, return None rather than guess. Showing
    the wrong artist's monthly listeners is worse than showing
    nothing."""
    song, artist = fake_clients

    def _query(q, limit=5):
        return _search_payload({"id": "TRK", "name": "x"})

    song.query_songs.side_effect = _query
    song.get_track_info.return_value = _get_track_payload(
        playcount=1_000,
        primary_artist_id="WRONG_ART",
        primary_artist_name="Someone Else",
    )

    # Don't even need to set artist.get_artist — we should never reach it.
    stats = spotify_public.artist_stats_v2(
        tidal_artist_id="99",
        tidal_artist_name="Jim",
        sample_isrcs=["USXXX00001", "USXXX00002"],
    )

    assert stats is None
    artist.get_artist.assert_not_called()


def test_artist_resolution_caches_resolved_id(isolated_cache, fake_clients):
    """A second call for the same Tidal artist id must skip the ISRC
    walk entirely (cached mapping) and only hit
    queryArtistOverview."""
    song, artist = fake_clients

    song.query_songs.side_effect = lambda q, limit=5: _search_payload(
        {"id": "TRK", "name": "x"}
    )
    song.get_track_info.return_value = _get_track_payload(
        playcount=1_000,
        primary_artist_id="JIM_ARTIST_ID",
        primary_artist_name="Jim",
    )
    artist.get_artist.return_value = _artist_overview_payload(
        monthly_listeners=1, name="Jim"
    )

    spotify_public.artist_stats_v2(
        tidal_artist_id="7",
        tidal_artist_name="Jim",
        sample_isrcs=["USXXX0001"],
    )
    pre_query = song.query_songs.call_count
    pre_track = song.get_track_info.call_count

    # Second call. Bust the artist_stats cache so we still hit the
    # artist overview, but the ISRC->artist mapping should be cached.
    conn = sqlite3.connect(str(isolated_cache))
    conn.execute("DELETE FROM artist_stats")
    conn.commit()
    conn.close()

    spotify_public.artist_stats_v2(
        tidal_artist_id="7",
        tidal_artist_name="Jim",
        sample_isrcs=["USXXX0001"],
    )

    assert song.query_songs.call_count == pre_query, (
        "second call re-walked the ISRC search — Tidal->Spotify "
        "artist mapping isn't being cached"
    )
    assert song.get_track_info.call_count == pre_track, (
        "second call re-fetched getTrack — mapping cache miss"
    )


# ---------------------------------------------------------------------------
# Schema migration: bumping _CACHE_SCHEMA_VERSION wipes the rows whose
# values the resolver fix could have made wrong.
# ---------------------------------------------------------------------------


def test_schema_bump_wipes_stale_caches(tmp_path, monkeypatch):
    """Existing users who upgrade carry a cache full of pre-fix rows
    (Thriller=40M, wrong Tidal->Spotify artist mappings). Opening
    the DB the first time on the new code must drop those rows so
    the corrected numbers appear without the user manually clearing
    anything."""
    db_path = tmp_path / "spotify_public_cache.db"
    monkeypatch.setattr(spotify_public, "_db_path", db_path)

    # Simulate the OLD database: schema version absent, all the
    # tables full of pre-fix rows. Built by hand with raw SQL so
    # the test can't be invalidated by a future change to the
    # in-code create-table statements.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE isrc_to_spotify_track (
            isrc TEXT PRIMARY KEY,
            spotify_track_id TEXT,
            fetched_at INTEGER
        );
        CREATE TABLE tidal_to_spotify_artist (
            tidal_artist_id TEXT PRIMARY KEY,
            spotify_artist_id TEXT,
            fetched_at INTEGER
        );
        CREATE TABLE track_playcount (
            isrc TEXT PRIMARY KEY,
            playcount INTEGER,
            fetched_at INTEGER
        );
        CREATE TABLE artist_stats (
            spotify_artist_id TEXT PRIMARY KEY,
            payload TEXT,
            fetched_at INTEGER
        );
        """
    )
    now = int(time.time())
    conn.execute(
        "INSERT INTO track_playcount VALUES ('USTHRILLER', 40000000, ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO isrc_to_spotify_track VALUES "
        "('USTHRILLER', 'WRONG_REISSUE_ID', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO tidal_to_spotify_artist VALUES ('42', 'WRONG_ART_ID', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO artist_stats VALUES ('WRONG_ART_ID', '{}', ?)",
        (now,),
    )
    conn.commit()
    conn.close()

    # Open via the production path. Migration runs.
    conn = spotify_public._db()
    try:
        track_rows = conn.execute(
            "SELECT COUNT(*) FROM track_playcount"
        ).fetchone()[0]
        isrc_rows = conn.execute(
            "SELECT COUNT(*) FROM isrc_to_spotify_track"
        ).fetchone()[0]
        artist_rows = conn.execute(
            "SELECT COUNT(*) FROM tidal_to_spotify_artist"
        ).fetchone()[0]
        # artist_stats is keyed by spotify_artist_id — those rows are
        # still factually correct, the migration leaves them so a
        # re-resolved mapping that lands on the same id avoids a
        # round trip.
        version = conn.execute(
            "SELECT value FROM cache_meta WHERE key='schema_version'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert track_rows == 0
    assert isrc_rows == 0
    assert artist_rows == 0
    assert int(version) == spotify_public._CACHE_SCHEMA_VERSION


def test_schema_bump_is_idempotent(tmp_path, monkeypatch):
    """Once the migration has run, subsequent opens must NOT re-clear
    rows the user has populated since."""
    db_path = tmp_path / "spotify_public_cache.db"
    monkeypatch.setattr(spotify_public, "_db_path", db_path)

    # First open: creates the tables + writes the version sentinel.
    spotify_public._db().close()

    # User uses the app, populates a real row.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO track_playcount VALUES ('USREAL01', 1234567, ?)",
        (int(time.time()),),
    )
    conn.commit()
    conn.close()

    # Second open via the production path — must leave the row alone.
    conn = spotify_public._db()
    try:
        rows = conn.execute(
            "SELECT playcount FROM track_playcount WHERE isrc='USREAL01'"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(1234567,)]


# ---------------------------------------------------------------------------
# Search-limit bump: candidate list widened from 5 to 50 so that albums
# whose canonical version sits beyond the top-5 results (e.g. when a
# track has many alternate releases / pre-release singles / lyric
# videos) still surface the right playcount.
# ---------------------------------------------------------------------------


def test_search_limit_is_widened(isolated_cache, fake_clients):
    """`_search_track_candidates` must request `limit=50` from
    spotapi. With the old `limit=5`, tracks like Quadeca's SCRAPYARD
    album returned undercounts because the canonical album version
    wasn't in the top 5 ISRC-search results."""
    song, _ = fake_clients
    captured: list[int] = []

    def _query(q, limit=5):
        captured.append(limit)
        return _search_payload({"id": "X", "name": "x"})

    song.query_songs.side_effect = _query
    song.get_track_info.return_value = _get_track_payload(
        playcount=0, primary_artist_name="x"
    )

    spotify_public._search_track_candidates("isrc:USXX0001")
    spotify_public._search_track_candidates("Quadeca DUSTCUTTER")

    assert captured == [50, 50], (
        f"expected limit=50 on every search call, got {captured}"
    )


# ---------------------------------------------------------------------------
# Artist-name search fallback: when the ISRC walk fails to find a
# primary-artist match (typical for collab-heavy artists like Justin
# Bieber whose top tracks on Tidal are all feature credits), fall
# back to Spotify's `query_artists` endpoint and accept the first
# name match.
# ---------------------------------------------------------------------------


def _artist_search_payload(*candidates: dict) -> dict:
    """Build a queryV2 artist-search response. Each candidate is
    `{"id": "<spotify_artist_id>", "name": "<display name>"}`."""
    return {
        "data": {
            "searchV2": {
                "artists": {
                    "items": [
                        {
                            "item": {
                                "data": {
                                    "uri": f"spotify:artist:{c['id']}",
                                    "profile": {"name": c["name"]},
                                }
                            }
                        }
                        for c in candidates
                    ]
                }
            }
        }
    }


def test_artist_resolution_falls_back_to_name_search(
    isolated_cache, fake_clients
):
    """Tidal artist 'Justin Bieber' has every sample ISRC on a
    collab where Bieber is a feature, not the primary. ISRC walk
    can't find a primary-artist match. Resolver must fall through
    to `query_artists("Justin Bieber")` and pick up the canonical
    Bieber artist id."""
    song, artist = fake_clients

    # Every ISRC search returns a track where someone else is primary.
    song.query_songs.side_effect = lambda q, limit=50: _search_payload(
        {"id": "FEAT_TRK_ID", "name": "Stay"},
    )
    song.get_track_info.return_value = _get_track_payload(
        playcount=2_000_000_000,
        primary_artist_id="KIDLAROI_ID",
        primary_artist_name="The Kid LAROI",
    )

    # Artist search returns Bieber as the first hit.
    artist.query_artists.return_value = _artist_search_payload(
        {"id": "BIEBER_ARTIST_ID", "name": "Justin Bieber"},
    )
    artist.get_artist.return_value = _artist_overview_payload(
        monthly_listeners=80_000_000, name="Justin Bieber"
    )

    stats = spotify_public.artist_stats_v2(
        tidal_artist_id="bieber_tidal_id",
        tidal_artist_name="Justin Bieber",
        sample_isrcs=["USFEAT00001", "USFEAT00002", "USFEAT00003"],
    )

    assert stats is not None
    assert stats.spotify_artist_id == "BIEBER_ARTIST_ID", (
        "fallback didn't pick up the artist-name search hit; resolver "
        "is still returning None when ISRC walk fails"
    )
    assert stats.monthly_listeners == 80_000_000
    assert artist.query_artists.called, (
        "artist-name search wasn't attempted after ISRC walk failed"
    )


def test_artist_name_search_rejects_non_matching_results(
    isolated_cache, fake_clients
):
    """Artist search is fuzzy — query "Jim" can return "Jim and the
    Hellcats", "Jimmy", "Jim Carrey", etc. The fallback must only
    accept hits whose name matches case-insensitively, and skip
    everything else."""
    song, artist = fake_clients

    song.query_songs.side_effect = lambda q, limit=50: _search_payload(
        {"id": "T", "name": "x"}
    )
    song.get_track_info.return_value = _get_track_payload(
        playcount=1, primary_artist_name="Someone Else"
    )

    # Search returns soundalikes only — none match "Jim" exactly.
    artist.query_artists.return_value = _artist_search_payload(
        {"id": "JIMMY_ID", "name": "Jimmy"},
        {"id": "JIMC_ID", "name": "Jim Carrey"},
        {"id": "JIMHELLCATS_ID", "name": "Jim and the Hellcats"},
    )

    stats = spotify_public.artist_stats_v2(
        tidal_artist_id="99",
        tidal_artist_name="Jim",
        sample_isrcs=["USXXX0001"],
    )

    assert stats is None, (
        "fallback accepted a soundalike artist whose name doesn't "
        "match exactly — should have returned None"
    )
    artist.get_artist.assert_not_called()
