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
TLS-fingerprinted client (Chrome 131) to get past Spotify's
anti-bot filters. The TLS impersonation is provided by curl-cffi
(see `app.spotify_curl_session`), the same transport tidalapi uses;
spotapi's bundled `tls_client` Go-CGO DLL is replaced because its
runtime panics crashed the Windows app on first call.

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
    """Install no-op stubs for the spotapi modules we don't use.

    Delegates to `app.spotify_curl_session` which owns the full stub
    set (`tls_client`, `pymongo`, `redis`) — keeping stub definitions
    in one place alongside the curl-cffi adapter that replaces the
    broken `tls_client` transport. See that module's docstring for
    the rationale on each stub.

    Kept as a thin wrapper here so existing call-sites (tests
    in particular) continue to work, and so the import order is
    explicit at the only point in `spotify_public` that touches
    spotapi.
    """
    from app.spotify_curl_session import install_spotapi_dep_stubs

    install_spotapi_dep_stubs()


def _ensure_client() -> tuple[Any, Any]:
    """Return a (song, artist) pair lazily. Thread-safe singleton."""
    global _artist_mod, _song_mod, _tls_client
    with _client_lock:
        if _artist_mod is not None:
            return _song_mod, _artist_mod
        _stub_unused_spotapi_deps()
        from app.spotify_curl_session import CurlSpotifyClient
        from spotapi.artist import Artist  # type: ignore
        from spotapi.song import Song  # type: ignore

        _tls_client = CurlSpotifyClient(auto_retries=2)
        _song_mod = Song(client=_tls_client)
        _artist_mod = Artist(client=_tls_client)
        return _song_mod, _artist_mod


# ---------------------------------------------------------------------------
# SQLite cache
# ---------------------------------------------------------------------------


_db_lock = threading.Lock()
_db_path = user_data_dir() / "spotify_public_cache.db"

# Bumped when a code change makes existing cached data unreliable. The
# version sentinel at the top of `_db()` wipes the affected tables on
# first open after a bump so users see corrected numbers without
# having to clear the cache by hand.
#
# History:
#   1: pre-v0.4.6 (no sentinel; treated as version 0).
#   2: v0.4.6 resolver fix — pre-fix rows had wrong playcounts (Thriller
#      cached at 40M from a reissue ISRC) and wrong artist mappings
#      (Tidal artist resolved to a featured Spotify collaborator).
#      Wiped to let the new resolver rebuild correctly.
#   3: v0.4.9 tls-client bundling fix — between v0.4.6 and v0.4.8 the
#      packaged Mac/Windows builds couldn't load tls-client's native
#      dylib, so spotapi calls died at ctypes load and every result
#      cached as null. Wiping flushes those nulls so the now-working
#      transport gets re-asked instead of waiting out the 1-day
#      negative TTL on each row.
#   4: search-limit bump (5 -> 50) and artist-name-search fallback.
#      Pre-bump rows could carry undercount playcounts (canonical
#      version wasn't in the top-5 search results so the resolver
#      capped at a non-canonical version's count, e.g. Quadeca's
#      SCRAPYARD tracks coming back at ~30% of Spotify's actual
#      number) and null artist mappings (no primary-artist match
#      found within the top-5 sample ISRCs, e.g. Justin Bieber's
#      monthly listeners going blank because his top-N tracks on
#      Tidal are all feature credits). Wipe to force re-resolution
#      under the new code paths.
_CACHE_SCHEMA_VERSION = 4


def _db() -> sqlite3.Connection:
    """Open the cache DB. Tables are created on demand. A version
    sentinel in `cache_meta` lets us invalidate existing rows when
    a code change makes them unsafe to reuse — bump
    `_CACHE_SCHEMA_VERSION` and the affected tables get cleared on
    next open.
    """
    conn = sqlite3.connect(str(_db_path), timeout=5.0)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache_meta ("
        "  key TEXT PRIMARY KEY,"
        "  value TEXT"
        ")"
    )
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

    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key='schema_version'"
    ).fetchone()
    stored = int(row[0]) if row and str(row[0]).isdigit() else 0
    if stored < _CACHE_SCHEMA_VERSION:
        # Wipe the rows the resolver fix invalidated. The ID-mapping
        # tables and the playcount cache can all carry wrong values
        # from the pre-fix code; the artist_stats table is keyed by
        # Spotify artist id so its rows are still accurate, but they
        # may be referenced by a now-cleared (and re-resolving)
        # tidal_to_spotify_artist mapping — leaving them avoids a
        # round trip when the new resolver lands on the same id.
        conn.execute("DELETE FROM tidal_to_spotify_artist")
        conn.execute("DELETE FROM isrc_to_spotify_track")
        conn.execute("DELETE FROM track_playcount")
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta (key, value) VALUES "
            "('schema_version', ?)",
            (str(_CACHE_SCHEMA_VERSION),),
        )
        conn.commit()
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


