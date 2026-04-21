"""Last.fm scrobbling integration.

Last.fm is the de-facto open protocol for music listening stats — it's
what third-party Tidal clients use because Tidal's own app has no
native last.fm support. We push two signals:

* ``updateNowPlaying`` — fired when a track starts. Tells last.fm what
  the user is listening to right now; shown on their profile. Not
  persisted.
* ``scrobble`` — fired once per play, when the track crosses last.fm's
  "actually listened" threshold (50% of duration or 4 minutes, whichever
  comes first, per their scrobble spec). Persisted into the user's
  timeline and drives top-artist/track/album charts.

Auth is the standard last.fm desktop flow:
1. Client fetches a request token (``auth.getToken``).
2. User opens ``https://www.last.fm/api/auth/?api_key=X&token=Y`` in
   a browser and approves.
3. Client calls ``auth.getSession`` with the token to mint a long-lived
   session key (doesn't expire unless the user revokes access).

Credentials live in a JSON file next to the other per-user state, with
atomic writes + chmod 600 since the session key is effectively a
password for the user's last.fm account.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from typing import Optional
from urllib.parse import urlencode

import requests

from app.paths import user_data_dir

LASTFM_FILE = user_data_dir() / "lastfm.json"
API_ROOT = "https://ws.audioscrobbler.com/2.0/"
AUTH_URL = "https://www.last.fm/api/auth/"

# Cap concurrent outbound Last.fm requests. Their documented fair-use
# limit is ~5 req/sec per IP; bursts above that get 429s. When a 50-
# track album loads and each TrackRow's frontend hook fires a
# track.getInfo request, they'd all hit our server almost
# simultaneously and fan out to Last.fm at once without this. The
# semaphore queues them so we stay well under their threshold at the
# cost of a few extra hundred ms for the last tracks in the list.
_LASTFM_CONCURRENCY = 4
_lastfm_api_semaphore = threading.BoundedSemaphore(_LASTFM_CONCURRENCY)

# --- Baked-in app credentials ------------------------------------------------
#
# Paste your Last.fm API credentials here ONCE, rebuild, and every launch
# after will use them automatically. Users never see the API key fields in
# Settings — the flow is just "Connect" → approve in browser → done.
#
# How to get them (5-minute one-time step):
#   1. Log into your Last.fm account.
#   2. Go to https://www.last.fm/api/account/create
#   3. Fill in any Application name + Description. Leave Callback URL blank.
#   4. Submit. The next page shows your API key and Shared secret.
#   5. Paste them below and save this file.
#
# Leaving these blank falls back to the manual-entry flow in Settings,
# where the user pastes their own credentials (useful for development
# or for anyone who'd rather use their own API application).
_DEFAULT_API_KEY = ""
_DEFAULT_API_SECRET = ""


@dataclass
class LastFmCreds:
    """Persisted last.fm credentials. ``api_key`` and ``api_secret``
    identify the app and come from last.fm's API-account page. The
    ``session_key`` (and corresponding ``username``) are minted once
    per user via the browser auth flow and never expire on their own.
    """

    api_key: str = ""
    api_secret: str = ""
    session_key: str = ""
    username: str = ""


def _safe_int(value) -> int:
    """Coerce Last.fm's mixed-type numeric fields (string, int, None) to
    int without raising. Their JSON is notorious for sending numbers as
    strings, and `userplaycount` is missing entirely on first-time views
    rather than being zero."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _best_lastfm_image(images: list) -> str:
    """Last.fm embeds track art as a list of {size, #text} entries.
    Pick the largest available URL. Last.fm's own placeholder URL
    (`2a96cbd8b46e442fc41c2b86b821562f.png`) leaks through when a
    track has no real art — filter that out so the UI can fall back
    to a generic icon instead of showing Last.fm's grey star."""
    by_size = {
        i.get("size"): i.get("#text")
        for i in images
        if isinstance(i, dict) and i.get("#text")
    }
    for s in ("extralarge", "large", "medium", "small"):
        url = by_size.get(s)
        if url and "2a96cbd8b46e442fc41c2b86b821562f" not in url:
            return url
    return ""


