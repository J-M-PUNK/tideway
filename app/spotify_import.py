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
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests

from app.paths import user_data_dir

log = logging.getLogger(__name__)

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"

# Minimum scopes for reading the user's playlists. "playlist-read-
# private" lets us see private playlists the user owns / is a
# collaborator on; "user-library-read" gives access to Liked Songs
# (so we can offer that as an import source).
SCOPES = "playlist-read-private user-library-read"

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
