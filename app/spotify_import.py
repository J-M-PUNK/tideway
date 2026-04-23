"""Spotify → Tidal playlist import.

OAuth (PKCE) flow against Spotify + fuzzy matching to Tidal. Matching
is best-effort: exact ISRC matches when both sides expose one, then
fuzzy (title + primary artist + duration tolerance) for the rest.

No client_secret is shipped — PKCE auth is designed for public
clients. The user registers their own Spotify app (free, one-time)
and pastes the client_id into Settings. Same friction pattern as our
Last.fm integration.

Token storage: spotify_session.json in the app's user_data_dir,
chmod 0600. Refresh tokens are long-lived; we refresh the access
token on expiry before every API call.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import logging
import os
import secrets
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode

import requests

from app.paths import user_data_dir

log = logging.getLogger(__name__)

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"

# Scopes we request on OAuth:
#  playlist-read-private — user's private + collaborative playlists
#  user-library-read     — saved tracks (Liked Songs) + saved albums
#  user-follow-read      — followed artists
# All read-only. Nothing we fetch here mutates the user's Spotify account.
SCOPES = "playlist-read-private user-library-read user-follow-read"

SESSION_FILE = user_data_dir() / "spotify_session.json"


@dataclass
class SpotifyAuth:
    access_token: str
    refresh_token: str
    expires_at: float  # unix seconds
    client_id: str


# ---------------------------------------------------------------------------
# OAuth / session persistence
# ---------------------------------------------------------------------------

_session_lock = threading.Lock()
# In-flight PKCE verifiers keyed by state token. Cleared after the
# callback exchanges the code. 10-minute TTL cap just in case the
# user abandons the flow mid-auth.
_pending_auth: dict[str, tuple[float, str, str]] = {}  # state -> (expires_at, verifier, client_id)


def _prune_pending() -> None:
    now = time.time()
    for k in list(_pending_auth.keys()):
        if _pending_auth[k][0] < now:
            _pending_auth.pop(k, None)


def _pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(client_id: str, redirect_uri: str) -> tuple[str, str]:
    """Kick off the PKCE auth dance. Returns (auth_url, state).
    Caller opens the URL in a browser; Spotify redirects to
    redirect_uri?code=...&state=... which our callback handler picks
    up and exchanges via `exchange_code()`."""
    _prune_pending()
    state = secrets.token_urlsafe(16)
    verifier, challenge = _pkce_pair()
    _pending_auth[state] = (time.time() + 600, verifier, client_id)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    return f"{SPOTIFY_AUTH_URL}?{urlencode(params)}", state


def exchange_code(
    code: str, state: str, redirect_uri: str
) -> Optional[SpotifyAuth]:
    """Exchange an authorization code for an access + refresh token
    using the PKCE verifier we stashed during build_auth_url()."""
    _prune_pending()
    entry = _pending_auth.pop(state, None)
    if entry is None:
        log.warning("spotify callback: unknown state %r", state)
        return None
    _expires_at, verifier, client_id = entry
    resp = requests.post(
        SPOTIFY_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        timeout=10,
    )
    if resp.status_code != 200:
        log.warning(
            "spotify token exchange failed: %s %s", resp.status_code, resp.text[:200]
        )
        return None
    data = resp.json()
    auth = SpotifyAuth(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at=time.time() + int(data.get("expires_in", 3600)) - 30,
        client_id=client_id,
    )
    save_session(auth)
    return auth


def refresh(auth: SpotifyAuth) -> Optional[SpotifyAuth]:
    resp = requests.post(
        SPOTIFY_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": auth.refresh_token,
            "client_id": auth.client_id,
        },
        timeout=10,
    )
    if resp.status_code != 200:
        log.warning("spotify refresh failed: %s %s", resp.status_code, resp.text[:200])
        return None
    data = resp.json()
    new_auth = SpotifyAuth(
        access_token=data["access_token"],
        # Spotify rotates refresh tokens on some responses; reuse the
        # old one when the new one is absent.
        refresh_token=data.get("refresh_token") or auth.refresh_token,
        expires_at=time.time() + int(data.get("expires_in", 3600)) - 30,
        client_id=auth.client_id,
    )
    save_session(new_auth)
    return new_auth


def load_session() -> Optional[SpotifyAuth]:
    with _session_lock:
        if not SESSION_FILE.exists():
            return None
        try:
            data = json.loads(SESSION_FILE.read_text())
            return SpotifyAuth(**data)
        except Exception:
            log.warning("spotify_session.json unreadable; ignoring")
            return None


def save_session(auth: SpotifyAuth) -> None:
    with _session_lock:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SESSION_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(auth)))
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        tmp.replace(SESSION_FILE)


def clear_session() -> None:
    with _session_lock:
        try:
            if SESSION_FILE.exists():
                SESSION_FILE.unlink()
        except Exception:
            pass


def _ensure_fresh(auth: SpotifyAuth) -> Optional[SpotifyAuth]:
    if time.time() < auth.expires_at:
        return auth
    return refresh(auth)


# ---------------------------------------------------------------------------
# Spotify API helpers
# ---------------------------------------------------------------------------


def _get(auth: SpotifyAuth, path: str, params: Optional[dict] = None) -> dict:
    fresh = _ensure_fresh(auth)
    if fresh is None:
        raise RuntimeError("Spotify session expired and refresh failed")
    resp = requests.get(
        f"{SPOTIFY_API}{path}",
        headers={"Authorization": f"Bearer {fresh.access_token}"},
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def current_user(auth: SpotifyAuth) -> dict:
    return _get(auth, "/me")


def list_playlists(auth: SpotifyAuth) -> list[dict]:
    """Paginate through the user's playlists and return minimal rows
    shaped for our UI: {id, name, tracks, image, owner}."""
    out: list[dict] = []
    url = "/me/playlists?limit=50"
    while url:
        data = _get(auth, url)
        for p in data.get("items") or []:
            if not isinstance(p, dict):
                continue
            images = p.get("images") or []
            cover = images[0]["url"] if images else None
            out.append({
                "id": p.get("id"),
                "name": p.get("name") or "(untitled)",
                "tracks": (p.get("tracks") or {}).get("total") or 0,
                "image": cover,
                "owner": (p.get("owner") or {}).get("display_name") or "",
                "description": p.get("description") or "",
            })
        next_url = data.get("next")
        if not next_url:
            break
        # Spotify's `next` is a full URL; trim to path+query since
        # _get prefixes the base.
        url = next_url.replace(SPOTIFY_API, "")
    return out


def list_playlist_tracks(auth: SpotifyAuth, playlist_id: str) -> list[dict]:
    """Fetch every track in a Spotify playlist. Returns normalized
    rows for our matcher — title, primary_artist, all_artists,
    duration_ms, isrc (when present)."""
    out: list[dict] = []
    url = (
        f"/playlists/{playlist_id}/tracks"
        "?limit=100&fields=items(track(name,artists(name),duration_ms,external_ids,id)),next"
    )
    while url:
        data = _get(auth, url)
        for item in data.get("items") or []:
            track = (item or {}).get("track") or {}
            if not track:
                continue
            artists = [a.get("name", "") for a in (track.get("artists") or [])]
            out.append({
                "id": track.get("id"),
                "name": track.get("name") or "",
                "artists": [a for a in artists if a],
                "duration_ms": track.get("duration_ms") or 0,
                "isrc": (track.get("external_ids") or {}).get("isrc") or None,
            })
        next_url = data.get("next")
        url = next_url.replace(SPOTIFY_API, "") if next_url else None
    return out


# ---------------------------------------------------------------------------
# Track matching against Tidal
# ---------------------------------------------------------------------------


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _similarity(a: str, b: str) -> float:
    """difflib SequenceMatcher — 0..1, roughly fuzzy-match ratio."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def match_track(session, spotify_track: dict) -> Optional[dict]:
    """Find the best Tidal track for a Spotify row. Strategy:

    1. ISRC lookup via Tidal's /v1/tracks/isrc/{isrc} — if it hits, we
       trust the result (ISRC uniquely identifies a recording).
    2. Fuzzy search: query `"{title} {primary_artist}"`, score each
       candidate by name + primary-artist + duration. Best score above
       a threshold wins.

    Returns {tidal_id, name, artists, duration, cover, confidence,
    reason} or None when nothing matched above threshold.
    """
    title = spotify_track.get("name") or ""
    artists: list[str] = spotify_track.get("artists") or []
    primary = artists[0] if artists else ""
    isrc = spotify_track.get("isrc")
    duration_ms = spotify_track.get("duration_ms") or 0

    # ISRC fast path.
    if isrc:
        try:
            resp = session.request.request("GET", f"tracks/isrc/{isrc}")
            if resp.status_code < 400:
                body = resp.json()
                items = (
                    body.get("items")
                    if isinstance(body, dict)
                    else body if isinstance(body, list) else []
                )
                if items:
                    t = items[0]
                    return _shape_match(t, confidence=1.0, reason="isrc")
        except Exception:
            pass

    # Fuzzy search fallback.
    query = f"{title} {primary}".strip()
    if not query:
        return None
    try:
        resp = session.request.request(
            "GET",
            "search/tracks",
            params={"query": query, "limit": 5, "countryCode": session.country_code},
        )
        if resp.status_code >= 400:
            return None
        body = resp.json()
    except Exception:
        return None
    candidates = body.get("items") or []
    best: Optional[tuple[float, dict]] = None
    for c in candidates:
        c_title = c.get("title") or ""
        c_artists = c.get("artists") or []
        c_primary = (c_artists[0].get("name") if c_artists else "") or ""
        c_duration = (c.get("duration") or 0) * 1000  # sec → ms
        title_score = _similarity(title, c_title)
        artist_score = _similarity(primary, c_primary)
        # Duration tolerance: 3-second diff is fine (remaster vs.
        # original), 10+ seconds is likely a different cut. Score
        # degrades linearly from 3s (1.0) to 15s (0.0).
        if duration_ms > 0 and c_duration > 0:
            delta = abs(c_duration - duration_ms)
            if delta <= 3000:
                duration_score = 1.0
            elif delta >= 15000:
                duration_score = 0.0
            else:
                duration_score = 1.0 - (delta - 3000) / 12000
        else:
            duration_score = 0.5  # neutral when we can't check
        score = 0.5 * title_score + 0.35 * artist_score + 0.15 * duration_score
        if best is None or score > best[0]:
            best = (score, c)
    if best is None:
        return None
    score, c = best
    if score < 0.55:
        return None
    return _shape_match(c, confidence=score, reason="fuzzy")


