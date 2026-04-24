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

# TTL for negative caches. When Spotify can't resolve an ISRC, or a
# getTrack call fails transiently (rate limit, network blip, schema
# drift), we cache the miss for a day so the next view retries
# instead of sitting dark for a full week.
_NULL_TTL_SEC = 24 * 3600

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
    if row is not None and row[1]:
        age = time.time() - row[1]
        pc_cached = row[0]
        # Treat cached `0` like a null: fresh-release tracks often
        # show playcount=0 for a day or two after release before
        # Spotify surfaces the real number. Holding that 0 for the
        # full 7 days leaves brand-new charting tracks permanently
        # reading zero even as they rocket into the millions. Refresh
        # on the short TTL so the number catches up.
        is_zeroish = pc_cached is None or int(pc_cached or 0) == 0
        if not is_zeroish and age < _STATS_TTL_SEC:
            return int(pc_cached)
        if is_zeroish and age < _NULL_TTL_SEC:
            return int(pc_cached) if pc_cached is not None else None

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
    """Resolve an ISRC to a Spotify track id.

    Popular songs have several Spotify entries for the same ISRC
    (the single release, the album version, deluxe editions, regional
    reissues), each with its own playcount. The canonical version
    can have 2B plays while a reissue has 40M — so picking the first
    search hit was a coin flip. When the search returns more than
    one candidate we fetch each track's playcount and keep the
    highest. The getTrack calls are bounded by the search's limit=5
    and only happen once per ISRC (result cached forever).

    Successful resolutions cache permanently. Misses are cached for
    a day, so a failed lookup (rate limit, transient network, schema
    drift) doesn't leave the track blank for a week.
    """
    with _db_lock:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT spotify_track_id, fetched_at FROM isrc_to_spotify_track "
                "WHERE isrc=?",
                (isrc,),
            ).fetchone()
        finally:
            conn.close()
    if row is not None:
        cached_id, fetched_at = row[0], row[1]
        if cached_id is not None:
            return cached_id
        if fetched_at and (time.time() - fetched_at) < _NULL_TTL_SEC:
            return None
        # Stale negative — fall through to retry.

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
    candidate_ids: list[str] = []
    for entry in items:
        item = (entry.get("item") or {}).get("data") or {}
        uri = item.get("uri") or ""
        if uri.startswith("spotify:track:"):
            candidate_ids.append(uri.split(":")[-1])

    spotify_id: Optional[str] = None
    if len(candidate_ids) == 1:
        spotify_id = candidate_ids[0]
    elif len(candidate_ids) > 1:
        # Rank candidates by playcount. Fallback to the first if no
        # candidate's playcount comes back (schema drift, all getTrack
        # calls failed) so we still have a track id for artist-
        # resolution callers.
        best_id = candidate_ids[0]
        best_pc = -1
        for track_id in candidate_ids:
            try:
                payload = _song_info(track_id)
            except Exception as exc:
                log.warning(
                    "spotify getTrack failed during ranking for %s: %s",
                    track_id, exc,
                )
                continue
            pc_raw = (payload.get("data") or {}).get("trackUnion", {}).get("playcount")
            try:
                pc = int(pc_raw) if pc_raw is not None else None
            except (TypeError, ValueError):
                pc = None
            if pc is not None and pc > best_pc:
                best_pc = pc
                best_id = track_id
        spotify_id = best_id

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
    """Resolve a Tidal artist id to a Spotify artist id. Successful
    mappings cache forever (artist ids don't change). Negative hits
    expire after a day so a transient flake (no ISRC available, ISRC
    search rate-limited) doesn't leave the artist permanently
    un-resolvable."""
    tidal_artist_id = str(tidal_artist_id)
    with _db_lock:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT spotify_artist_id, fetched_at FROM tidal_to_spotify_artist "
                "WHERE tidal_artist_id=?",
                (tidal_artist_id,),
            ).fetchone()
        finally:
            conn.close()
    if row is not None:
        cached_id, fetched_at = row[0], row[1]
        if cached_id is not None:
            return cached_id
        if fetched_at and (time.time() - fetched_at) < _NULL_TTL_SEC:
            return None
        # Stale negative — fall through and retry.

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


