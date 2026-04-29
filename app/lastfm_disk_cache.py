"""Disk-backed cache for expensive Last.fm-derived endpoints.

The Last.fm endpoints in `server.py` use an in-memory dict cache
(`_lastfm_cache`) with a 5-minute TTL by default. That's plenty for
most callers. The exception is `lastfm_chart_top_tracks_resolved`,
which fans out 50 sequential `tidal.search` calls server-side and
takes ~18 seconds on cold load. Its 1-hour TTL was already long, but
the in-memory dict dies on app restart, so anyone who quits Tideway
between visits paid the 18 seconds again.

This module is the disk layer. Same shape as `app.spotify_public`'s
SQLite cache: one DB file in `user_data_dir()`, atomic write through
SQLite's connection commit, and a schema-version sentinel that
invalidates rows when a code change makes them unsafe to reuse.

Storage shape: a single `entries` table keyed on the cache key
(`{username}|{endpoint-key}`) with a JSON-encoded value blob and a
fetched-at timestamp. JSON is fine for the chart-top-tracks-resolved
payload (a list of `track_to_dict` results, ~50 KB).

Errors are logged and swallowed everywhere — persistence is a
performance optimization, not correctness. A disk failure should
fall through to a fresh fetch, not crash the request.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Optional

from app.paths import user_data_dir

log = logging.getLogger(__name__)

_db_path = user_data_dir() / "lastfm_disk_cache.db"
_db_lock = threading.Lock()

# Bumped when a code change makes existing cached payloads unsafe
# (e.g. the resolver started returning a different track-dict shape).
# Wipes the entries table on first open after a bump so users see
# corrected data without having to clear by hand.
_SCHEMA_VERSION = 1


def _connect() -> sqlite3.Connection:
    """Open the DB. Tables are created on demand. A version sentinel
    in `cache_meta` lets us drop stale rows when the cached payload
    shape changes incompatibly. Caller must close.
    """
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_db_path), timeout=5.0)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache_meta ("
        "  key TEXT PRIMARY KEY,"
        "  value TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS entries ("
        "  key TEXT PRIMARY KEY,"
        "  payload TEXT NOT NULL,"
        "  fetched_at INTEGER NOT NULL"
        ")"
    )
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key='schema_version'"
    ).fetchone()
    stored = int(row[0]) if row and str(row[0]).isdigit() else 0
    if stored < _SCHEMA_VERSION:
        conn.execute("DELETE FROM entries")
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) "
            "VALUES ('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        conn.commit()
    return conn


def get(key: str, ttl_sec: float) -> Optional[Any]:
    """Return the cached value for `key` if it's still within
    `ttl_sec`, else None. Caller decides what counts as fresh —
    different endpoints have different TTLs."""
    if not key:
        return None
    with _db_lock:
        try:
            conn = _connect()
        except Exception:
            log.exception("lastfm-disk-cache open failed")
            return None
        try:
            row = conn.execute(
                "SELECT payload, fetched_at FROM entries WHERE key=?",
                (key,),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    payload, fetched_at = row
    if (time.time() - float(fetched_at)) >= ttl_sec:
        return None
    try:
        return json.loads(payload)
    except (TypeError, ValueError):
        # Corrupt row — fail open so the caller refetches and the
        # next set() overwrites it.
        log.warning("lastfm-disk-cache row %r had invalid JSON; ignoring", key)
        return None


def set(key: str, value: Any) -> None:
    """Persist `value` under `key`. JSON-encoded; if `value` isn't
    JSON-serializable the write is skipped (logged) so callers don't
    crash mid-request."""
    if not key:
        return
    try:
        payload = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        log.warning(
            "lastfm-disk-cache: value for key=%r isn't JSON-serializable; "
            "skipping persist",
            key,
        )
        return
    with _db_lock:
        try:
            conn = _connect()
        except Exception:
            log.exception("lastfm-disk-cache open failed")
            return
        try:
            conn.execute(
                "INSERT OR REPLACE INTO entries (key, payload, fetched_at) "
                "VALUES (?, ?, ?)",
                (key, payload, int(time.time())),
            )
            conn.commit()
        except Exception:
            log.exception("lastfm-disk-cache write failed for key=%r", key)
        finally:
            conn.close()


def clear() -> None:
    """Drop every persisted entry. Called from
    `_invalidate_lastfm_cache` so disconnect / re-auth doesn't serve
    a different account's data."""
    with _db_lock:
        try:
            conn = _connect()
        except Exception:
            log.exception("lastfm-disk-cache open failed")
            return
        try:
            conn.execute("DELETE FROM entries")
            conn.commit()
        except Exception:
            log.exception("lastfm-disk-cache clear failed")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Test helpers — `monkeypatch _db_path` to redirect the cache to a tmp dir.
# ---------------------------------------------------------------------------


def _set_db_path_for_testing(path: Any) -> None:
    """Redirect the on-disk cache file. Call only from pytest fixtures."""
    global _db_path
    _db_path = path