def _resolve_artist_via_name_match(
    tidal_artist_name: str, sample_isrcs: list[str]
) -> Optional[str]:
    """Walk a list of sample ISRCs and return the Spotify artist id
    of the first track whose primary artist's name matches the Tidal
    artist's name. Falls back to a direct artist-name search on
    Spotify when the ISRC walk doesn't find a primary-artist match.

    Two-stage resolution:

    1. **ISRC walk (preferred).** For each of up to 15 sample ISRCs
       (was 5), find the Spotify track and check whether its primary
       artist's name matches `tidal_artist_name`. Returns the first
       match. This is the most accurate path — the ISRC ties us to
       a specific track Tidal has, so the artist we resolve through
       it is provably someone Tidal also lists. Bumped from 5 to 15
       because artists like Justin Bieber, who are heavy feature
       collaborators, can have their entire top-15-on-Tidal be
       feature credits where the host (Kid LAROI, Skrillex, etc.) is
       Spotify's primary artist.

    2. **Artist-name search fallback.** When the ISRC walk fails —
       common for artists whose entire top-N is feature credits —
       fall back to Spotify's `query_artists` endpoint with
       `tidal_artist_name`. We accept the first result whose
       displayed name equals our target (case-insensitive). Less
       precise than the ISRC walk because two artists can share a
       name (e.g. multiple "John Smith" entries on Spotify), but
       precise enough for major artists with unique names — and
       returning *something* is better than the wrong-person /
       no-stats UX the ISRC-only path produces.
    """
    wanted = (tidal_artist_name or "").strip().lower()
    if not wanted:
        return None
    for isrc in sample_isrcs[:15]:
        ic = (isrc or "").strip().upper()
        if not ic:
            continue
        for tid, _name in _search_track_candidates(f"isrc:{ic}"):
            try:
                payload = _song_info(tid)
            except Exception as exc:
                log.warning("spotify getTrack failed for %s: %s", tid, exc)
                continue
            union = (payload.get("data") or {}).get("trackUnion") or {}
            first_items = (
                (union.get("firstArtist") or {}).get("items")
                or (union.get("artists") or {}).get("items")
                or []
            )
            for item in first_items:
                uri = item.get("uri") or ""
                if not uri.startswith("spotify:artist:"):
                    continue
                name = str(
                    (item.get("profile") or {}).get("name")
                    or item.get("name")
                    or ""
                )
                if name.strip().lower() == wanted:
                    return uri.split(":")[-1]
    return _search_artist_by_name(tidal_artist_name)


def _search_artist_by_name(name: str) -> Optional[str]:
    """Spotify artist search keyed on display name. Returns the
    Spotify artist id of the first hit whose name matches `name`
    (case-insensitive, trimmed). Used as a fallback after the ISRC
    walk in `_resolve_artist_via_name_match` fails.

    Errors are logged and swallowed; callers treat None as "couldn't
    resolve".
    """
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    try:
        _, artist_mod = _ensure_client()
        res = artist_mod.query_artists(name, limit=10)
    except Exception as exc:
        log.warning("spotify artist search failed for %r: %s", name, exc)
        return None
    items = (
        (res.get("data") or {})
        .get("searchV2", {})
        .get("artists", {})
        .get("items")
        or []
    )
    for entry in items:
        item = (entry.get("item") or {}).get("data") or {}
        uri = item.get("uri") or ""
        if not uri.startswith("spotify:artist:"):
            continue
        item_name = str(
            (item.get("profile") or {}).get("name")
            or item.get("name")
            or ""
        )
        if item_name.strip().lower() == wanted:
            return uri.split(":")[-1]
    return None


def _read_cached_tidal_artist_mapping(
    tidal_artist_id: str,
) -> Optional[tuple[Optional[str], Optional[float]]]:
    """Return the cached `(spotify_artist_id, fetched_at)` for the
    Tidal artist, or `None` when no row exists. Splitting this out
    keeps `artist_stats_v2` and the legacy `_tidal_to_spotify_artist`
    aligned on cache shape."""
    with _db_lock:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT spotify_artist_id, fetched_at FROM tidal_to_spotify_artist "
                "WHERE tidal_artist_id=?",
                (str(tidal_artist_id),),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return None
    return row[0], row[1]


