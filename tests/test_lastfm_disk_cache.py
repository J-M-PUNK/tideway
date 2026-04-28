"""Tests for the SQLite-backed Last.fm cache.

The motivating case is the resolved chart whose cold load is ~18 s.
With this cache, that work survives app restart instead of dying
with the in-memory dict. Tests cover the round-trip, TTL expiry,
the schema-version sentinel that wipes incompatible payloads on
upgrade, and the JSON-serialization guard.
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from app import lastfm_disk_cache


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Redirect the cache to a per-test sqlite file. The module
    re-resolves `_db_path` on every call (no module-level connection
    is held), so a monkeypatch is enough."""
    db_path = tmp_path / "lastfm_disk_cache.db"
    monkeypatch.setattr(lastfm_disk_cache, "_db_path", db_path)
    return db_path


def test_set_then_get_roundtrips(isolated_db):
    lastfm_disk_cache.set("alice|chart-top-tracks-resolved:50", [{"id": "1"}])
    out = lastfm_disk_cache.get(
        "alice|chart-top-tracks-resolved:50", ttl_sec=3600
    )
    assert out == [{"id": "1"}]


def test_get_returns_none_for_missing_key(isolated_db):
    assert (
        lastfm_disk_cache.get("alice|nonexistent", ttl_sec=3600) is None
    )


def test_get_returns_none_when_expired(isolated_db):
    """A row whose `fetched_at` is older than the caller's ttl
    counts as a miss. Caller refetches and the next set() overwrites."""
    lastfm_disk_cache.set("alice|short-lived", "value")
    # Manually backdate the row by one hour and check a 5-second TTL.
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "UPDATE entries SET fetched_at = ? WHERE key = 'alice|short-lived'",
        (int(time.time()) - 3600,),
    )
    conn.commit()
    conn.close()
    assert lastfm_disk_cache.get("alice|short-lived", ttl_sec=5) is None


def test_get_honors_long_ttl(isolated_db):
    """Same backdating but a TTL longer than the row's age — the row
    should still be served. Sanity check that the comparison goes
    the right way."""
    lastfm_disk_cache.set("alice|fresh", "value")
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "UPDATE entries SET fetched_at = ? WHERE key = 'alice|fresh'",
        (int(time.time()) - 60,),
    )
    conn.commit()
    conn.close()
    assert lastfm_disk_cache.get("alice|fresh", ttl_sec=3600) == "value"


def test_clear_drops_all_entries(isolated_db):
    """Disconnect / re-auth uses this. Two accounts' rows must not
    leak across a clear."""
    lastfm_disk_cache.set("alice|x", 1)
    lastfm_disk_cache.set("bob|y", 2)
    lastfm_disk_cache.clear()
    assert lastfm_disk_cache.get("alice|x", ttl_sec=3600) is None
    assert lastfm_disk_cache.get("bob|y", ttl_sec=3600) is None


def test_set_overwrites_existing_key(isolated_db):
    lastfm_disk_cache.set("alice|k", "first")
    lastfm_disk_cache.set("alice|k", "second")
    assert lastfm_disk_cache.get("alice|k", ttl_sec=3600) == "second"


def test_set_skips_non_serializable_value(isolated_db, caplog):
    """Pathological caller hands us something json.dumps can't
    handle. The cache must skip the write rather than crash the
    request — caching is a perf optimization, not correctness."""

    class NotSerializable:
        pass

    lastfm_disk_cache.set("alice|bad", NotSerializable())
    # Nothing was persisted; subsequent get() returns None.
    assert lastfm_disk_cache.get("alice|bad", ttl_sec=3600) is None


def test_get_skips_corrupt_row(isolated_db):
    """A row whose JSON is corrupt (manual DB edit, half-written
    file from a crashed sqlite session) returns None instead of
    raising. The next set() overwrites."""
    # Open a connection that bypasses the module's set() so we can
    # write garbage.
    conn = lastfm_disk_cache._connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO entries (key, payload, fetched_at) "
            "VALUES (?, ?, ?)",
            ("alice|corrupt", "{not-json", int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()
    assert lastfm_disk_cache.get("alice|corrupt", ttl_sec=3600) is None


def test_get_returns_none_for_empty_key(isolated_db):
    """The cache key is `{username}|{endpoint-key}`. An empty key
    can only happen via a programming bug; defend against it
    rather than letting an empty-string row leak across users."""
    assert lastfm_disk_cache.get("", ttl_sec=3600) is None


def test_set_skips_empty_key(isolated_db):
    """Force the DB to exist via a real set first, then verify the
    empty-key set didn't add a second row."""
    lastfm_disk_cache.set("alice|valid", "ok")
    lastfm_disk_cache.set("", "anything")
    conn = sqlite3.connect(str(isolated_db))
    rows = conn.execute("SELECT COUNT(*) FROM entries").fetchone()
    conn.close()
    assert rows[0] == 1  # only the valid entry from above


def test_schema_bump_wipes_existing_rows(tmp_path, monkeypatch):
    """When we bump _SCHEMA_VERSION because the cached payload shape
    changed, opening the DB on the new code must drop the stale rows.
    Same pattern app/spotify_public.py uses."""
    db_path = tmp_path / "lastfm_disk_cache.db"
    monkeypatch.setattr(lastfm_disk_cache, "_db_path", db_path)
    # Build a DB at the OLD schema version (0 = pre-sentinel) with
    # a row that the new code might not understand.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE entries (
            key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            fetched_at INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO entries VALUES ('alice|old', ?, ?)",
        (json.dumps({"old_shape": True}), int(time.time())),
    )
    conn.commit()
    conn.close()
    # Bump the in-process schema version above what's persisted, then
    # reopen via the production path.
    monkeypatch.setattr(lastfm_disk_cache, "_SCHEMA_VERSION", 99)
    lastfm_disk_cache._connect().close()
    # Row from the old schema should be gone; the new sentinel value
    # should be in cache_meta.
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM entries"
        ).fetchone()
        assert row[0] == 0
        ver = conn.execute(
            "SELECT value FROM cache_meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert int(ver) == 99
    finally:
        conn.close()
