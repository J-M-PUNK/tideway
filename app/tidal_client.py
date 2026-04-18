import json
import re
import threading
import webbrowser
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import tidalapi

SESSION_FILE = Path("tidal_session.json")


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
        collected.extend(page)
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


class TidalClient:
    def __init__(self):
        config = tidalapi.Config(quality=tidalapi.Quality.high_lossless)
        self.session = tidalapi.Session(config)
        self._login_future: Optional[Future] = None

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
            self.session.load_oauth_session(
                data["token_type"],
                data["access_token"],
                data.get("refresh_token"),
                expiry,
            )
            return self.session.check_login()
        except Exception:
            return False

    def save_session(self):
        expiry = self.session.expiry_time
        data = {
            "token_type": self.session.token_type,
            "access_token": self.session.access_token,
            "refresh_token": self.session.refresh_token,
            "expiry_time": expiry.isoformat() if expiry else None,
        }
        with open(SESSION_FILE, "w") as f:
            json.dump(data, f)

    def logout(self):
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
        config = tidalapi.Config(quality=tidalapi.Quality.high_lossless)
        self.session = tidalapi.Session(config)

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
