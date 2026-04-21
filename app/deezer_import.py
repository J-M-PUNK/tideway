"""Deezer playlist import (public API — no OAuth).

Deezer's OAuth requires a client_secret (no PKCE support), which
would force users to paste two credentials just to get started. The
public API route is a much lighter UX: the user pastes a playlist
URL, we extract the id, and fetch tracks via
`GET https://api.deezer.com/playlist/{id}/tracks` — no auth, no
setup, no registered developer app.

Trade-off: only *public* Deezer playlists work this way. Private
playlists would need OAuth. Deezer makes it trivial to toggle a
playlist public → import → back to private, so this covers the
vast majority of use cases.
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlparse

import requests

from app import spotify_import

log = logging.getLogger(__name__)

API_BASE = "https://api.deezer.com"


def parse_playlist_id(source: str) -> Optional[str]:
    """Accept either a bare numeric id or one of the URL shapes
    Deezer hands out (deezer.com/us/playlist/12345, deezer.page.link/..., etc.).
    Returns the numeric id as a string, or None if we can't find
    one."""
    if not source:
        return None
    s = source.strip()
    # Bare numeric.
    if s.isdigit():
        return s
    # URL — pick the last numeric segment. Deezer playlist URLs are
    # shaped like .../playlist/12345 or .../playlist/12345/tracks.
    try:
        parsed = urlparse(s)
    except Exception:
        return None
    path = parsed.path or s
    # Walk path components in reverse — the playlist id is always
    # adjacent to the "playlist" keyword. This also tolerates extra
    # paths that some short-link redirects append.
    parts = [p for p in path.split("/") if p]
    for i, p in enumerate(parts):
        if p == "playlist" and i + 1 < len(parts):
            candidate = parts[i + 1]
            if candidate.isdigit():
                return candidate
    # Fallback: last numeric segment anywhere in the path.
    for p in reversed(parts):
        if p.isdigit():
            return p
    # Last-ditch: pull any long-ish digit run out of the raw input.
    m = re.search(r"(\d{6,})", s)
    return m.group(1) if m else None


def fetch_playlist(playlist_id: str) -> dict:
    """Fetch playlist metadata + tracks in one shot. Deezer's
    /playlist/{id} endpoint embeds the track list on the parent
    response (unlike Spotify, which requires a separate tracks call).
    Shape we return matches the shape Spotify's flow produces so the
    same matcher + review UI handles both.
    """
    resp = requests.get(f"{API_BASE}/playlist/{playlist_id}", timeout=15)
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, dict) and body.get("error"):
        # Deezer's API returns 200 with {"error": {...}} for not-found
        # etc. Promote to an exception the endpoint surfaces as 502.
        err = body["error"]
        raise RuntimeError(
            f"Deezer error {err.get('code')}: {err.get('message') or err}"
        )
    tracks_raw = ((body.get("tracks") or {}).get("data")) or []

    # For very long playlists Deezer paginates the embedded tracks —
    # chase `tracks.next` links until exhausted.
    next_url = (body.get("tracks") or {}).get("next")
    while next_url:
        try:
            page = requests.get(next_url, timeout=15).json()
        except Exception:
            break
        tracks_raw.extend(page.get("data") or [])
        next_url = page.get("next")

    tracks: list[dict] = []
    for t in tracks_raw:
        if not isinstance(t, dict):
            continue
        artist = t.get("artist") or {}
        tracks.append(
            {
                "name": t.get("title") or "",
                "artists": [artist.get("name", "")] if artist.get("name") else [],
                "duration_ms": int(t.get("duration") or 0) * 1000,
                # Deezer's tracks carry an ISRC in the full /track/{id}
                # endpoint but not on the embedded list. Leave None;
                # matcher falls back to fuzzy search which is fine for
                # the common case.
                "isrc": None,
            }
        )

    return {
        "name": body.get("title") or "",
        "description": body.get("description") or "",
        "cover": body.get("picture_medium") or body.get("picture") or None,
        "track_count": body.get("nb_tracks") or len(tracks),
        "tracks": tracks,
    }


def match_each(session, tracks: list[dict]) -> list[dict]:
    """Match Deezer rows against Tidal. Same shape as the Spotify /
    M3U flows so the frontend's MatchReview handles all three."""
    out: list[dict] = []
    for t in tracks:
        match = spotify_import.match_track(session, t)
        out.append(
            {
                "spotify": {
                    "name": t.get("name"),
                    "artists": t.get("artists") or [],
                    "duration_ms": t.get("duration_ms") or 0,
                    "isrc": None,
                },
                "match": match,
            }
        )
    return out
