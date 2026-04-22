"""Enrichment via Spotify's public GraphQL endpoints.

Last.fm stays the source of truth for PERSONAL listening data —
the user's own scrobbles, their top artists across a week / month /
year, their loved tracks. Spotify's public numbers complement that
with GLOBAL popularity signals that Last.fm doesn't have a meaningful
analog for:

  - Per-track total play counts (in the billions for popular songs)
  - Per-artist monthly listeners, follower count, world rank
  - Per-artist top listening cities (city + country + listener count)

None of that is a substitute for Last.fm; they're two sides of the
same stats page.

We reach Spotify through `spotapi` (https://github.com/Aran404/SpotAPI),
which wraps the private GraphQL API Spotify's own Web Player uses.
No OAuth, no user account — just anonymous requests through a
TLS-fingerprint-spoofed client (Chrome 120) to get past Spotify's
anti-bot filters.

**Matching Tidal → Spotify.** Tidal and Spotify don't share IDs, so
every lookup starts from an ISRC (International Standard Recording
Code — a universal track identifier that both services publish).
For tracks: search Spotify by `isrc:<code>`, pick the first hit,
fetch its getTrack GraphQL → playcount. For artists: do the track
lookup for any of their tracks, walk to the track's primary artist,
fetch queryArtistOverview GraphQL → monthly listeners + cities.

**Caching.** All results go into a SQLite DB at
`user_data_dir()/spotify_public_cache.db`. ID→ID maps (ISRC →
Spotify track, Tidal artist → Spotify artist) persist forever since
IDs don't change. Numeric stats (playcount, monthly listeners)
expire after 7 days — long enough that we don't hammer Spotify,
short enough that numbers don't go stale by more than a week.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from app.paths import user_data_dir

log = logging.getLogger(__name__)

# TTL for numeric stats. Monthly listeners + playcounts change slowly
# (drift of <1% per day for most artists), so a week is plenty —
# and keeps us well under any reasonable per-day request budget.
_STATS_TTL_SEC = 7 * 24 * 3600

# SpotAPI loads a bunch of unused deps at import time (pymongo, redis,
# etc.) so we keep it lazy — a bare `from app.spotify_public import …`
# at server startup shouldn't pay for it.
_client_lock = threading.Lock()
_artist_mod: Any = None
_song_mod: Any = None
_tls_client: Any = None


def _stub_unused_spotapi_deps() -> None:
    """Install empty-module stubs for pymongo + redis before the
    first spotapi import.

    spotapi/utils/saver.py has unconditional `import pymongo` /
    `import redis` at module level. Both are only referenced inside
    `MongoSaver` and `RedisSaver` classes we never instantiate — the
    imports just need to resolve for spotapi itself to load. Empty
    `types.ModuleType` objects satisfy that, letting us drop the
    real libraries (~15 MB combined) from requirements.txt.

    Idempotent — once stubbed, subsequent calls are no-ops.
    """
    import sys
    import types as _types

    for name in ("pymongo", "redis"):
        if name not in sys.modules:
            sys.modules[name] = _types.ModuleType(name)


def _ensure_client() -> tuple[Any, Any]:
    """Return a (song, artist) pair lazily. Thread-safe singleton."""
    global _artist_mod, _song_mod, _tls_client
    with _client_lock:
        if _artist_mod is not None:
            return _song_mod, _artist_mod
        _stub_unused_spotapi_deps()
        from spotapi.artist import Artist  # type: ignore
        from spotapi.client import TLSClient  # type: ignore
        from spotapi.song import Song  # type: ignore

        _tls_client = TLSClient("chrome_120", "", auto_retries=2)
        _song_mod = Song(client=_tls_client)
        _artist_mod = Artist(client=_tls_client)
        return _song_mod, _artist_mod


# ---------------------------------------------------------------------------
# SQLite cache
# ---------------------------------------------------------------------------


_db_lock = threading.Lock()
_db_path = user_data_dir() / "spotify_public_cache.db"


def _db() -> sqlite3.Connection:
    """Open the cache DB. Tables are created on demand; migrations
    are additive (new columns via ALTER TABLE) so an older DB from a
    previous version still opens.
    """
    conn = sqlite3.connect(str(_db_path), timeout=5.0)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS isrc_to_spotify_track ("
        "  isrc TEXT PRIMARY KEY,"
        "  spotify_track_id TEXT,"
        "  fetched_at INTEGER"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tidal_to_spotify_artist ("
        "  tidal_artist_id TEXT PRIMARY KEY,"
        "  spotify_artist_id TEXT,"
        "  fetched_at INTEGER"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS track_playcount ("
        "  isrc TEXT PRIMARY KEY,"
        "  playcount INTEGER,"
        "  fetched_at INTEGER"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS artist_stats ("
        "  spotify_artist_id TEXT PRIMARY KEY,"
        "  payload TEXT,"
        "  fetched_at INTEGER"
        ")"
    )
    return conn


# ---------------------------------------------------------------------------
# Public accessors
# ---------------------------------------------------------------------------


@dataclass
class ArtistStats:
    spotify_artist_id: str
    name: str
    monthly_listeners: Optional[int]
    followers: Optional[int]
    world_rank: Optional[int]
    top_cities: list[dict]  # [{city, country, listeners}]

    def to_dict(self) -> dict:
        return {
            "spotify_artist_id": self.spotify_artist_id,
            "name": self.name,
            "monthly_listeners": self.monthly_listeners,
            "followers": self.followers,
            "world_rank": self.world_rank,
            "top_cities": self.top_cities,
        }


def playcount_by_isrc(isrc: str) -> Optional[int]:
    """Return Spotify's global play count for the track identified
    by `isrc`, or None if Spotify doesn't know the track or we can't
    reach them.

    Cached 7 days — fresh enough that the user doesn't see stale
    data, infrequent enough that Spotify doesn't rate-limit us.
    """
    isrc = (isrc or "").strip().upper()
    if not isrc:
        return None

    with _db_lock:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT playcount, fetched_at FROM track_playcount WHERE isrc=?",
                (isrc,),
            ).fetchone()
        finally:
            conn.close()
    if row is not None and row[1] and (time.time() - row[1]) < _STATS_TTL_SEC:
        return int(row[0]) if row[0] is not None else None

    track_id = _isrc_to_spotify_track(isrc)
    if not track_id:
        _write_track_playcount(isrc, None)
        return None
    try:
        payload = _song_info(track_id)
    except Exception as exc:
        log.warning("spotify getTrack failed for isrc=%s: %s", isrc, exc)
        return None
    pc_raw = (payload.get("data") or {}).get("trackUnion", {}).get("playcount")
    try:
        pc = int(pc_raw) if pc_raw is not None else None
    except (TypeError, ValueError):
        pc = None
    _write_track_playcount(isrc, pc)
    return pc


def album_total_plays(isrcs: list[str]) -> dict:
    """Sum Spotify play counts across an album's tracks.

    Returns `{total_plays, resolved, total}`:
      - total_plays: sum of playcounts across all tracks Spotify
        could resolve
      - resolved:    how many ISRCs got a non-None playcount
      - total:       how many ISRCs we tried

    Lookups run in parallel — a 15-track album would take ~7s
    serially (ISRC search + getTrack round-trip per track) but
    parallelizes to ~1.5s with max_workers=5. The underlying
    TLSClient from spotapi is thread-safe; the SQLite cache is
    serialised via `_db_lock`, so concurrent inserts are safe.

    A partial result (resolved < total) still produces a sum — when
    Spotify is missing a few tracks the number is an under-estimate,
    but rendering "4.8B+" is better than showing nothing. The caller
    can check `resolved / total` to decide whether to annotate the
    number with a "(partial)" hint.
    """
    from concurrent.futures import ThreadPoolExecutor

    cleaned = [i.strip().upper() for i in isrcs if i and i.strip()]
    if not cleaned:
        return {"total_plays": 0, "resolved": 0, "total": 0}

    def _one(isrc: str) -> Optional[int]:
        try:
            return playcount_by_isrc(isrc)
        except Exception as exc:
            log.warning("playcount lookup raised for %s: %s", isrc, exc)
            return None

    # Cap at 5 workers so we stay well under any plausible Spotify
    # per-origin rate limit. Five covers the typical 12-15-track
    # album in ~two round-trip waves.
    with ThreadPoolExecutor(max_workers=min(5, len(cleaned))) as pool:
        results = list(pool.map(_one, cleaned))

    total_plays = 0
    resolved = 0
    for pc in results:
        if pc is not None and pc > 0:
            total_plays += pc
            resolved += 1
    return {
        "total_plays": total_plays,
        "resolved": resolved,
        "total": len(cleaned),
    }


def artist_stats(
    tidal_artist_id: str, sample_isrc: str
) -> Optional[ArtistStats]:
    """Return Spotify's artist-level stats for a Tidal artist,
    resolved via a sample ISRC from that artist's catalog.

    `tidal_artist_id` is what the cache keys on — once resolved, the
    Tidal → Spotify artist mapping persists forever. `sample_isrc`
    is only used the first time (or after a failed lookup).
    """
    spotify_artist_id = _tidal_to_spotify_artist(tidal_artist_id, sample_isrc)
    if not spotify_artist_id:
        return None

    with _db_lock:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT payload, fetched_at FROM artist_stats "
                "WHERE spotify_artist_id=?",
                (spotify_artist_id,),
            ).fetchone()
        finally:
            conn.close()
    if row is not None and row[1] and (time.time() - row[1]) < _STATS_TTL_SEC:
        try:
            return ArtistStats(**json.loads(row[0]))
        except Exception:
            pass  # fall through and refetch

    try:
        _, artist = _ensure_client()
        payload = artist.get_artist(spotify_artist_id)
    except Exception as exc:
        log.warning(
            "spotify queryArtistOverview failed for %s: %s",
            spotify_artist_id, exc,
        )
        return None

    union = (payload.get("data") or {}).get("artistUnion") or {}
    profile = union.get("profile") or {}
    stats = union.get("stats") or {}
    cities_raw = (stats.get("topCities") or {}).get("items") or []
    cities: list[dict] = []
    for c in cities_raw[:5]:
        try:
            cities.append(
                {
                    "city": str(c.get("city") or ""),
                    "country": str(c.get("country") or ""),
                    "listeners": int(c.get("numberOfListeners") or 0),
                }
            )
        except (TypeError, ValueError):
            continue

    result = ArtistStats(
        spotify_artist_id=spotify_artist_id,
        name=str(profile.get("name") or ""),
        monthly_listeners=_safe_int(stats.get("monthlyListeners")),
        followers=_safe_int(stats.get("followers")),
        world_rank=_safe_int(stats.get("worldRank")),
        top_cities=cities,
    )
    with _db_lock:
        conn = _db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO artist_stats "
                "(spotify_artist_id, payload, fetched_at) VALUES (?, ?, ?)",
                (
                    spotify_artist_id,
                    json.dumps(result.to_dict()),
                    int(time.time()),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return result


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _isrc_to_spotify_track(isrc: str) -> Optional[str]:
    """Resolve an ISRC to a Spotify track id. Permanently cached —
    ISRCs are stable."""
    with _db_lock:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT spotify_track_id FROM isrc_to_spotify_track "
                "WHERE isrc=?",
                (isrc,),
            ).fetchone()
        finally:
            conn.close()
    if row is not None:
        return row[0]

    try:
        song, _ = _ensure_client()
        res = song.query_songs(f"isrc:{isrc}", limit=5)
    except Exception as exc:
        log.warning("spotify isrc search failed for %s: %s", isrc, exc)
        return None

    items = (
        (res.get("data") or {})
        .get("searchV2", {})
        .get("tracksV2", {})
        .get("items")
        or []
    )
    spotify_id: Optional[str] = None
    for entry in items:
        item = (entry.get("item") or {}).get("data") or {}
        uri = item.get("uri") or ""
        if uri.startswith("spotify:track:"):
            spotify_id = uri.split(":")[-1]
            break

    with _db_lock:
        conn = _db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO isrc_to_spotify_track "
                "(isrc, spotify_track_id, fetched_at) VALUES (?, ?, ?)",
                (isrc, spotify_id, int(time.time())),
            )
            conn.commit()
        finally:
            conn.close()
    return spotify_id


def _tidal_to_spotify_artist(
    tidal_artist_id: str, sample_isrc: str
) -> Optional[str]:
    """Resolve a Tidal artist id to a Spotify artist id. Persistent
    cache — artists don't change id. The sample ISRC is only used
    on cache miss."""
    tidal_artist_id = str(tidal_artist_id)
    with _db_lock:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT spotify_artist_id FROM tidal_to_spotify_artist "
                "WHERE tidal_artist_id=?",
                (tidal_artist_id,),
            ).fetchone()
        finally:
            conn.close()
    if row is not None:
        return row[0]

    # Chain: ISRC → Spotify track → primary artist on that track.
    spotify_track_id = _isrc_to_spotify_track(sample_isrc or "")
    spotify_artist_id: Optional[str] = None
    if spotify_track_id:
        try:
            payload = _song_info(spotify_track_id)
            union = (payload.get("data") or {}).get("trackUnion") or {}
            # The primary-artist list shape varies between GraphQL
            # response versions. Try both the new (`firstArtist`) and
            # the legacy (`artists`) keys.
            first_items = (
                (union.get("firstArtist") or {}).get("items")
                or (union.get("artists") or {}).get("items")
                or []
            )
            for item in first_items:
                uri = item.get("uri") or ""
                if uri.startswith("spotify:artist:"):
                    spotify_artist_id = uri.split(":")[-1]
                    break
        except Exception as exc:
            log.warning(
                "spotify getTrack→artist walk failed for %s: %s",
                spotify_track_id, exc,
            )

    with _db_lock:
        conn = _db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO tidal_to_spotify_artist "
                "(tidal_artist_id, spotify_artist_id, fetched_at) "
                "VALUES (?, ?, ?)",
                (tidal_artist_id, spotify_artist_id, int(time.time())),
            )
            conn.commit()
        finally:
            conn.close()
    return spotify_artist_id


def _song_info(spotify_track_id: str) -> Mapping[str, Any]:
    """Call Spotify's getTrack GraphQL via SpotAPI's `Song`."""
    song, _ = _ensure_client()
    return song.get_track_info(spotify_track_id)


def _write_track_playcount(isrc: str, playcount: Optional[int]) -> None:
    with _db_lock:
        conn = _db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO track_playcount "
                "(isrc, playcount, fetched_at) VALUES (?, ?, ?)",
                (isrc, playcount, int(time.time())),
            )
            conn.commit()
        finally:
            conn.close()


def _safe_int(v: object) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ArtistStats",
    "playcount_by_isrc",
    "album_total_plays",
    "artist_stats",
]