def purge_null_playcounts(codes: list[str]) -> None:
    """Drop null-or-zero cached playcounts for the given ISRCs so the
    next lookup retries Spotify. Used by `?refresh=true` on the batch
    endpoint when a page wants complete data badly enough to eat the
    extra round-trips.
    """
    if not codes:
        return
    with _db_lock:
        conn = _db()
        try:
            placeholders = ",".join("?" for _ in codes)
            conn.execute(
                f"DELETE FROM track_playcount WHERE "
                f"(playcount IS NULL OR playcount = 0) "
                f"AND isrc IN ({placeholders})",
                codes,
            )
            conn.commit()
        finally:
            conn.close()


def _write_track_playcount(isrc: str, playcount: Optional[int]) -> None:
    _write_cache_rows(
        (
            "INSERT OR REPLACE INTO track_playcount "
            "(isrc, playcount, fetched_at) VALUES (?, ?, ?)",
            (isrc, playcount, int(time.time())),
        ),
    )


def _write_isrc_mapping(isrc: str, spotify_track_id: Optional[str]) -> None:
    _write_cache_rows(
        (
            "INSERT OR REPLACE INTO isrc_to_spotify_track "
            "(isrc, spotify_track_id, fetched_at) VALUES (?, ?, ?)",
            (isrc, spotify_track_id, int(time.time())),
        ),
    )


def _write_cache_rows(*statements: tuple[str, tuple]) -> None:
    """Execute one or more INSERT/UPDATE statements against the cache
    DB inside a single connection + lock acquire, so callers writing
    to multiple tables in one logical step don't pay for two
    connection opens and two CREATE-TABLE sweeps."""
    if not statements:
        return
    with _db_lock:
        conn = _db()
        try:
            for sql, params in statements:
                conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


def _safe_int(v: object) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def playcount_with_fallback(
    isrc: str, title: str, artist: str
) -> Optional[int]:
    """Like playcount_by_isrc, but on ISRC miss (Spotify doesn't index
    this specific recording) falls back to a title + primary-artist
    search. Used by the Popular page's batch fetch where we have
    the Tidal track's full metadata anyway.

    Match is strict: exact title, exact primary-artist name. The
    resulting playcount is cached under the original ISRC so the next
    visit doesn't repeat the fuzzy search.
    """
    isrc = (isrc or "").strip().upper()
    if not isrc:
        return None

    pc = playcount_by_isrc(isrc)
    if pc is not None and pc > 0:
        return pc

    # ISRC path returned null/0. Common causes: Spotify hasn't
    # indexed this specific release (feature-version ISRCs often
    # miss), track was released too recently for playcounts to
    # aggregate, or the ISRC is a Tidal-only reissue. If we have
    # the title + artist we can search Spotify for the canonical
    # version instead.
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title or not artist:
        return pc

    try:
        song, _ = _ensure_client()
        res = song.query_songs(f"{artist} {title}", limit=5)
    except Exception as exc:
        log.warning(
            "spotify fallback search failed for %s / %s: %s",
            title, artist, exc,
        )
        return pc

    items = (
        (res.get("data") or {})
        .get("searchV2", {})
        .get("tracksV2", {})
        .get("items")
        or []
    )
    wanted_title = title.lower()
    wanted_artist = artist.lower()

    best_pc = -1
    best_id: Optional[str] = None
    for entry in items[:5]:
        item = (entry.get("item") or {}).get("data") or {}
        uri = item.get("uri") or ""
        if not uri.startswith("spotify:track:"):
            continue
        name = str(item.get("name") or "")
        if name.lower() != wanted_title:
            continue
        track_id = uri.split(":")[-1]
        # Confirm primary artist + read playcount via getTrack — more
        # reliable than the search's inline artist block which varies
        # by response version.
        try:
            payload = _song_info(track_id)
        except Exception:
            continue
        union = (payload.get("data") or {}).get("trackUnion") or {}
        first_items = (
            (union.get("firstArtist") or {}).get("items")
            or (union.get("artists") or {}).get("items")
            or []
        )
        if not first_items:
            continue
        first_name = str(
            (first_items[0].get("profile") or {}).get("name")
            or first_items[0].get("name")
            or ""
        )
        if first_name.lower() != wanted_artist:
            continue
        pc_raw = union.get("playcount")
        try:
            cand_pc = int(pc_raw) if pc_raw is not None else None
        except (TypeError, ValueError):
            cand_pc = None
        if cand_pc is not None and cand_pc > best_pc:
            best_pc = cand_pc
            best_id = track_id

    if best_id is not None and best_pc > 0:
        # Cache the playcount AND the upgraded ISRC→track mapping in a
        # single transaction. The mapping write unblocks downstream
        # artist-resolution which walks `isrc_to_spotify_track` rather
        # than `track_playcount` — without it, a stale NULL from the
        # earlier direct-ISRC search would still force a re-resolve.
        now_s = int(time.time())
        _write_cache_rows(
            (
                "INSERT OR REPLACE INTO track_playcount "
                "(isrc, playcount, fetched_at) VALUES (?, ?, ?)",
                (isrc, best_pc, now_s),
            ),
            (
                "INSERT OR REPLACE INTO isrc_to_spotify_track "
                "(isrc, spotify_track_id, fetched_at) VALUES (?, ?, ?)",
                (isrc, best_id, now_s),
            ),
        )
        return best_pc
    return pc


