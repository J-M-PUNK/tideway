"""Tests for the per-call TTL on `_lastfm_cached`.

The Popular tracks page resolves 50 Last.fm chart entries to Tidal
tracks, which is intentionally slow (~18 s cold load) to keep the
fan-out under Tidal's abuse threshold. The default 5-minute Last.fm
cache TTL meant a user revisiting the page after 5 minutes paid that
cost again. We added a `ttl_sec` parameter to `_lastfm_cached` and
the chart endpoint passes 1 hour. These tests pin both the default
TTL and the override path so a refactor can't silently regress either.
"""
from unittest.mock import patch

import server


def setup_function(_fn):
    """Each test starts with a clean cache so ordering doesn't matter."""
    server._lastfm_cache.clear()


def test_default_ttl_constant_is_300s():
    assert server._LASTFM_CACHE_TTL_SEC == 300.0


def test_first_call_invokes_fetch():
    fetch_count = [0]

    def fetch():
        fetch_count[0] += 1
        return "v1"

    with patch.object(server.lastfm, "status", return_value={"username": "u"}):
        assert server._lastfm_cached("k", fetch) == "v1"
        assert fetch_count[0] == 1


def test_second_call_within_default_ttl_hits_cache():
    fetch_count = [0]

    def fetch():
        fetch_count[0] += 1
        return "v1"

    with patch.object(server.lastfm, "status", return_value={"username": "u"}):
        server._lastfm_cached("k", fetch)
        server._lastfm_cached("k", fetch)
        assert fetch_count[0] == 1


def test_custom_ttl_extends_cache_window_past_default():
    """If a caller passes ttl_sec=3600 and the entry is 10 minutes old
    (past the default 300s but well under the override), the entry
    should still be a hit. This is the actual fix for the slow Popular
    tracks page."""
    fetch_count = [0]

    def fetch():
        fetch_count[0] += 1
        return "v1"

    with patch.object(server.lastfm, "status", return_value={"username": "u"}):
        server._lastfm_cached("k", fetch, ttl_sec=3600.0)
        assert fetch_count[0] == 1

        # Age the entry to look 10 minutes old.
        ts, val = server._lastfm_cache["u|k"]
        server._lastfm_cache["u|k"] = (ts - 600.0, val)

        server._lastfm_cached("k", fetch, ttl_sec=3600.0)
        assert fetch_count[0] == 1, "ttl_sec=3600 should keep the entry fresh"


def test_default_ttl_misses_after_5_minutes():
    """The default 5-minute window must still be 5 minutes — only the
    chart endpoint opted into the longer window."""
    fetch_count = [0]

    def fetch():
        fetch_count[0] += 1
        return "v1"

    with patch.object(server.lastfm, "status", return_value={"username": "u"}):
        server._lastfm_cached("k", fetch)
        assert fetch_count[0] == 1
        # Age 6 minutes.
        ts, val = server._lastfm_cache["u|k"]
        server._lastfm_cache["u|k"] = (ts - 360.0, val)

        server._lastfm_cached("k", fetch)
        assert fetch_count[0] == 2, "default 300s TTL should miss after 6 min"


def test_cache_keys_are_scoped_by_username():
    """Reconnecting to a different Last.fm account must not serve the
    previous user's cached data."""
    fetch_count = [0]

    def fetch():
        fetch_count[0] += 1
        return f"v{fetch_count[0]}"

    with patch.object(server.lastfm, "status", return_value={"username": "alice"}):
        assert server._lastfm_cached("k", fetch) == "v1"

    with patch.object(server.lastfm, "status", return_value={"username": "bob"}):
        # Different username, same key — must miss.
        assert server._lastfm_cached("k", fetch) == "v2"
        assert fetch_count[0] == 2


def test_invalidate_clears_all_entries():
    fetch_count = [0]

    def fetch():
        fetch_count[0] += 1
        return "v"

    with patch.object(server.lastfm, "status", return_value={"username": "u"}):
        server._lastfm_cached("a", fetch)
        server._lastfm_cached("b", fetch)
        assert fetch_count[0] == 2
        server._invalidate_lastfm_cache()
        server._lastfm_cached("a", fetch)
        server._lastfm_cached("b", fetch)
        assert fetch_count[0] == 4
