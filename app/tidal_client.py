import json
import os
import re
import stat
import tempfile
import threading
import time
import webbrowser
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import tidalapi

from app.paths import user_data_dir

SESSION_FILE = user_data_dir() / "tidal_session.json"


def _fetch_all_pages(method) -> list:
    """Exhaustively fetch every item from a paginated tidalapi list method.

    Strategy, in order:
      1. Ask for a huge single page with newest-first ordering.
      2. If that returns nothing or throws, ask for a huge single page with no
         ordering (some older tidalapi versions don't accept the kwargs).
      3. If still nothing, page through manually in chunks of 50.
      4. As a final fallback, call with no arguments.
    """
    # 1) Single-shot ordered fetch
    for kwargs in (
        {"limit": 10000, "order": "DATE", "order_direction": "DESC"},
        {"limit": 10000},
    ):
        try:
            items = list(method(**kwargs))
            if items:
                return items
        except Exception:
            pass

    # 2) Manual pagination in chunks of 50
    collected: list = []
    seen_ids: set = set()
    offset = 0
    page_size = 50
    while True:
        page: list = []
        for kwargs in (
            {"limit": page_size, "offset": offset},
            {"limit": page_size, "offset": offset, "order": "DATE", "order_direction": "DESC"},
        ):
            try:
                page = list(method(**kwargs))
                break
            except Exception:
                continue
        if not page:
            break
        # Duplicate-detection safety: if the endpoint misbehaves and keeps
        # returning the same page regardless of offset (happens on some
        # tidalapi versions / under transient 5xx retries), `len(page)
        # == page_size` would loop until the 20k cap with a completely
        # duplicated `collected` list. Break as soon as a page contributes
        # zero new items.
        added = 0
        for obj in page:
            key = getattr(obj, "id", None)
            if key is None:
                key = getattr(obj, "uuid", None)
            if key is None:
                # Item without a stable identifier — append unconditionally
                # but don't count it as "new" for the progress check.
                collected.append(obj)
                continue
            if key in seen_ids:
                continue
            seen_ids.add(key)
            collected.append(obj)
            added += 1
        if added == 0:
            break
        if len(page) < page_size:
            break
        offset += page_size
        if offset > 20000:  # hard safety cap
            break
    if collected:
        return collected

    # 3) Last resort
    try:
        return list(method())
    except Exception:
        return []


def _sort_by_date_added(items: list) -> list:
    """Sort items newest-first using whichever timestamp attribute tidalapi
    exposes. If no such attribute is found, return the list unchanged."""
    if not items:
        return items

    candidates = ("user_date_added", "date_added", "created", "added_at", "dateAdded")

    def key(obj):
        for attr in candidates:
            value = getattr(obj, attr, None)
            if value is None:
                continue
            if isinstance(value, datetime):
                return value.timestamp()
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
            except Exception:
                try:
                    return float(value)
                except Exception:
                    continue
        return None

    keyed = [(key(o), i, o) for i, o in enumerate(items)]
    if all(k is None for k, _, _ in keyed):
        return items  # no timestamp available; preserve server order
    # Items without a timestamp fall to the bottom
    keyed.sort(key=lambda t: (t[0] is None, -(t[0] or 0), t[1]))
    return [o for _, _, o in keyed]


# Tidal returns a `highestSoundQuality` code on the subscription endpoint.
# Map those codes to the tidalapi.Quality member names we use everywhere
# else in the app, and keep them in ascending order so "everything at or
# below X" is a simple slice.
_SUB_TTL_SEC = 300.0  # 5 minutes; subscriptions change rarely.

_SUB_QUALITY_ORDER = ["low_96k", "low_320k", "high_lossless", "hi_res_lossless"]
_SUB_QUALITY_MAP = {
    "LOW": "low_96k",
    "HIGH": "low_320k",
    "LOSSLESS": "high_lossless",
    "HI_RES_LOSSLESS": "hi_res_lossless",
    # Tidal's current single-tier subscription reports `HI_RES` as the
    # `highestSoundQuality`, which DOES include hi-res FLAC (Max). The
    # catch: only a PKCE-authenticated session's access_token is
    # entitled to stream it — device-code sessions 401 at Max even
    # though the subscription allows it. We therefore translate HI_RES
    # to hi_res_lossless ONLY when the session is PKCE; for device-code
    # sessions we fall back to high_lossless so the UI doesn't offer
    # an unreachable option. See get_max_quality() below.
    "HI_RES": "hi_res_lossless",
    "MASTER": "hi_res_lossless",
    "HIFI": "high_lossless",
    "HIFI_PLUS": "hi_res_lossless",
}

