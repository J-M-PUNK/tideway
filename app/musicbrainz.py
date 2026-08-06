"""Minimal MusicBrainz client for relationship-based discovery (#307).

MusicBrainz is *keyless* — its Web Service needs only a descriptive
User-Agent and roughly one request per second (rate-limited per IP; since
Tideway is a desktop app, every user gets their own budget, so there's no
central bottleneck). Read-only and cached in-process because artist
relationships barely move.

This powers the "More From These People" row: band members, aliases (the
real person behind a stage name — e.g. "venturing" is Jane Remover), and
collaborators of the listener's top artists. That's a *structural* signal
Tidal's / Last.fm's behavioural similarity can't produce. Coverage is
patchy for brand-new underground artists (MB simply has no data yet), so
the caller treats an empty result as "nothing to add" and drops the row.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Optional

import requests

_BASE = "https://musicbrainz.org/ws/2"
# MB requires a real, contactful User-Agent; requests without one get
# blocked. Not a secret / not per-user — one app-level string.
_USER_AGENT = "Tideway/1.x (https://github.com/J-M-PUNK/tideway)"
# MB asks for ~1 req/sec; a hair over keeps us clear of the limiter.
_MIN_INTERVAL_S = 1.1
_TIMEOUT_S = 15

# Relationship types worth surfacing, mapped to a short human reason.
# Anything else still counts as a generic "related to".
_RELATION_LABEL = {
    "member of band": "bandmate of",
    "is person": "the artist behind",
    "collaboration": "collaborator of",
    "supporting musician": "plays with",
    "founder": "founder alongside",
}

_rate_lock = threading.Lock()
_last_call = [0.0]

_cache_lock = threading.Lock()
_cache: dict = {}  # mbid -> (ts, list[dict])
_CACHE_TTL_S = 7 * 24 * 3600.0  # a week

_search_cache_lock = threading.Lock()
_search_cache: dict = {}  # name(lower) -> (ts, Optional[mbid])


def _throttled_get(path: str, params: dict) -> Optional[dict]:
    with _rate_lock:
        wait = _MIN_INTERVAL_S - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
    try:
        resp = requests.get(
            f"{_BASE}{path}",
            params={**params, "fmt": "json"},
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT_S,
        )
        if not resp.ok:
            return None
        return resp.json()
    except Exception as exc:
        print(f"[musicbrainz] request failed: {exc!r}", file=sys.stderr, flush=True)
        return None


def resolve_mbid(name: str) -> Optional[str]:
    """Best-effort MBID for an artist name — the fallback for when Last.fm
    didn't hand us one. Cached; returns None on no confident match."""
    name = (name or "").strip()
    if not name:
        return None
    key = name.lower()
    now = time.monotonic()
    with _search_cache_lock:
        c = _search_cache.get(key)
        if c and now - c[0] < _CACHE_TTL_S:
            return c[1]
    data = _throttled_get("/artist", {"query": f'artist:"{name}"', "limit": "1"})
    mbid = None
    arts = (data or {}).get("artists") or []
    if arts:
        top = arts[0]
        # MB's score is 0..100; only trust a strong, near-exact match so we
        # don't attach a wrong artist's whole discography.
        if int(top.get("score") or 0) >= 90 and (
            (top.get("name") or "").strip().lower() == key
        ):
            mbid = top.get("id")
    with _search_cache_lock:
        _search_cache[key] = (time.monotonic(), mbid)
    return mbid


def related_artists(mbid: str, seed_name: str) -> list[dict]:
    """`[{"name", "reason"}]` for artists related to `mbid` — members,
    the person behind an alias, collaborators. Empty when MB has nothing
    (the common case for very new artists), so the section can drop out."""
    mbid = (mbid or "").strip()
    if not mbid:
        return []
    now = time.monotonic()
    with _cache_lock:
        c = _cache.get(mbid)
        if c and now - c[0] < _CACHE_TTL_S:
            return c[1]
    data = _throttled_get(f"/artist/{mbid}", {"inc": "artist-rels"})
    out: list[dict] = []
    seen: set = set()
    for rel in (data or {}).get("relations", []) or []:
        rtype = (rel.get("type") or "").lower()
        # Whitelist musical relationships only (substring match so variants
        # like "instrumental supporting musician" still count). MB also
        # carries personal edges (dated, married, sibling) — surfacing an
        # artist's ex's albums as a "recommendation" is the noise to avoid.
        label = next(
            (lbl for key, lbl in _RELATION_LABEL.items() if key in rtype), None
        )
        if not label:
            continue
        art = rel.get("artist") or {}
        name = (art.get("name") or "").strip()
        if not name:
            continue
        low = name.lower()
        if low == (seed_name or "").strip().lower() or low in seen:
            continue
        seen.add(low)
        out.append({"name": name, "reason": f"{label.capitalize()} {seed_name}"})
    with _cache_lock:
        _cache[mbid] = (time.monotonic(), out)
    return out
