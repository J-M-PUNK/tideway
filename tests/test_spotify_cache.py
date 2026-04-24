"""Tests for the Spotify-public SQLite cache helpers.

Focus: `purge_null_playcounts` is the self-healing mechanism that
lets the Popular page re-query stale zeros/nulls. If it regresses,
fresh-release tracks stay dark even after the root cause is fixed.
"""
import sqlite3
import time
from pathlib import Path

import pytest

from app import spotify_public


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Redirect the module-level _db_path to a tmp file so tests
    don't touch the real user-data cache. Returns the path for
    direct assertions."""
    db_path = tmp_path / "spotify_public_cache.db"
    monkeypatch.setattr(spotify_public, "_db_path", db_path)
    # First open creates the schema via _db().
    conn = spotify_public._db()
    conn.close()
    return db_path


def _seed(db_path: Path, rows: list[tuple[str, int | None]]) -> None:
    """Insert (isrc, playcount) pairs with a current timestamp."""
    conn = sqlite3.connect(str(db_path))
    conn.executemany(
        "INSERT INTO track_playcount (isrc, playcount, fetched_at) VALUES (?, ?, ?)",
        [(isrc, pc, int(time.time())) for isrc, pc in rows],
    )
    conn.commit()
    conn.close()


def _all(db_path: Path) -> dict[str, int | None]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT isrc, playcount FROM track_playcount ORDER BY isrc"
    ).fetchall()
    conn.close()
    return dict(rows)


def test_purges_null_entries(isolated_cache):
    _seed(isolated_cache, [("USABC1", None), ("USABC2", 5_000_000)])

    spotify_public.purge_null_playcounts(["USABC1", "USABC2"])

    remaining = _all(isolated_cache)
    assert "USABC1" not in remaining
    assert remaining["USABC2"] == 5_000_000


def test_purges_zero_entries(isolated_cache):
    """Zero is the release-week failure mode — should also be cleared
    so the next fetch re-queries."""
    _seed(isolated_cache, [("USABC1", 0), ("USABC2", 1)])

    spotify_public.purge_null_playcounts(["USABC1", "USABC2"])

    remaining = _all(isolated_cache)
    assert "USABC1" not in remaining
    assert remaining["USABC2"] == 1


def test_leaves_positive_untouched(isolated_cache):
    _seed(
        isolated_cache,
        [("USABC1", 100), ("USABC2", 5_000_000), ("USABC3", 1_000_000_000)],
    )

    spotify_public.purge_null_playcounts(["USABC1", "USABC2", "USABC3"])

    remaining = _all(isolated_cache)
    assert remaining == {
        "USABC1": 100,
        "USABC2": 5_000_000,
        "USABC3": 1_000_000_000,
    }


def test_only_affects_requested_isrcs(isolated_cache):
    """A null row for an ISRC not in the purge list must stay."""
    _seed(isolated_cache, [("USABC1", None), ("USXYZ9", None)])

    spotify_public.purge_null_playcounts(["USABC1"])

    remaining = _all(isolated_cache)
    assert "USABC1" not in remaining
    assert "USXYZ9" in remaining
    assert remaining["USXYZ9"] is None


def test_handles_empty_list(isolated_cache):
    """No-op must not raise and must not delete anything."""
    _seed(isolated_cache, [("USABC1", None), ("USABC2", 100)])

    spotify_public.purge_null_playcounts([])

    remaining = _all(isolated_cache)
    assert set(remaining.keys()) == {"USABC1", "USABC2"}


def test_mixed_payload(isolated_cache):
    """Realistic Popular-page scenario: 5 tracks, mix of states."""
    _seed(
        isolated_cache,
        [
            ("USABC1", None),   # stale null — flush
            ("USABC2", 0),      # release-week zero — flush
            ("USABC3", 100),    # genuine low-play — keep
            ("USABC4", 5_000_000),  # hit — keep
            ("USABC5", None),   # not in purge list — keep
        ],
    )

    spotify_public.purge_null_playcounts(
        ["USABC1", "USABC2", "USABC3", "USABC4"]
    )

    remaining = _all(isolated_cache)
    assert "USABC1" not in remaining
    assert "USABC2" not in remaining
    assert remaining["USABC3"] == 100
    assert remaining["USABC4"] == 5_000_000
    assert remaining["USABC5"] is None