# Client-level ceiling: even if the subscription says Max, the device-code
# OAuth client_id is capped at Lossless. Only the PKCE client is entitled
# for hi-res.
_DEVICE_CODE_MAX = "high_lossless"


class TidalClient:
    # Proactive refresh window: if the stored expiry is within this many
    # seconds of "now" when the watchdog ticks, refresh the token.
    # tidalapi tokens last an hour, so a 5-minute cushion gives us plenty
    # of margin without thrashing on refreshes.
    _REFRESH_WINDOW_SEC = 5 * 60
    # How often the watchdog re-checks. 60s is frequent enough that the
    # expiry window can't slip past between checks, but light enough on
    # cycles that nobody notices.
    _REFRESH_CHECK_INTERVAL_SEC = 60

    def __init__(self):
        config = tidalapi.Config(quality=tidalapi.Quality.high_lossless)
        self.session = tidalapi.Session(config)
        self._login_future: Optional[Future] = None
        # Cached subscription-tier result. Populated on first call and
        # refreshed every _SUB_TTL_SEC. Subscription almost never changes
        # within a session, so a long TTL is fine.
        self._sub_cache: tuple[float, Optional[str]] = (0.0, None)
        self._sub_lock = threading.Lock()
        # Serializes force_refresh calls so concurrent 401s from parallel
        # workers don't fire two token_refresh RPCs at once (second one
        # would race and potentially invalidate the first's token).
        self._refresh_lock = threading.Lock()
        # Background watchdog: refreshes the token a few minutes before
        # it's due to expire. Without this, a long-running session (user
        # leaves the tab open overnight) lands a 401 the next time they
        # click anything, and though downstream callers have their own
        # retry-on-401 wrappers, it's a worse UX than just keeping the
        # token fresh in the background. Daemon so it doesn't block
        # process exit.
        self._refresh_stop = threading.Event()
        threading.Thread(
            target=self._refresh_watchdog, daemon=True, name="tidal-refresh"
        ).start()

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def load_session(self) -> bool:
        if not SESSION_FILE.exists():
            return False
        try:
            with open(SESSION_FILE) as f:
                data = json.load(f)
            expiry = (
                datetime.fromisoformat(data["expiry_time"])
                if data.get("expiry_time")
                else None
            )
            refresh_token = data.get("refresh_token")
            is_pkce = bool(data.get("is_pkce", False))
            # For PKCE sessions, swap the client_id to the hi-res-entitled
            # one BEFORE any Tidal call. Otherwise the /sessions call
            # inside load_oauth_session (and the proactive refresh below)
            # would run under the device-code client_id and the resulting
            # tokens/session wouldn't be entitled for Max quality.
            if is_pkce:
                self.session.is_pkce = True
                self.session.client_enable_hires()
            # Proactive refresh: tidalapi only refreshes on 401 when the
            # response's userMessage starts with the exact string
            # "The token has expired." — Tidal has changed that message
            # format in the past, and when it doesn't match we get raw
            # 401s propagating to the caller (e.g. download endpoints).
            # If the stored expiry is in the past, skip trying the stale
            # token and refresh up front.
            now = datetime.now(expiry.tzinfo) if expiry and expiry.tzinfo else datetime.now()
            if expiry and refresh_token and expiry <= now:
                try:
                    if self.session.token_refresh(refresh_token):
                        self.save_session()
                except Exception:
                    pass
            self.session.load_oauth_session(
                data["token_type"],
                self.session.access_token or data["access_token"],
                getattr(self.session, "refresh_token", None) or refresh_token,
                self.session.expiry_time or expiry,
                is_pkce=is_pkce,
            )
            return self.session.check_login()
        except Exception:
            return False

    def force_refresh(self) -> bool:
        """Explicitly refresh the access token using the stored refresh
        token. Called by the download path when it hits a 401 — tidalapi's
        built-in refresh only triggers on a specific error message we
        can't rely on. Returns True on success.

        Logs to stderr on failure so the caller (and whoever is tailing
        the server log) can see whether the refresh actually ran and
        why it failed. If the refresh token is itself expired, wipes the
        persisted session file so the user is bounced to the login flow
        on next auth check instead of being stuck in a retry loop.

        Serialized on _refresh_lock so concurrent 401s from parallel
        workers don't fire two token_refresh RPCs at once. The second
        caller will see the fresh token after the first finishes.
        """
        import sys as _sys

        with self._refresh_lock:
            refresh_token = getattr(self.session, "refresh_token", None)
            if not refresh_token:
                print(
                    "[tidal] force_refresh: no refresh_token on session",
                    file=_sys.stderr,
                    flush=True,
                )
                return False
            try:
                ok = self.session.token_refresh(refresh_token)
            except Exception as exc:
                print(
                    f"[tidal] force_refresh: token_refresh raised: {exc!r}",
                    file=_sys.stderr,
                    flush=True,
                )
                # AuthenticationError means the refresh token itself is dead.
                # Blow the session away so the user is prompted to log in
                # again rather than chasing 401s forever.
                if type(exc).__name__ == "AuthenticationError":
                    try:
                        self.logout()
                    except Exception:
                        pass
                return False
            if not ok:
                print(
                    "[tidal] force_refresh: token_refresh returned False",
                    file=_sys.stderr,
                    flush=True,
                )
                return False
            self.save_session()
            print("[tidal] force_refresh: success", file=_sys.stderr, flush=True)
            return True

    def _refresh_watchdog(self) -> None:
        """Background loop that refreshes the token before it expires.

        Sleeps for _REFRESH_CHECK_INTERVAL_SEC between checks. Each tick:
        if we're logged in and the stored expiry is within the window,
        fire force_refresh. Errors inside force_refresh are already
        logged there; the watchdog swallows its own errors so a transient
        network blip doesn't kill the thread.
        """
        import sys as _sys

        while not self._refresh_stop.is_set():
            # Wake up early if someone signals stop (e.g. tests).
            if self._refresh_stop.wait(self._REFRESH_CHECK_INTERVAL_SEC):
                return
            try:
                expiry = getattr(self.session, "expiry_time", None)
                refresh_token = getattr(self.session, "refresh_token", None)
                if not expiry or not refresh_token:
                    continue
                now = (
                    datetime.now(expiry.tzinfo)
                    if getattr(expiry, "tzinfo", None)
                    else datetime.now()
                )
                seconds_left = (expiry - now).total_seconds()
                if seconds_left > self._REFRESH_WINDOW_SEC:
                    continue
                print(
                    f"[tidal] refresh watchdog: token expires in "
                    f"{seconds_left:.0f}s — refreshing",
                    file=_sys.stderr,
                    flush=True,
                )
                self.force_refresh()
            except Exception as exc:
                print(
                    f"[tidal] refresh watchdog: ignoring error: {exc!r}",
                    file=_sys.stderr,
                    flush=True,
                )

    def get_max_quality(self) -> Optional[str]:
        """Return the highest audio quality the logged-in account is
        allowed to stream, as a tidalapi Quality member name (e.g.
        'high_lossless'). None means the check failed or the user isn't
        logged in yet — caller should assume no filtering in that case.

        The subscription endpoint is the only source of truth for this;
        tidalapi itself doesn't surface the tier. Cached for _SUB_TTL_SEC
        so the frontend hitting /api/qualities on every mount doesn't
        fan out to a network call each time.
        """
        import sys as _sys

        now = time.monotonic()
        with self._sub_lock:
            cached_at, cached_val = self._sub_cache
            if now - cached_at < _SUB_TTL_SEC and cached_val is not None:
                return cached_val
        try:
            user = getattr(self.session, "user", None)
            uid = getattr(user, "id", None) if user else None
            if not uid:
                print(
                    "[tidal] get_max_quality: no user.id yet",
                    file=_sys.stderr,
                    flush=True,
                )
                return None
            resp = self.session.request.basic_request(
                "GET", f"users/{uid}/subscription"
            )
            if not resp.ok:
                print(
                    f"[tidal] get_max_quality: subscription fetch returned "
                    f"{resp.status_code}",
                    file=_sys.stderr,
                    flush=True,
                )
                return None
            data = resp.json()
        except Exception as exc:
            print(
                f"[tidal] get_max_quality: subscription fetch raised: {exc!r}",
                file=_sys.stderr,
                flush=True,
            )
            return None
        # Tidal exposes the ceiling under `highestSoundQuality`. Some
        # older/newer responses nest it under `subscription.type` —
        # check both and map to our internal code.
        hsq = (
            data.get("highestSoundQuality")
            or (data.get("subscription") or {}).get("highestSoundQuality")
            or (data.get("subscription") or {}).get("type")
            or ""
        )
        mapped = _SUB_QUALITY_MAP.get(str(hsq).upper())
        # Cap at the client's ceiling. A device-code session with a Max
        # subscription still can't stream hi-res — the client_id is the
        # gate, not the subscription. Clamp to _DEVICE_CODE_MAX in that
        # case so the UI never offers Max to a session that can't
        # deliver it.
        is_pkce = bool(getattr(self.session, "is_pkce", False))
        capped = mapped
        if mapped and not is_pkce:
            try:
                if (
                    _SUB_QUALITY_ORDER.index(mapped)
                    > _SUB_QUALITY_ORDER.index(_DEVICE_CODE_MAX)
                ):
                    capped = _DEVICE_CODE_MAX
            except ValueError:
                pass
        print(
            f"[tidal] get_max_quality: raw={hsq!r} mapped={mapped!r} "
            f"is_pkce={is_pkce} capped={capped!r} "
            f"response_keys={sorted(data.keys())}",
            file=_sys.stderr,
            flush=True,
        )
        mapped = capped
        if not mapped:
            return None
        with self._sub_lock:
            self._sub_cache = (now, mapped)
        return mapped

    def clamp_quality_to_subscription(
        self, requested: Optional[str]
    ) -> Optional[str]:
        """Downgrade `requested` to the highest tier the account can
        actually stream. Callers shove this in front of any code path
        that sets `session.config.quality` before a stream / download
        fetch — without it, picking e.g. "hi_res_lossless" on a HiFi
        account generates inevitable 401s from Tidal's playbackinfo
        endpoint. When the subscription lookup fails (network / stale
        token), pass the value through unchanged — better a 401 than
        silently downgrading the user for a transient lookup miss.
        """
        if not requested:
            return requested
        max_q = self.get_max_quality()
        if not max_q:
            return requested
        try:
            req_idx = _SUB_QUALITY_ORDER.index(requested)
            max_idx = _SUB_QUALITY_ORDER.index(max_q)
        except ValueError:
            return requested
        if req_idx <= max_idx:
            return requested
        return max_q

    def save_session(self):
        """Persist the session atomically and with 0600 perms.

        A crash between truncating the file and json.dump finishing would
        otherwise leave a zero-byte `tidal_session.json` and silently log
        the user out. We also chmod the file to user-only (the access +
        refresh tokens sit in plaintext) — otherwise on a shared Unix box
        every other user can read them.
        """
        expiry = self.session.expiry_time
        data = {
            "token_type": self.session.token_type,
            "access_token": self.session.access_token,
            "refresh_token": self.session.refresh_token,
            "expiry_time": expiry.isoformat() if expiry else None,
            # Track PKCE sessions separately — their client_id/secret
            # need to be re-swapped to the hi-res-entitled pair after
            # load_oauth_session, otherwise subsequent stream requests
            # silently drop back to Lossless even if the tokens are
            # valid.
            "is_pkce": bool(getattr(self.session, "is_pkce", False)),
        }
        target = SESSION_FILE
        # Write to a sibling tempfile in the same dir so os.replace stays
        # atomic (rename across filesystems isn't).
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".tidal_session.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(data, f)
            try:
                os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                # chmod is best-effort on Windows / exotic filesystems.
                pass
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def logout(self):
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
        config = tidalapi.Config(quality=tidalapi.Quality.high_lossless)
        self.session = tidalapi.Session(config)
        # The subscription-tier cache belongs to the previous session.
        # Bust it so the next login re-detects under the new client_id.
        with self._sub_lock:
            self._sub_cache = (0.0, None)

    # ------------------------------------------------------------------
    # OAuth login
    # ------------------------------------------------------------------

    def start_oauth_login(self) -> Tuple[str, str, Future]:
        """Initiate device-code OAuth. Returns (url, user_code, future).
        The future resolves when the user completes browser auth."""
        login, future = self.session.login_oauth()
        self._login_future = future
        url = f"https://{login.verification_uri_complete}"
        return url, login.user_code, future

    def complete_login(self, future: Future, timeout: int = 300) -> bool:
        """Block until the OAuth future resolves, then save session. Returns success."""
        try:
            future.result(timeout=timeout)
            if self.session.check_login():
                self.save_session()
                return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # PKCE login — required for hi-res (Max) downloads.
    #
    # Tidal caps the device-code OAuth `client_id` at Lossless regardless
    # of subscription. Only the PKCE flow (which uses a different
    # `client_id_pkce` baked into Tidal's own mobile app) is entitled to
    # stream hi-res FLAC. The tradeoff is UX: there's no device-code
    # handoff — the user logs in in a browser, gets redirected to an
    # "Oops" page (Tidal has no handler for our redirect), copies the
    # URL, and pastes it back to us.
    # ------------------------------------------------------------------

    def pkce_login_url(self) -> str:
        """URL the user should open in a browser to begin PKCE login."""
        return self.session.pkce_login_url()

    def complete_pkce_login(self, redirect_url: str) -> bool:
        """Exchange the pasted 'Oops' redirect URL for access tokens,
        enable the hi-res client_id, and persist the session. Returns
        True on success.
        """
        try:
            token = self.session.pkce_get_auth_token(redirect_url)
            self.session.process_auth_token(token, is_pkce_token=True)
            # Defensive: make sure is_pkce actually sticks on the session
            # so save_session() writes is_pkce=True. If tidalapi's
            # process_auth_token ever stops setting this attribute, the
            # reloaded session would silently fall back to the non-hi-res
            # client_id on next restart.
            self.session.is_pkce = True
            # Swap the active client_id/secret to the hi-res-entitled
            # PKCE pair so subsequent API calls use the credentials that
            # actually unlock Max quality streams.
            self.session.client_enable_hires()
            if self.session.check_login():
                self.save_session()
                # Bust the subscription cache — it may have been
                # populated earlier under a device-code session that
                # reported a Lossless ceiling; the PKCE session
                # should re-detect at Max.
                with self._sub_lock:
                    self._sub_cache = (0.0, None)
                return True
        except Exception as exc:
            import sys as _sys
            print(
                f"[tidal] complete_pkce_login failed: {exc!r}",
                file=_sys.stderr,
                flush=True,
            )
        return False

    # ------------------------------------------------------------------
    # User info
    # ------------------------------------------------------------------

    def get_user_info(self) -> Optional[str]:
        try:
            user = self.session.user
            for attr in ("username", "first_name", "email"):
                value = getattr(user, attr, None)
                if value:
                    return str(value)
            return "Tidal User"
        except Exception:
            return None

    def get_user_avatar_url(self) -> Optional[str]:
        """Tidal exposes a per-user `picture_id` on FetchedUser; most
        accounts don't set one. Returns a CDN URL (210×210) if available,
        or None — callers should fall back to initials.
        """
        try:
            user = self.session.user
            img = getattr(user, "image", None)
            if callable(img):
                return img(210)
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # URL parsing and content fetching
    # ------------------------------------------------------------------

    def parse_url(self, url: str) -> Tuple[str, str]:
        """Return (content_type, id_string) for a Tidal URL."""
        patterns = {
            "track": r"/track/(\d+)",
            "album": r"/album/(\d+)",
            "playlist": r"/playlist/([\w\-]+)",
        }
        for content_type, pattern in patterns.items():
            m = re.search(pattern, url)
            if m:
                return content_type, m.group(1)
        raise ValueError(f"Unrecognized Tidal URL: {url}")

    def fetch_url(self, url: str):
        """Fetch track, album, or playlist object from a Tidal URL.
        Returns (content_type, object)."""
        content_type, content_id = self.parse_url(url)
        if content_type == "track":
            return content_type, self.session.track(int(content_id))
        if content_type == "album":
            return content_type, self.session.album(int(content_id))
        if content_type == "playlist":
            return content_type, self.session.playlist(content_id)
        raise ValueError(f"Unsupported content type: {content_type}")

    def get_track_url(self, track) -> Optional[str]:
        try:
            return track.get_url()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 25) -> dict:
        """Returns dict with keys: tracks, albums, artists, playlists."""
        models = [tidalapi.Track, tidalapi.Album, tidalapi.Artist, tidalapi.Playlist]
        return self.session.search(query, models=models, limit=limit)

    # ------------------------------------------------------------------
    # Library / favorites
    # ------------------------------------------------------------------

    def _favorites_sorted(self, method_name: str) -> list:
        """Fetch every favorite of the given type, paging through the API if
        necessary, then sort newest-first."""
        try:
            method = getattr(self.session.user.favorites, method_name)
        except Exception:
            return []

        items = _fetch_all_pages(method)
        return _sort_by_date_added(items)

    def get_favorite_artists(self) -> list:
        return self._favorites_sorted("artists")

    def get_favorite_tracks(self) -> list:
        return self._favorites_sorted("tracks")

    def get_favorite_albums(self) -> list:
        return self._favorites_sorted("albums")

    def get_favorite_playlists(self) -> list:
        return self._favorites_sorted("playlists")

    def get_user_playlists(self) -> list:
        items = _fetch_all_pages(self.session.user.playlists)
        return _sort_by_date_added(items)

    def get_artist_albums(self, artist) -> list:
        try:
            return list(artist.get_albums())
        except Exception:
            return []

    def get_artist_releases(self, artist, limit: int = 30) -> list:
        """Albums + EPs + singles for an artist, combined. `get_albums()`
        alone excludes singles, which is exactly the content the feed
        page needs (singles drop more frequently than albums). Results
        are deduped by id, sorted nothing — the caller sorts by date."""
        out: list = []
        seen: set = set()
        for fn in (
            lambda: artist.get_albums(limit=limit),
            lambda: artist.get_ep_singles(limit=limit),
        ):
            try:
                for item in fn():
                    key = str(getattr(item, "id", "") or "")
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    out.append(item)
            except Exception:
                continue
        return out

    def get_artist_top_tracks(self, artist) -> list:
        try:
            return list(artist.get_top_tracks(limit=10))
        except Exception:
            return []

    def get_album_tracks(self, album) -> list:
        try:
            return list(album.tracks())
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Writes (favorites + playlists)
    # ------------------------------------------------------------------

    @property
    def user_id(self) -> Optional[str]:
        try:
            uid = getattr(self.session.user, "id", None)
            return str(uid) if uid is not None else None
        except Exception:
            return None

    def favorite(self, kind: str, obj_id: str, add: bool) -> None:
        """Add or remove a favorite. `kind` in {track, album, artist, playlist}."""
        favs = self.session.user.favorites
        method_name = ("add_" if add else "remove_") + kind
        method = getattr(favs, method_name, None)
        if method is None:
            raise ValueError(f"Unsupported favorite kind: {kind}")
        # tidalapi: playlist uses uuid (str), others use int.
        arg: object = obj_id if kind == "playlist" else int(obj_id)
        method(arg)

    def favorites_snapshot(self) -> dict:
        """Return sets of favorite IDs the UI needs to render heart states."""
        result = {"tracks": [], "albums": [], "artists": [], "playlists": []}
        for kind, attr in (
            ("tracks", "get_favorite_tracks"),
            ("albums", "get_favorite_albums"),
            ("artists", "get_favorite_artists"),
            ("playlists", "get_favorite_playlists"),
        ):
            try:
                items = getattr(self, attr)()
                result[kind] = [
                    str(getattr(i, "id", "") or getattr(i, "uuid", "")) for i in items
                ]
            except Exception:
                continue
        return result

    def create_playlist(self, title: str, description: str = ""):
        return self.session.user.create_playlist(title, description)

    def owns_playlist(self, playlist) -> bool:
        try:
            creator = getattr(playlist, "creator", None)
            if creator is None:
                return False
            return str(getattr(creator, "id", "")) == (self.user_id or "")
        except Exception:
            return False