def _sign(params: dict, api_secret: str) -> str:
    """Compute last.fm's api_sig: md5(sorted k/v pairs + secret)."""
    # format + callback are explicitly excluded from the signature per
    # last.fm's spec — including them gives "Invalid method signature".
    filtered = {k: v for k, v in params.items() if k not in ("format", "callback")}
    joined = "".join(f"{k}{filtered[k]}" for k in sorted(filtered))
    return hashlib.md5((joined + api_secret).encode("utf-8")).hexdigest()


class LastFmClient:
    def __init__(self) -> None:
        self.creds = LastFmCreds()
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not LASTFM_FILE.exists():
            return
        try:
            data = json.loads(LASTFM_FILE.read_text())
            self.creds = LastFmCreds(
                api_key=str(data.get("api_key", "")),
                api_secret=str(data.get("api_secret", "")),
                session_key=str(data.get("session_key", "")),
                username=str(data.get("username", "")),
            )
        except Exception:
            # Corrupt file — start blank. User can re-enter credentials.
            pass

    def _save(self) -> None:
        """Atomic write + 0600 perms. The session key is a long-lived
        credential for the user's last.fm account, so we don't leave it
        world-readable on disk."""
        data = asdict(self.creds)
        parent = str(LASTFM_FILE.parent)
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".lastfm.", suffix=".tmp", dir=parent
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(data, f, indent=2)
            try:
                os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            os.replace(tmp_path, LASTFM_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Credential resolution
    # ------------------------------------------------------------------

    def _effective_api_key(self) -> str:
        """User-entered key wins; fall back to the module-level default.
        Lets builds ship with baked-in app credentials so end users
        never see the API key fields."""
        return self.creds.api_key or _DEFAULT_API_KEY

    def _effective_api_secret(self) -> str:
        return self.creds.api_secret or _DEFAULT_API_SECRET

    # ------------------------------------------------------------------
    # Public state
    # ------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            c = self.creds
            return {
                # True if either the user entered credentials OR the build
                # ships with baked-in defaults. Either way the UI can skip
                # to the Connect step.
                "has_credentials": bool(
                    self._effective_api_key() and self._effective_api_secret()
                ),
                # When baked-in defaults are present, the user never
                # needed to paste anything. Signals to the UI that the
                # "reset credentials" affordance is pointless.
                "using_default_credentials": bool(
                    _DEFAULT_API_KEY
                    and _DEFAULT_API_SECRET
                    and not c.api_key
                    and not c.api_secret
                ),
                "connected": bool(c.session_key),
                "username": c.username or None,
            }

    def set_credentials(self, api_key: str, api_secret: str) -> None:
        """Store the user's API credentials (from last.fm/api/account/create).
        Drops any existing session — a different app key means a different
        app identity and the old session won't work."""
        new_key = api_key.strip()
        new_secret = api_secret.strip()
        with self._lock:
            # Preserve the session if credentials are unchanged (user
            # clicked Save twice). Session keys belong to (user × app),
            # so a different app identity invalidates them.
            keep_session = (
                new_key == self.creds.api_key
                and new_secret == self.creds.api_secret
            )
            self.creds = LastFmCreds(
                api_key=new_key,
                api_secret=new_secret,
                session_key=self.creds.session_key if keep_session else "",
                username=self.creds.username if keep_session else "",
            )
            self._save()

    def disconnect(self) -> None:
        """Drop the session key so scrobbling stops. Keeps the api_key /
        api_secret so the user can reconnect with one click."""
        with self._lock:
            self.creds.session_key = ""
            self.creds.username = ""
            self._save()

    # ------------------------------------------------------------------
    # Auth flow
    # ------------------------------------------------------------------

    def get_auth_url(self) -> tuple[str, str]:
        """Start the browser auth flow. Returns (auth_url, token).

        Caller should open `auth_url` in the user's default browser, then
        call ``complete_auth(token)`` after the user says they've
        approved. Tokens expire 60 minutes after issue.
        """
        with self._lock:
            key = self._effective_api_key()
            secret = self._effective_api_secret()
            if not key or not secret:
                raise RuntimeError("Set Last.fm API credentials first")
            params = {
                "method": "auth.getToken",
                "api_key": key,
                "format": "json",
            }
            params["api_sig"] = _sign(params, secret)
            resp = requests.get(API_ROOT, params=params, timeout=10)
            resp.raise_for_status()
            token = resp.json().get("token")
            if not token:
                raise RuntimeError("Last.fm didn't issue a token")
            url = AUTH_URL + "?" + urlencode({"api_key": key, "token": token})
            return url, token

    def complete_auth(self, token: str) -> str:
        """Exchange a browser-approved token for a session key. Returns
        the username on success; raises on failure (most commonly when
        the user hasn't actually approved yet)."""
        with self._lock:
            key = self._effective_api_key()
            secret = self._effective_api_secret()
            if not key or not secret:
                raise RuntimeError("Set Last.fm API credentials first")
            params = {
                "method": "auth.getSession",
                "api_key": key,
                "token": token,
                "format": "json",
            }
            params["api_sig"] = _sign(params, secret)
            resp = requests.get(API_ROOT, params=params, timeout=10)
            if not resp.ok:
                # last.fm returns a human-readable error message in
                # `message`; pass it through so the UI can show the
                # actual reason (e.g. "Unauthorized Token - This token
                # has not been authorized").
                try:
                    msg = resp.json().get("message") or resp.text
                except Exception:
                    msg = resp.text
                raise RuntimeError(msg)
            session = resp.json().get("session") or {}
            key = session.get("key")
            name = session.get("name") or ""
            if not key:
                raise RuntimeError("Last.fm didn't return a session key")
            self.creds.session_key = key
            self.creds.username = name
            self._save()
            return name

    # ------------------------------------------------------------------
    # Scrobbling
    # ------------------------------------------------------------------

    def _call(self, method: str, extra: dict) -> dict:
        """Signed POST to the last.fm API. Common path for scrobble +
        updateNowPlaying."""
        with self._lock:
            if not self.creds.session_key:
                raise RuntimeError("Not connected to Last.fm")
            key = self._effective_api_key()
            secret = self._effective_api_secret()
            params = {
                "method": method,
                "api_key": key,
                "sk": self.creds.session_key,
                "format": "json",
                **extra,
            }
            params["api_sig"] = _sign(params, secret)
        # Release the lock before the network hop so a slow last.fm
        # response doesn't block other threads (e.g. a settings write).
        resp = requests.post(API_ROOT, data=params, timeout=10)
        if not resp.ok:
            try:
                data = resp.json()
            except Exception:
                data = {}
            msg = data.get("message") or resp.text
            raise RuntimeError(f"Last.fm {method} failed: {msg}")
        return resp.json() if resp.text else {}

    def now_playing(self, artist: str, track: str, album: str = "",
                    duration: int = 0) -> None:
        if not artist or not track:
            return
        extra = {"artist": artist, "track": track}
        if album:
            extra["album"] = album
        if duration > 0:
            extra["duration"] = str(int(duration))
        self._call("track.updateNowPlaying", extra)

    # ------------------------------------------------------------------
    # Public read endpoints. These only need api_key + username, no
    # session signing. Used by the Stats dashboard.
    # ------------------------------------------------------------------

    _VALID_PERIODS = ("overall", "7day", "1month", "3month", "6month", "12month")

    def _public_get(self, params: dict) -> Optional[dict]:
        """Unsigned GET against the API root. Returns the parsed JSON
        body on success, None on any network/parse failure. Callers
        handle empty results gracefully."""
        with self._lock:
            username = self.creds.username
            api_key = self._effective_api_key()
        if not username or not api_key:
            return None
        params = {**params, "user": username, "api_key": api_key, "format": "json"}
        try:
            with _lastfm_api_semaphore:
                resp = requests.get(API_ROOT, params=params, timeout=10)
        except Exception as exc:
            print(
                f"[lastfm] public GET network error: {exc!r}",
                file=sys.stderr,
                flush=True,
            )
            return None
        if not resp.ok:
            return None
        try:
            return resp.json()
        except Exception:
            return None

    def get_user_info(self) -> dict:
        """User profile header: total playcount, registered date, avatar."""
        data = self._public_get({"method": "user.getInfo"})
        if not data:
            return {}
        user = data.get("user") or {}
        reg_raw = user.get("registered") or {}
        # Last.fm returns `registered` as {"unixtime": "123", "#text": 123}
        reg_ts = None
        if isinstance(reg_raw, dict):
            try:
                reg_ts = int(reg_raw.get("unixtime") or reg_raw.get("#text") or 0) or None
            except (TypeError, ValueError):
                reg_ts = None
        return {
            "username": user.get("name") or "",
            "realname": user.get("realname") or "",
            "playcount": int(user.get("playcount") or 0),
            "track_count": int(user.get("track_count") or 0),
            "artist_count": int(user.get("artist_count") or 0),
            "album_count": int(user.get("album_count") or 0),
            "country": user.get("country") or "",
            "url": user.get("url") or "",
            "registered_at": reg_ts,
            "image": _best_lastfm_image(user.get("image") or []),
        }

    def get_top_artists(self, period: str = "overall", limit: int = 50) -> list[dict]:
        if period not in self._VALID_PERIODS:
            period = "overall"
        data = self._public_get({
            "method": "user.getTopArtists",
            "period": period,
            "limit": str(max(1, min(1000, int(limit)))),
        })
        if not data:
            return []
        raw = (data.get("topartists") or {}).get("artist") or []
        if isinstance(raw, dict):
            raw = [raw]
        out: list[dict] = []
        for a in raw:
            if not isinstance(a, dict):
                continue
            out.append({
                "name": a.get("name") or "",
                "playcount": int(a.get("playcount") or 0),
                "url": a.get("url") or "",
                "image": _best_lastfm_image(a.get("image") or []),
                "mbid": a.get("mbid") or "",
            })
        return out

    def get_top_tracks(self, period: str = "overall", limit: int = 50) -> list[dict]:
        if period not in self._VALID_PERIODS:
            period = "overall"
        data = self._public_get({
            "method": "user.getTopTracks",
            "period": period,
            "limit": str(max(1, min(1000, int(limit)))),
        })
        if not data:
            return []
        raw = (data.get("toptracks") or {}).get("track") or []
        if isinstance(raw, dict):
            raw = [raw]
        out: list[dict] = []
        for t in raw:
            if not isinstance(t, dict):
                continue
            artist = t.get("artist") or {}
            out.append({
                "name": t.get("name") or "",
                "artist": artist.get("name") if isinstance(artist, dict) else str(artist),
                "playcount": int(t.get("playcount") or 0),
                "duration": int(t.get("duration") or 0),
                "url": t.get("url") or "",
                "image": _best_lastfm_image(t.get("image") or []),
            })
        return out

    def get_top_albums(self, period: str = "overall", limit: int = 50) -> list[dict]:
        if period not in self._VALID_PERIODS:
            period = "overall"
        data = self._public_get({
            "method": "user.getTopAlbums",
            "period": period,
            "limit": str(max(1, min(1000, int(limit)))),
        })
        if not data:
            return []
        raw = (data.get("topalbums") or {}).get("album") or []
        if isinstance(raw, dict):
            raw = [raw]
        out: list[dict] = []
        for a in raw:
            if not isinstance(a, dict):
                continue
            artist = a.get("artist") or {}
            out.append({
                "name": a.get("name") or "",
                "artist": artist.get("name") if isinstance(artist, dict) else str(artist),
                "playcount": int(a.get("playcount") or 0),
                "url": a.get("url") or "",
                "image": _best_lastfm_image(a.get("image") or []),
            })
        return out

    def get_loved_tracks(self, limit: int = 50) -> list[dict]:
        """Tracks the user has "loved" (heart-clicked) on Last.fm."""
        data = self._public_get({
            "method": "user.getLovedTracks",
            "limit": str(max(1, min(1000, int(limit)))),
        })
        if not data:
            return []
        raw = (data.get("lovedtracks") or {}).get("track") or []
        if isinstance(raw, dict):
            raw = [raw]
        out: list[dict] = []
        for t in raw:
            if not isinstance(t, dict):
                continue
            artist = t.get("artist") or {}
            date = t.get("date") or {}
            ts = None
            if isinstance(date, dict) and date.get("uts"):
                try:
                    ts = int(date["uts"])
                except (TypeError, ValueError):
                    ts = None
            out.append({
                "name": t.get("name") or "",
                "artist": artist.get("name") if isinstance(artist, dict) else str(artist),
                "loved_at": ts,
                "url": t.get("url") or "",
                "image": _best_lastfm_image(t.get("image") or []),
            })
        return out

    # ------------------------------------------------------------------
    # Global charts. Not user-scoped — these work even without a Last.fm
    # session, only an api_key. Used to power the "Popular" page with
    # crowd-sourced charts alongside Tidal's editorial ones.
    # ------------------------------------------------------------------

    def _public_get_no_user(self, params: dict) -> Optional[dict]:
        """Like `_public_get` but skips the `user` parameter. Chart
        endpoints are truly global so the user-scoped GET helper's
        requirement that a username be set doesn't apply."""
        with self._lock:
            api_key = self._effective_api_key()
        if not api_key:
            return None
        params = {**params, "api_key": api_key, "format": "json"}
        try:
            with _lastfm_api_semaphore:
                resp = requests.get(API_ROOT, params=params, timeout=10)
        except Exception as exc:
            print(
                f"[lastfm] public GET network error: {exc!r}",
                file=sys.stderr,
                flush=True,
            )
            return None
        if not resp.ok:
            return None
        try:
            return resp.json()
        except Exception:
            return None

    def get_chart_top_artists(self, limit: int = 50) -> list[dict]:
        data = self._public_get_no_user({
            "method": "chart.getTopArtists",
            "limit": str(max(1, min(500, int(limit)))),
        })
        if not data:
            return []
        raw = (data.get("artists") or {}).get("artist") or []
        if isinstance(raw, dict):
            raw = [raw]
        out: list[dict] = []
        for a in raw:
            if not isinstance(a, dict):
                continue
            out.append({
                "name": a.get("name") or "",
                "playcount": _safe_int(a.get("playcount")),
                "listeners": _safe_int(a.get("listeners")),
                "url": a.get("url") or "",
                "image": _best_lastfm_image(a.get("image") or []),
                "mbid": a.get("mbid") or "",
            })
        return out

    def get_chart_top_tracks(self, limit: int = 50) -> list[dict]:
        data = self._public_get_no_user({
            "method": "chart.getTopTracks",
            "limit": str(max(1, min(500, int(limit)))),
        })
        if not data:
            return []
        raw = (data.get("tracks") or {}).get("track") or []
        if isinstance(raw, dict):
            raw = [raw]
        out: list[dict] = []
        for t in raw:
            if not isinstance(t, dict):
                continue
            artist = t.get("artist") or {}
            out.append({
                "name": t.get("name") or "",
                "artist": artist.get("name") if isinstance(artist, dict) else str(artist),
                "playcount": _safe_int(t.get("playcount")),
                "listeners": _safe_int(t.get("listeners")),
                "duration": _safe_int(t.get("duration")),
                "url": t.get("url") or "",
                "image": _best_lastfm_image(t.get("image") or []),
            })
        return out

    def get_chart_top_tags(self, limit: int = 50) -> list[dict]:
        data = self._public_get_no_user({
            "method": "chart.getTopTags",
            "limit": str(max(1, min(500, int(limit)))),
        })
        if not data:
            return []
        raw = (data.get("tags") or {}).get("tag") or []
        if isinstance(raw, dict):
            raw = [raw]
        out: list[dict] = []
        for t in raw:
            if not isinstance(t, dict):
                continue
            out.append({
                "name": t.get("name") or "",
                "taggings": _safe_int(t.get("taggings")),
                "reach": _safe_int(t.get("reach")),
                "url": t.get("url") or "",
            })
        return out

    # ------------------------------------------------------------------
    # Per-user playcounts on specific entities. Last.fm's artist.getInfo,
    # album.getInfo, and track.getInfo all return a `userplaycount` field
    # when called with `user=X`. Used to badge "you've played this N
    # times" on detail pages.
    # ------------------------------------------------------------------

    def _info_get(self, params: dict) -> Optional[dict]:
        """Helper for *.getInfo calls that want `user=X` when available
        but still work without it — global listeners/playcount come back
        regardless, while `userplaycount` needs the user parameter.
        Lets us show crowd stats on artist/album/track pages even if
        Last.fm isn't connected, only that the api_key is configured."""
        with self._lock:
            username = self.creds.username
            api_key = self._effective_api_key()
        if not api_key:
            return None
        full = {**params, "api_key": api_key, "format": "json"}
        if username:
            full["user"] = username
        try:
            with _lastfm_api_semaphore:
                resp = requests.get(API_ROOT, params=full, timeout=10)
        except Exception as exc:
            print(
                f"[lastfm] _info_get network error: {exc!r}",
                file=sys.stderr,
                flush=True,
            )
            return None
        if not resp.ok:
            return None
        try:
            return resp.json()
        except Exception:
            return None

    def get_artist_playcount(self, artist: str) -> dict:
        data = self._info_get({"method": "artist.getInfo", "artist": artist})
        if not data:
            return {}
        a = data.get("artist") or {}
        stats = a.get("stats") or {}
        return {
            "userplaycount": _safe_int(stats.get("userplaycount")),
            "listeners": _safe_int(stats.get("listeners")),
            "playcount": _safe_int(stats.get("playcount")),
            "url": a.get("url") or "",
        }

    def get_album_playcount(self, artist: str, album: str) -> dict:
        data = self._info_get({
            "method": "album.getInfo",
            "artist": artist,
            "album": album,
            "autocorrect": "1",
        })
        if not data:
            return {}
        a = data.get("album") or {}
        return {
            "userplaycount": _safe_int(a.get("userplaycount")),
            "listeners": _safe_int(a.get("listeners")),
            "playcount": _safe_int(a.get("playcount")),
            "url": a.get("url") or "",
        }

    def get_track_playcount(self, artist: str, track: str) -> dict:
        data = self._info_get({
            "method": "track.getInfo",
            "artist": artist,
            "track": track,
            "autocorrect": "1",
        })
        if not data:
            return {}
        t = data.get("track") or {}
        return {
            "userplaycount": _safe_int(t.get("userplaycount")),
            "userloved": str(t.get("userloved", "0")) == "1",
            "listeners": _safe_int(t.get("listeners")),
            "playcount": _safe_int(t.get("playcount")),
            "url": t.get("url") or "",
        }

    # ------------------------------------------------------------------
    # Weekly scrobble counts for the last N weeks. Used to render the
    # listening-activity bar chart on the Stats page.
    #
    # Last.fm's `user.getRecentTracks` lets us query a time range and
    # read the total count from the `@attr.total` envelope without
    # actually downloading the tracks (limit=1, one-item response).
    # We fire one call per week in parallel since each is independent.
    # ------------------------------------------------------------------

    def get_weekly_scrobbles(self, weeks: int = 52) -> list[dict]:
        from concurrent.futures import ThreadPoolExecutor

        with self._lock:
            username = self.creds.username
            api_key = self._effective_api_key()
        if not username or not api_key:
            return []
        weeks = max(1, min(104, int(weeks)))
        now = int(time.time())
        # Snap to start-of-day UTC so adjacent bars don't slice odd
        # minute-boundaries. Doesn't need to be perfect — the visual
        # effect is "roughly this week vs that week".
        day = 86400
        end_today = (now // day) * day + day
        buckets: list[tuple[int, int]] = []
        for i in range(weeks):
            to_ts = end_today - i * 7 * day
            from_ts = to_ts - 7 * day
            buckets.append((from_ts, to_ts))

        def _count(ft: tuple[int, int]) -> dict:
            frm, to = ft
            params = {
                "method": "user.getRecentTracks",
                "user": username,
                "api_key": api_key,
                "format": "json",
                "limit": "1",
                "from": str(frm),
                "to": str(to),
            }
            try:
                with _lastfm_api_semaphore:
                    resp = requests.get(API_ROOT, params=params, timeout=10)
                if not resp.ok:
                    return {"from": frm, "to": to, "count": 0}
                data = resp.json() or {}
                attr = (data.get("recenttracks") or {}).get("@attr") or {}
                total = _safe_int(attr.get("total"))
            except Exception:
                total = 0
            return {"from": frm, "to": to, "count": total}

        # 10 parallel workers keeps us well under Last.fm's 5-req/sec
        # per-IP rate limit across a 52-week fetch (~5s wall-clock).
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(_count, buckets))
        # Return oldest → newest so the UI doesn't need to reverse.
        results.sort(key=lambda r: r["from"])
        return results

    def get_recent_tracks(self, limit: int = 50) -> list[dict]:
        """Fetch the user's recent scrobbles via ``user.getRecentTracks``.

        Public endpoint — only needs api_key + username, no signing and
        no session key. That means we can read history even if the user
        hasn't finished the Connect flow yet, as long as we know their
        username (which `complete_auth` already persists).

        Returns normalized dicts with `artist`, `track`, `album`,
        `played_at` (UNIX epoch, null for the now-playing row), `cover`
        (best-available image URL), and `now_playing` (true for the one
        special row Last.fm emits when the user is actively listening
        somewhere).
        """
        with self._lock:
            username = self.creds.username
            api_key = self._effective_api_key()
        if not username or not api_key:
            return []
        params = {
            "method": "user.getRecentTracks",
            "user": username,
            "api_key": api_key,
            "format": "json",
            "limit": str(max(1, min(200, int(limit)))),
        }
        try:
            with _lastfm_api_semaphore:
                resp = requests.get(API_ROOT, params=params, timeout=10)
        except Exception as exc:
            print(
                f"[lastfm] get_recent_tracks network error: {exc!r}",
                file=sys.stderr,
                flush=True,
            )
            return []
        if not resp.ok:
            return []
        try:
            data = resp.json()
        except Exception:
            return []
        raw = (data.get("recenttracks") or {}).get("track") or []
        # Last.fm returns a single object instead of a list when there's
        # only one row. Normalize.
        if isinstance(raw, dict):
            raw = [raw]
        out: list[dict] = []
        for t in raw:
            if not isinstance(t, dict):
                continue
            attr = t.get("@attr") or {}
            now_playing = str(attr.get("nowplaying", "")).lower() == "true"
            date_obj = t.get("date") or {}
            played_at = None
            if date_obj.get("uts"):
                try:
                    played_at = int(date_obj["uts"])
                except (TypeError, ValueError):
                    played_at = None
            artist_raw = t.get("artist")
            if isinstance(artist_raw, dict):
                artist = artist_raw.get("#text") or artist_raw.get("name") or ""
            else:
                artist = str(artist_raw or "")
            album_raw = t.get("album")
            album = album_raw.get("#text") if isinstance(album_raw, dict) else ""
            out.append(
                {
                    "artist": artist,
                    "track": t.get("name") or "",
                    "album": album or "",
                    "played_at": played_at,
                    "now_playing": now_playing,
                    "cover": _best_lastfm_image(t.get("image") or []),
                    "url": t.get("url") or "",
                }
            )
        return out

    def scrobble(self, artist: str, track: str, album: str = "",
                 duration: int = 0, timestamp: Optional[int] = None) -> None:
        if not artist or not track:
            return
        ts = timestamp if timestamp is not None else int(time.time())
        extra = {
            "artist[0]": artist,
            "track[0]": track,
            "timestamp[0]": str(ts),
        }
        if album:
            extra["album[0]"] = album
        if duration > 0:
            extra["duration[0]"] = str(int(duration))
        try:
            self._call("track.scrobble", extra)
        except Exception as exc:
            # Don't surface scrobble errors to callers — the user is
            # trying to listen to music, not debug last.fm. Log for
            # visibility and move on.
            print(
                f"[lastfm] scrobble failed: {exc!r}",
                file=sys.stderr,
                flush=True,
            )