def debug_resolve_artist(
    tidal_artist_id: str,
    tidal_artist_name: str,
    sample_isrcs: list[str],
) -> dict:
    """Run the Tidal → Spotify artist resolution end-to-end WITHOUT
    touching the cache, and return a structured report of what
    happened at each step. Used by the /api/debug/artist-resolve
    endpoint to diagnose artists whose monthly-listeners don't show
    up on the artist page.

    Does not mutate the cache. Exceptions are captured into the
    report instead of raised.
    """
    report: dict = {
        "tidal_artist_id": str(tidal_artist_id),
        "tidal_artist_name": tidal_artist_name,
        "sample_isrcs_available": list(sample_isrcs),
        "steps": [],
        "selected_spotify_artist_id": None,
        "selected_spotify_artist_name": None,
        "name_match": None,
        "monthly_listeners": None,
        "followers": None,
        "world_rank": None,
        "verdict": "unknown",
        "errors": [],
    }

    if not sample_isrcs:
        report["verdict"] = "no_isrc"
        report["steps"].append(
            {"stage": "isrc-selection", "detail": "No ISRCs on any top track"}
        )
        return report

    wanted_name_norm = (tidal_artist_name or "").strip().lower()

    selected_spotify_artist_id: Optional[str] = None
    selected_source_isrc: Optional[str] = None
    selected_track_artists: list[str] = []

    for isrc in sample_isrcs[:5]:
        isrc_clean = (isrc or "").strip().upper()
        if not isrc_clean:
            continue

        step: dict = {
            "stage": "isrc-resolve",
            "isrc": isrc_clean,
            "search_candidates": [],
            "picked_track_id": None,
            "picked_track_artists": [],
            "first_artist_uri": None,
        }

        try:
            song, _ = _ensure_client()
            res = song.query_songs(f"isrc:{isrc_clean}", limit=5)
        except Exception as exc:
            step["error"] = f"search failed: {exc!r}"
            report["steps"].append(step)
            continue

        items = (
            (res.get("data") or {})
            .get("searchV2", {})
            .get("tracksV2", {})
            .get("items")
            or []
        )
        for entry in items:
            item = (entry.get("item") or {}).get("data") or {}
            uri = item.get("uri") or ""
            if not uri.startswith("spotify:track:"):
                continue
            step["search_candidates"].append(
                {
                    "track_id": uri.split(":")[-1],
                    "name": item.get("name") or "",
                }
            )

        if not step["search_candidates"]:
            step["detail"] = "Spotify returned no track candidates for this ISRC"
            report["steps"].append(step)
            continue

        # Walk the first candidate to get its artists. (Ranking doesn't
        # affect artist resolution — any candidate's primary-artist
        # walk lands on the same artist, so we pick the first to keep
        # this cheap and deterministic.)
        picked_id = step["search_candidates"][0]["track_id"]
        step["picked_track_id"] = picked_id
        try:
            payload = _song_info(picked_id)
        except Exception as exc:
            step["error"] = f"getTrack failed: {exc!r}"
            report["steps"].append(step)
            continue

        union = (payload.get("data") or {}).get("trackUnion") or {}
        first_items = (
            (union.get("firstArtist") or {}).get("items")
            or (union.get("artists") or {}).get("items")
            or []
        )
        artist_uri: Optional[str] = None
        for item in first_items:
            uri = item.get("uri") or ""
            name = str(item.get("profile", {}).get("name") or item.get("name") or "")
            if name:
                step["picked_track_artists"].append(name)
            if not artist_uri and uri.startswith("spotify:artist:"):
                artist_uri = uri
                step["first_artist_uri"] = uri

        if not artist_uri:
            step["detail"] = "No spotify:artist URI found on the track"
            report["steps"].append(step)
            continue

        candidate_artist_id = artist_uri.split(":")[-1]
        step["candidate_spotify_artist_id"] = candidate_artist_id

        # If this candidate's first listed name matches the Tidal
        # artist's name we're done. Otherwise keep the first resolve
        # as a fallback and try the next ISRC, which might come from
        # a track where THIS artist is primary instead of a featured
        # collaborator.
        name_matches = (
            len(step["picked_track_artists"]) > 0
            and step["picked_track_artists"][0].strip().lower() == wanted_name_norm
        )
        step["name_matches_tidal"] = name_matches

        if selected_spotify_artist_id is None:
            selected_spotify_artist_id = candidate_artist_id
            selected_source_isrc = isrc_clean
            selected_track_artists = list(step["picked_track_artists"])

        if name_matches:
            selected_spotify_artist_id = candidate_artist_id
            selected_source_isrc = isrc_clean
            selected_track_artists = list(step["picked_track_artists"])
            report["steps"].append(step)
            break

        report["steps"].append(step)

    if not selected_spotify_artist_id:
        report["verdict"] = "no_spotify_match"
        return report

    report["selected_spotify_artist_id"] = selected_spotify_artist_id
    report["selected_source_isrc"] = selected_source_isrc
    report["selected_track_artists"] = selected_track_artists
    report["name_match"] = (
        bool(selected_track_artists)
        and selected_track_artists[0].strip().lower() == wanted_name_norm
    )

    try:
        _, artist = _ensure_client()
        payload = artist.get_artist(selected_spotify_artist_id)
    except Exception as exc:
        report["errors"].append(f"queryArtistOverview: {exc!r}")
        report["verdict"] = "artist_overview_failed"
        return report

    union = (payload.get("data") or {}).get("artistUnion") or {}
    profile = union.get("profile") or {}
    stats = union.get("stats") or {}
    report["selected_spotify_artist_name"] = str(profile.get("name") or "")
    report["monthly_listeners"] = _safe_int(stats.get("monthlyListeners"))
    report["followers"] = _safe_int(stats.get("followers"))
    report["world_rank"] = _safe_int(stats.get("worldRank"))
    report["stats_keys_present"] = sorted(stats.keys()) if isinstance(stats, dict) else []

    if report["monthly_listeners"] is None:
        report["verdict"] = "no_monthly_listeners"
    elif not report["name_match"]:
        report["verdict"] = "wrong_artist_possible"
    else:
        report["verdict"] = "ok"
    return report


__all__ = [
    "ArtistStats",
    "playcount_by_isrc",
    "playcount_with_fallback",
    "purge_null_playcounts",
    "album_total_plays",
    "artist_stats",
    "debug_resolve_artist",
]
