"""Null-aware artist_stats cache TTL.

The artist-overview cache long-cached a *null* monthly-listeners
result on the full 7-day TTL — so a single transient
queryArtistOverview failure (Spotify rotating the persisted-query
hash, a network blip) blanked every artist's monthly listeners for a
week, even after the upstream recovered. A null is a miss, not a real
"0 listeners", and must ride the short 1-day negative TTL like the
rest of the module's misses. These tests pin that.
"""
from __future__ import annotations

import json

from app import spotify_public as sp
from app.spotify_public import ArtistStats, _fresh_cached_artist_stats


def _row(stats: ArtistStats, age_sec: float):
    """A (payload, fetched_at) cache row `age_sec` seconds old."""
    import time

    return (json.dumps(stats.to_dict()), time.time() - age_sec)


def _real() -> ArtistStats:
    return ArtistStats(
        spotify_artist_id="abc",
        name="Real Artist",
        monthly_listeners=1_000_000,
        followers=500,
        world_rank=10,
        top_cities=[],
    )


def _null() -> ArtistStats:
    # What got cached when the overview fetch failed / came back empty.
    return ArtistStats(
        spotify_artist_id="abc",
        name="",
        monthly_listeners=None,
        followers=None,
        world_rank=None,
        top_cities=[],
    )


def test_real_stats_served_within_full_ttl():
    out = _fresh_cached_artist_stats(_row(_real(), age_sec=60))
    assert out is not None and out.monthly_listeners == 1_000_000


def test_real_stats_expire_after_full_ttl():
    assert (
        _fresh_cached_artist_stats(_row(_real(), age_sec=sp._STATS_TTL_SEC + 10))
        is None
    )


def test_null_stats_served_only_within_negative_ttl():
    # A fresh null (< 1 day) is honored so we don't refetch on every
    # pageview during a genuine outage window.
    assert _fresh_cached_artist_stats(_row(_null(), age_sec=60)) is not None


def test_null_stats_expire_after_negative_ttl_not_full_ttl():
    # The fix: a null older than the 1-day negative TTL is a miss ->
    # refetch, instead of sitting dark until the 7-day TTL. Pick an
    # age between the two TTLs to prove the null uses the short one.
    age = sp._NULL_TTL_SEC + 60
    assert age < sp._STATS_TTL_SEC  # guard: the two TTLs differ as expected
    assert _fresh_cached_artist_stats(_row(_null(), age_sec=age)) is None


def test_malformed_row_is_a_miss():
    assert _fresh_cached_artist_stats(("not json", 1.0)) is None
    assert _fresh_cached_artist_stats(None) is None
    assert _fresh_cached_artist_stats((json.dumps({}), 0)) is None  # no fetched_at
