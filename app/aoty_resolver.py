"""AOTY listing → Tidal-resolved listing.

Takes raw `AotyAlbum` dicts from `app.aoty` and decorates each
with a `tidal_album` field (or None when Tidal doesn't have the
album). The resolution itself is a Tidal search per entry,
parallelised across a small thread pool, with results cached
per-album in a long-TTL persistent cache so chart-cache misses
only re-resolve genuinely new entries.

Same shape as the per-track resolver used by the Last.fm Popular
page — see `lastfm_chart_top_tracks_resolved` in server.py for
the prior art and the rate-limit rationale (3 workers + jittered
sleeps; 50 concurrent searches in ~7 s trips Tidal's abuse
detection over time).

Lives outside server.py so the AOTY feature can ship without
waiting for the larger server.py-into-routers split (PR #49).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)


# Tidal album ids for popular albums are very stable. 30 days
# mirrors the per-track cache used by the Last.fm Popular page so
# both integrations age uniformly.
_RESOLVE_TTL_SEC = 86400.0 * 30


def resolve_listing(listing: list[dict]) -> list[dict]:
    """Return a new list with each entry decorated with `tidal_album`.

    `listing` is the output of `app.aoty.top_albums_of_year` /
    `app.aoty.recent_releases` — a list of dicts with `artist` +
    `title` keys (and other AOTY metadata).

    The returned list is the same shape with one extra key per
    entry: `tidal_album` is a Tidal album dict (per `album_to_dict`)
    on success, or None when Tidal doesn't have a match or the
    search failed.
    """
    if not listing:
        return []

    # Lazy imports to avoid a circular dependency on server module
    # state — server.py defines `tidal`, `settings`,
    # `tidal_jitter_sleep`, `album_to_dict`, `filter_explicit_dupes`
    # at module-level, so by the time resolve_listing runs (in a
    # request handler) the server module is fully populated.
    from server import (
        album_to_dict,
        filter_explicit_dupes,
        settings,
        tidal,
        tidal_jitter_sleep,
    )
    from app import lastfm_disk_cache

    pref = (settings.explicit_content_preference or "explicit").lower()

    def _resolve_one(entry: dict) -> dict:
        artist = (entry.get("artist") or "").strip()
        title = (entry.get("title") or "").strip()
        if not artist or not title:
            return {**entry, "tidal_album": None}

        cache_key = (
            f"aoty:resolve-album:{pref}:"
            f"{artist.lower()}:{title.lower()}"
        )
        cached = lastfm_disk_cache.get(cache_key, _RESOLVE_TTL_SEC)
        if cached is not None:
            return {**entry, "tidal_album": cached}

        tidal_jitter_sleep()
        try:
            results = tidal.search(f"{artist} {title}", limit=5)
        except Exception:
            return {**entry, "tidal_album": None}
        albums = filter_explicit_dupes(
            results.get("albums", []), pref, kind="album"
        )
        if not albums:
            return {**entry, "tidal_album": None}

        # Exact title + artist first; fall back to Tidal's top hit.
        wt = title.lower()
        wa = artist.lower()
        exact = next(
            (
                a for a in albums
                if getattr(a, "name", "").lower() == wt
                and any(
                    getattr(ar, "name", "").lower() == wa
                    for ar in (getattr(a, "artists", None) or [])
                )
            ),
            None,
        )
        try:
            resolved = album_to_dict(exact or albums[0])
        except Exception:
            return {**entry, "tidal_album": None}

        # Only persist successes — caching None for 30 days would
        # blank an album from the chart on a single transient
        # Tidal hiccup.
        try:
            lastfm_disk_cache.set(cache_key, resolved)
        except Exception:
            pass
        return {**entry, "tidal_album": resolved}

    # 3 workers matches the rate-limit posture used by the
    # Popular page resolver. After the first run the per-album
    # cache is hot, so this only fires fresh searches for entries
    # that changed.
    with ThreadPoolExecutor(max_workers=3) as pool:
        return list(pool.map(_resolve_one, listing))
