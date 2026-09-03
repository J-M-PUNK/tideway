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

# Bumped when the matching rules change, so entries resolved under the
# old rules are not served from a 30-day cache. v2 stopped falling back
# to Tidal's top search hit; without a bump, every album already
# mis-resolved under v1 would stay wrong for a month.
_RESOLVE_CACHE_VERSION = "v2"

# How close a Tidal album title has to be to AOTY's before we believe
# it is the same record. Titles differ in punctuation, capitalisation,
# bracketed edition suffixes ("(Deluxe)"), so an exact compare is too
# strict — but the failure this replaces was accepting anything at all,
# so the bar is set high.
_TITLE_MATCH_MIN = 88.0
_ARTIST_MATCH_MIN = 80.0


def _norm(text: str) -> str:
    """Lowercase, strip a trailing bracketed edition tag and collapse
    whitespace. "Big Fish Theory (Deluxe Edition)" and "big fish
    theory" should compare equal."""
    import re

    t = (text or "").strip().lower()
    t = re.sub(r"[\(\[][^)\]]*[)\]]\s*$", "", t).strip()
    t = re.sub(r"\s+", " ", t)
    return t


def _similar(a: str, b: str) -> float:
    """0-100 similarity. rapidfuzz is already a dependency (it backs
    the local-library search), so this costs nothing new."""
    from rapidfuzz import fuzz

    return float(fuzz.ratio(_norm(a), _norm(b)))


def _is_plausible_match(album, want_artist: str, want_title: str) -> bool:
    """Whether a Tidal search hit is actually the album AOTY listed.

    Tidal's search returns *something* for almost any query. Asking it
    for "Denzel Curry & Kenneth Blume ii" returned Vince Staples' "Big
    Fish Theory" — a different artist, a different title, and nine
    years older — which then sat at the top of the Top Albums of the
    Year row because the resolver took the first hit unconditionally.

    Require the title to match closely. The artist is checked too, but
    only as a veto when we can read one: AOTY credits collaborations in
    ways Tidal does not ("Denzel Curry & Kenneth Blume" vs two artist
    entries), so a title-only match with an unreadable artist is
    allowed through, while a confident artist mismatch is not.
    """
    name = getattr(album, "name", "") or ""
    if _similar(name, want_title) < _TITLE_MATCH_MIN:
        return False
    names = [
        getattr(ar, "name", "") or ""
        for ar in (getattr(album, "artists", None) or [])
    ]
    names = [n for n in names if n]
    if not names:
        return True
    want = _norm(want_artist)
    return any(
        _similar(n, want_artist) >= _ARTIST_MATCH_MIN
        or _norm(n) in want
        or want in _norm(n)
        for n in names
    )


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
            f"aoty:resolve-album:{_RESOLVE_CACHE_VERSION}:{pref}:"
            f"{artist.lower()}:{title.lower()}"
        )
        cached = lastfm_disk_cache.get(cache_key, _RESOLVE_TTL_SEC)
        if cached is not None:
            return {**entry, "tidal_album": cached}

        tidal_jitter_sleep()
        try:
            results = tidal.search(f"{artist} {title}", limit=5)
        except Exception as exc:
            log.warning("aoty resolve search %r/%r failed: %s", artist, title, exc)
            return {**entry, "tidal_album": None}
        albums = filter_explicit_dupes(
            results.get("albums", []), pref, kind="album"
        )
        if not albums:
            return {**entry, "tidal_album": None}

        # Exact title + artist first, then the best plausible hit.
        # Never Tidal's top hit unconditionally: search returns
        # something for almost any query, and taking it put Vince
        # Staples' "Big Fish Theory" (2017) at the top of Top Albums of
        # the Year, standing in for an album by Denzel Curry.
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
        if exact is None:
            exact = next(
                (a for a in albums if _is_plausible_match(a, artist, title)),
                None,
            )
        if exact is None:
            # Nothing on Tidal we can stand behind. A row one album
            # shorter is better than a row with the wrong album in it,
            # and callers already drop entries whose tidal_album is
            # None. Not cached — see below.
            log.info(
                "aoty resolve: no plausible Tidal match for %r / %r",
                artist,
                title,
            )
            return {**entry, "tidal_album": None}
        try:
            resolved = album_to_dict(exact)
        except Exception as exc:
            log.warning("aoty resolve serialize %r/%r failed: %s", artist, title, exc)
            return {**entry, "tidal_album": None}

        # Only persist successes — caching None for 30 days would
        # blank an album from the chart on a single transient
        # Tidal hiccup.
        try:
            lastfm_disk_cache.set(cache_key, resolved)
        except Exception as exc:
            log.debug("aoty resolve cache write failed (%s); continuing", exc)
        return {**entry, "tidal_album": resolved}

    # 3 workers matches the rate-limit posture used by the
    # Popular page resolver. After the first run the per-album
    # cache is hot, so this only fires fresh searches for entries
    # that changed.
    with ThreadPoolExecutor(max_workers=3) as pool:
        return list(pool.map(_resolve_one, listing))