def _write_tidal_artist_mapping(
    tidal_artist_id: str, spotify_artist_id: Optional[str]
) -> None:
    _write_cache_rows(
        (
            "INSERT OR REPLACE INTO tidal_to_spotify_artist "
            "(tidal_artist_id, spotify_artist_id, fetched_at) VALUES (?, ?, ?)",
            (str(tidal_artist_id), spotify_artist_id, int(time.time())),
        ),
    )


def _fetch_artist_overview(
    spotify_artist_id: str,
) -> Optional[ArtistStats]:
    """Cached read of `queryArtistOverview`. Caches the JSON-serialized
    ArtistStats under the Spotify artist id for 7 days."""
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
    _write_cache_rows(
        (
            "INSERT OR REPLACE INTO artist_stats "
            "(spotify_artist_id, payload, fetched_at) VALUES (?, ?, ?)",
            (
                spotify_artist_id,
                json.dumps(result.to_dict()),
                int(time.time()),
            ),
        ),
    )
    return result


def artist_stats_v2(
    tidal_artist_id: str,
    tidal_artist_name: str,
    sample_isrcs: list[str],
) -> Optional[ArtistStats]:
    """Return Spotify's artist-level stats for a Tidal artist,
    resolved via a list of sample ISRCs and a strict name match.

    Walks the ISRCs in order, pivots through Spotify's getTrack to
    the primary artist, and only accepts the result when the primary
    artist's name matches `tidal_artist_name` (case-insensitive).
    Returns None when nothing matches — showing the wrong artist's
    monthly listeners is worse than showing none.

    The Tidal → Spotify artist mapping persists permanently once
    resolved; subsequent calls only hit `queryArtistOverview` (which
    is itself cached for 7 days under the Spotify artist id).
    """
    cached = _read_cached_tidal_artist_mapping(tidal_artist_id)
    if cached is not None:
        cached_id, fetched_at = cached
        if cached_id is not None:
            return _fetch_artist_overview(cached_id)
        # Negative cache hit on the short TTL — don't re-walk yet.
        if fetched_at and (time.time() - fetched_at) < _NULL_TTL_SEC:
            return None
        # Stale negative — fall through and retry.

    spotify_artist_id = _resolve_artist_via_name_match(
        tidal_artist_name, sample_isrcs
    )
    _write_tidal_artist_mapping(tidal_artist_id, spotify_artist_id)
    if not spotify_artist_id:
        return None
    return _fetch_artist_overview(spotify_artist_id)


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


def _search_track_candidates(query: str) -> list[tuple[str, str]]:
    """Run a Spotify GraphQL track search and return
    `[(track_id, display_name), ...]` for hits whose URI is a
    spotify:track:. Errors are logged and swallowed — callers
    treat an empty list as "search produced nothing useful".

    Limit is 50 (was 5). For tracks that exist in multiple Spotify
    entries (album version, single release, lyric video, alt master,
    pre-release) the canonical highest-playcount version is often
    not in the top 5 results — Spotify's search ranks by relevance
    against the query, not by playcount. The downstream
    `_resolve_canonical_track` filter scans the candidate list for
    title/artist matches and picks the highest playcount among those,
    so widening the candidate list directly raises ceiling on what
    we can find without making the lookup any less accurate. The
    pre-filter on `name.lower() == wanted_title` gates the expensive
    per-candidate `_song_info` calls so wider lookups don't blow up
    cost.
    """
    try:
        song, _ = _ensure_client()
        res = song.query_songs(query, limit=50)
    except Exception as exc:
        log.warning("spotify track search failed for %r: %s", query, exc)
        return []
    items = (
        (res.get("data") or {})
        .get("searchV2", {})
        .get("tracksV2", {})
        .get("items")
        or []
    )
    out: list[tuple[str, str]] = []
    for entry in items:
        item = (entry.get("item") or {}).get("data") or {}
        uri = item.get("uri") or ""
        if not uri.startswith("spotify:track:"):
            continue
        out.append((uri.split(":")[-1], str(item.get("name") or "")))
    return out


def _track_primary_artist_name(payload: Mapping[str, Any]) -> str:
    """Pull the primary-artist display name out of a getTrack payload.
    Handles both shapes the GraphQL response has shipped — the newer
    `firstArtist.items[0]` and the older `artists.items[0]`."""
    union = (payload.get("data") or {}).get("trackUnion") or {}
    first_items = (
        (union.get("firstArtist") or {}).get("items")
        or (union.get("artists") or {}).get("items")
        or []
    )
    if not first_items:
        return ""
    first = first_items[0]
    return str(
        (first.get("profile") or {}).get("name")
        or first.get("name")
        or ""
    )