def _shape_match(t: dict, *, confidence: float, reason: str) -> dict:
    artists = [a.get("name", "") for a in (t.get("artists") or [])]
    album = t.get("album") or {}
    cover_uuid = album.get("cover")
    cover = (
        f"https://resources.tidal.com/images/{cover_uuid.replace('-', '/')}/320x320.jpg"
        if isinstance(cover_uuid, str) and cover_uuid
        else None
    )
    return {
        "tidal_id": str(t.get("id")),
        "name": t.get("title") or "",
        "artists": [a for a in artists if a],
        "duration": t.get("duration") or 0,
        "cover": cover,
        "confidence": round(float(confidence), 3),
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Library listers (Liked Songs / Saved Albums / Followed Artists)
# ---------------------------------------------------------------------------


def list_liked_tracks(auth: SpotifyAuth) -> list[dict]:
    """Paginate /me/tracks — the user's Liked Songs — and return
    match-shaped rows."""
    out: list[dict] = []
    url = "/me/tracks?limit=50"
    while url:
        data = _get(auth, url)
        for item in data.get("items") or []:
            track = (item or {}).get("track") or {}
            if not track:
                continue
            artists = [a.get("name", "") for a in (track.get("artists") or [])]
            out.append(
                {
                    "id": track.get("id"),
                    "name": track.get("name") or "",
                    "artists": [a for a in artists if a],
                    "duration_ms": track.get("duration_ms") or 0,
                    "isrc": (track.get("external_ids") or {}).get("isrc") or None,
                }
            )
        next_url = data.get("next")
        url = next_url.replace(SPOTIFY_API, "") if next_url else None
    return out


def list_saved_albums(auth: SpotifyAuth) -> list[dict]:
    """Paginate /me/albums. Rows are normalized to what our album
    matcher expects (name, primary_artist, upc, total_tracks)."""
    out: list[dict] = []
    url = "/me/albums?limit=50"
    while url:
        data = _get(auth, url)
        for item in data.get("items") or []:
            album = (item or {}).get("album") or {}
            if not album:
                continue
            artists = [a.get("name", "") for a in (album.get("artists") or [])]
            out.append(
                {
                    "id": album.get("id"),
                    "name": album.get("name") or "",
                    "artists": [a for a in artists if a],
                    "total_tracks": album.get("total_tracks") or 0,
                    "upc": (album.get("external_ids") or {}).get("upc") or None,
                    "image": (
                        (album.get("images") or [{}])[0].get("url")
                        if album.get("images")
                        else None
                    ),
                }
            )
        next_url = data.get("next")
        url = next_url.replace(SPOTIFY_API, "") if next_url else None
    return out


def list_followed_artists(auth: SpotifyAuth) -> list[dict]:
    """Paginate /me/following?type=artist. Cursor-based pagination
    (unlike everything else in Spotify's API)."""
    out: list[dict] = []
    url = "/me/following?type=artist&limit=50"
    while url:
        data = _get(auth, url)
        block = data.get("artists") or {}
        for a in block.get("items") or []:
            if not isinstance(a, dict):
                continue
            out.append(
                {
                    "id": a.get("id"),
                    "name": a.get("name") or "",
                    "followers": (a.get("followers") or {}).get("total") or 0,
                    "genres": a.get("genres") or [],
                    "image": (
                        (a.get("images") or [{}])[0].get("url")
                        if a.get("images")
                        else None
                    ),
                }
            )
        next_url = block.get("next")
        url = next_url.replace(SPOTIFY_API, "") if next_url else None
    return out


# ---------------------------------------------------------------------------
# Album + artist matchers
# ---------------------------------------------------------------------------


def match_album(session, spotify_album: dict) -> Optional[dict]:
    """UPC fast path → fuzzy title + primary artist + track-count
    tiebreaker. UPC identifies a specific release (same ISRC concept
    but at the album level)."""
    name = spotify_album.get("name") or ""
    artists = spotify_album.get("artists") or []
    primary = artists[0] if artists else ""
    upc = spotify_album.get("upc")
    total_tracks = spotify_album.get("total_tracks") or 0

    if upc:
        try:
            resp = session.request.request("GET", f"albums/byBarcodeId", params={"barcodeId": upc})
            if resp.status_code < 400:
                body = resp.json()
                items = body.get("items") if isinstance(body, dict) else body
                if items:
                    a = items[0]
                    return _shape_album_match(a, confidence=1.0, reason="upc")
        except Exception:
            pass

    query = f"{name} {primary}".strip()
    if not query:
        return None
    try:
        resp = session.request.request(
            "GET",
            "search/albums",
            params={"query": query, "limit": 5, "countryCode": session.country_code},
        )
        if resp.status_code >= 400:
            return None
        body = resp.json()
    except Exception:
        return None
    candidates = body.get("items") or []
    best: Optional[tuple[float, dict]] = None
    for c in candidates:
        c_name = c.get("title") or ""
        c_artists = c.get("artists") or []
        c_primary = (c_artists[0].get("name") if c_artists else "") or ""
        c_tracks = c.get("numberOfTracks") or 0
        name_score = _similarity(name, c_name)
        artist_score = _similarity(primary, c_primary)
        # Track-count tiebreaker — same album usually has the same
        # count; deluxe vs. standard editions differ. Lightly weighted.
        if total_tracks > 0 and c_tracks > 0:
            diff = abs(c_tracks - total_tracks)
            count_score = 1.0 if diff == 0 else max(0.0, 1.0 - diff / 10.0)
        else:
            count_score = 0.5
        score = 0.5 * name_score + 0.4 * artist_score + 0.1 * count_score
        if best is None or score > best[0]:
            best = (score, c)
    if best is None:
        return None
    score, c = best
    if score < 0.55:
        return None
    return _shape_album_match(c, confidence=score, reason="fuzzy")


def _shape_album_match(a: dict, *, confidence: float, reason: str) -> dict:
    artists = [ar.get("name", "") for ar in (a.get("artists") or [])]
    cover_uuid = a.get("cover")
    cover = (
        f"https://resources.tidal.com/images/{cover_uuid.replace('-', '/')}/320x320.jpg"
        if isinstance(cover_uuid, str) and cover_uuid
        else None
    )
    return {
        "tidal_id": str(a.get("id")),
        "name": a.get("title") or "",
        "artists": [ar for ar in artists if ar],
        "duration": a.get("duration") or 0,
        "cover": cover,
        "confidence": round(float(confidence), 3),
        "reason": reason,
    }


def match_artist(session, spotify_artist: dict) -> Optional[dict]:
    """Fuzzy name match, tie-broken by Tidal popularity. Artist
    matching is fuzzier than track/album matching — identical-name
    different artists are common ("Beach House" vs. another "Beach
    House" with 200 listeners). We use popularity as a tiebreaker
    because the user almost always means the big one.
    """
    name = spotify_artist.get("name") or ""
    if not name:
        return None
    try:
        resp = session.request.request(
            "GET",
            "search/artists",
            params={"query": name, "limit": 5, "countryCode": session.country_code},
        )
        if resp.status_code >= 400:
            return None
        body = resp.json()
    except Exception:
        return None
    candidates = body.get("items") or []
    if not candidates:
        return None
    # Score: high name similarity dominates; popularity nudges ties.
    best: Optional[tuple[float, dict]] = None
    max_pop = max((c.get("popularity") or 0) for c in candidates) or 1
    for c in candidates:
        name_score = _similarity(name, c.get("name") or "")
        pop_score = (c.get("popularity") or 0) / max_pop
        score = 0.85 * name_score + 0.15 * pop_score
        if best is None or score > best[0]:
            best = (score, c)
    if best is None:
        return None
    score, c = best
    if score < 0.6:
        return None
    pic_uuid = c.get("picture")
    picture = (
        f"https://resources.tidal.com/images/{pic_uuid.replace('-', '/')}/320x320.jpg"
        if isinstance(pic_uuid, str) and pic_uuid
        else None
    )
    return {
        "tidal_id": str(c.get("id")),
        "name": c.get("name") or "",
        # `artists` kept for a uniform shape with track/album matches —
        # the UI reads this field for every kind.
        "artists": [],
        "duration": 0,
        "cover": picture,
        "confidence": round(float(score), 3),
        "reason": "fuzzy",
    }


# Cap concurrent Tidal search requests per import. 6 is well inside
# tidalapi's rate budget and delivers the ~6x speed-up a 100-track
# playlist needs to finish in a few seconds instead of half a minute.
_MATCH_WORKERS = 6


def _parallel_match(
    matcher: Callable[[object, dict], Optional[dict]],
    session,
    rows: list[dict],
) -> list[Optional[dict]]:
    """Run `matcher(session, row)` across `rows` in parallel, preserving
    input order. tidalapi's request session is thread-safe; see the
    existing fan-outs in server.py `/api/artist` and `/api/album`."""
    if not rows:
        return []
    with ThreadPoolExecutor(
        max_workers=min(_MATCH_WORKERS, len(rows)),
        thread_name_prefix="import-match",
    ) as pool:
        return list(pool.map(lambda r: matcher(session, r), rows))


def match_tracks(session, rows: list[dict]) -> list[dict]:
    """Match a list of source rows against Tidal tracks in parallel.
    Shared by the Spotify playlist / liked-tracks endpoints and by
    deezer_import / playlist_import so every import path gets the
    same concurrency profile."""
    matches = _parallel_match(match_track, session, rows)
    return [
        {
            "spotify": {
                "name": r.get("name"),
                "artists": r.get("artists") or [],
                "duration_ms": r.get("duration_ms") or 0,
                "isrc": r.get("isrc"),
            },
            "match": m,
        }
        for r, m in zip(rows, matches)
    ]


def match_albums(session, rows: list[dict]) -> list[dict]:
    matches = _parallel_match(match_album, session, rows)
    return [
        {
            "spotify": {
                "name": r.get("name"),
                "artists": r.get("artists") or [],
                "duration_ms": 0,
                "isrc": None,
            },
            "match": m,
        }
        for r, m in zip(rows, matches)
    ]


def match_artists(session, rows: list[dict]) -> list[dict]:
    matches = _parallel_match(match_artist, session, rows)
    return [
        {
            "spotify": {
                "name": r.get("name"),
                "artists": [],
                "duration_ms": 0,
                "isrc": None,
            },
            "match": m,
        }
        for r, m in zip(rows, matches)
    ]