def _resolve_canonical_track(
    isrc: str, title: str, artist: str
) -> tuple[Optional[int], Optional[str]]:
    """Find the highest-playcount Spotify track that has the right
    title and primary artist, considering BOTH an `isrc:` search and
    a `<artist> <title>` search.

    Tidal's metadata sometimes carries the ISRC of a less-played
    reissue, so an `isrc:` search alone caps us at that reissue's
    candidates and misses the canonical 1.6B-play original (which
    has its own ISRC that Spotify will never alias). The title
    search reaches across reissues. The strict primary-artist filter
    keeps us from picking a same-titled track by a different artist
    (Adele's "Hello" vs Lionel Richie's "Hello").

    Returns `(playcount, spotify_track_id)` or `(None, None)` when
    nothing matches.
    """
    wanted_title = (title or "").strip().lower()
    wanted_artist = (artist or "").strip().lower()
    if not wanted_title or not wanted_artist:
        return None, None

    # Insertion-ordered dedupe across both searches. The ISRC pass
    # often produces zero or one candidate; the title pass covers
    # reissues with unrelated ISRCs.
    candidates: dict[str, str] = {}
    for tid, name in _search_track_candidates(f"isrc:{isrc}"):
        candidates.setdefault(tid, name)
    for tid, name in _search_track_candidates(f"{artist} {title}"):
        candidates.setdefault(tid, name)

    best_pc = -1
    best_id: Optional[str] = None
    for tid, name in candidates.items():
        # Title check at the search-result level — getTrack doesn't
        # always echo the same display name verbatim, and we want
        # to skip "Thriller (Live)" / "Thriller - Remastered" before
        # paying for a getTrack round trip.
        if name.lower() != wanted_title:
            continue
        try:
            payload = _song_info(tid)
        except Exception as exc:
            log.warning("spotify getTrack failed for %s: %s", tid, exc)
            continue
        if _track_primary_artist_name(payload).strip().lower() != wanted_artist:
            continue
        union = (payload.get("data") or {}).get("trackUnion") or {}
        pc_raw = union.get("playcount")
        try:
            cand_pc = int(pc_raw) if pc_raw is not None else None
        except (TypeError, ValueError):
            cand_pc = None
        if cand_pc is not None and cand_pc > best_pc:
            best_pc = cand_pc
            best_id = tid

    if best_id is None or best_pc <= 0:
        return None, None
    return best_pc, best_id


def playcount_with_fallback(
    isrc: str, title: str, artist: str
) -> Optional[int]:
    """Return the canonical playcount for the Tidal track identified
    by `(isrc, title, artist)`. Searches both the supplied ISRC and
    `<artist> <title>` so a reissue's lower-trafficked ISRC doesn't
    cap us below the canonical 1.6B-play number. Result is cached
    under the original ISRC for 7 days, so the doubled search cost
    is paid once per track.

    Falls back to ISRC-only when title or artist is missing — in
    that mode the function behaves exactly like `playcount_by_isrc`.
    """
    isrc = (isrc or "").strip().upper()
    if not isrc:
        return None

    # Cache fast path. Same zero-aware semantics as playcount_by_isrc:
    # a fresh non-zero playcount is honored for the full 7-day TTL,
    # but zeroes get retried on the short 1-day negative TTL because
    # that's the release-week-aggregation lull.
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
        is_zeroish = pc_cached is None or int(pc_cached or 0) == 0
        if not is_zeroish and age < _STATS_TTL_SEC:
            return int(pc_cached)
        if is_zeroish and age < _NULL_TTL_SEC:
            return int(pc_cached) if pc_cached is not None else None

    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title or not artist:
        # No metadata = no canonical search; fall back to ISRC-only.
        return playcount_by_isrc(isrc)

    pc, track_id = _resolve_canonical_track(isrc, title, artist)
    now_s = int(time.time())
    if pc is not None and track_id:
        # Cache the canonical playcount AND map the original ISRC to
        # the canonical track id so downstream artist-resolution can
        # reuse the mapping rather than re-walking the searches.
        _write_cache_rows(
            (
                "INSERT OR REPLACE INTO track_playcount "
                "(isrc, playcount, fetched_at) VALUES (?, ?, ?)",
                (isrc, pc, now_s),
            ),
            (
                "INSERT OR REPLACE INTO isrc_to_spotify_track "
                "(isrc, spotify_track_id, fetched_at) VALUES (?, ?, ?)",
                (isrc, track_id, now_s),
            ),
        )
        return pc

    # Nothing matched. Cache as null on the negative TTL so we don't
    # refire on every pageview but DO retry tomorrow (cf. the
    # release-week zero behavior).
    _write_track_playcount(isrc, None)
    return None


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
    "artist_stats_v2",
    "debug_resolve_artist",
]
