"""FastAPI backend for the Tidal Downloader web UI.

Wraps the existing `app/` package (TidalClient, Downloader, Settings) and
exposes it over HTTP + SSE so a React frontend can drive it.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlparse

import tidalapi
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app.downloader import DownloadItem, DownloadStatus, Downloader
from app.http import SESSION
from app.local_index import LocalIndex
from app.settings import Settings, load_settings, save_settings
from app.tidal_client import TidalClient


tidal = TidalClient()
settings: Settings = load_settings()
# Guards the `settings` rebind + downloader.settings swap so workers never
# see a torn state (new global, old downloader field or vice versa).
_settings_lock = threading.Lock()
tidal.load_session()

_oauth_lock = threading.Lock()
_oauth_state: dict[str, Any] = {"url": None, "user_code": None, "future": None}

# Hosts we're willing to proxy images from. Keep tight to avoid turning the
# proxy into a general-purpose SSRF primitive.
ALLOWED_IMAGE_HOSTS = {
    "resources.tidal.com",
    "images.tidal.com",
}

# check_login() hits Tidal over the network. Cache the result briefly so a
# page load that fires a dozen authed requests doesn't fan out to a dozen
# round-trips (and risk rate-limiting).
_AUTH_CACHE_TTL = 30.0
_auth_cache: dict[str, Any] = {"at": 0.0, "ok": False}
_auth_cache_lock = threading.Lock()

# Tidal stream URLs are signed and valid for several minutes. Cache the
# resolved preview URL per track so browser seek/reload doesn't re-hit the
# API on every range request.
_PREVIEW_CACHE_TTL = 120.0
_preview_cache: dict[int, tuple[float, str]] = {}
_preview_cache_lock = threading.Lock()


def _is_logged_in() -> bool:
    import time

    now = time.monotonic()
    with _auth_cache_lock:
        if now - _auth_cache["at"] < _AUTH_CACHE_TTL:
            return bool(_auth_cache["ok"])
    try:
        ok = bool(tidal.session.check_login())
    except Exception:
        ok = False
    with _auth_cache_lock:
        _auth_cache["at"] = now
        _auth_cache["ok"] = ok
    return ok


def _invalidate_auth_cache() -> None:
    with _auth_cache_lock:
        _auth_cache["at"] = 0.0
        _auth_cache["ok"] = False


def _invalidate_preview_cache() -> None:
    with _preview_cache_lock:
        _preview_cache.clear()


# ---------------------------------------------------------------------------
# Download broker — bridges thread-based Downloader callbacks to SSE clients
# ---------------------------------------------------------------------------


# Cap per-subscriber queue size so a disconnected/slow client can't balloon
# memory with every download-progress event. Worst case: we drop an oldest
# `item` event for that subscriber — those are idempotent snapshots and
# the next emission for the same track resyncs the UI.
_SUBSCRIBER_QUEUE_MAXSIZE = 256


def _drop_one_item_event(q: asyncio.Queue) -> bool:
    """Scan the queue and pull out one idempotent `item` event, preserving
    ordering of everything else. Returns True if one was dropped.
    """
    # Drain all, keep non-item ones, requeue in order, signal drop of first item.
    dropped = False
    saved: list = []
    while True:
        try:
            evt = q.get_nowait()
        except Exception:
            break
        if not dropped and isinstance(evt, dict) and evt.get("type") == "item":
            dropped = True
            continue
        saved.append(evt)
    for evt in saved:
        try:
            q.put_nowait(evt)
        except Exception:
            break
    return dropped


class DownloadBroker:
    def __init__(self) -> None:
        self._items: dict[str, DownloadItem] = {}
        self._items_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subs: set[asyncio.Queue] = set()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def snapshot(self) -> list[DownloadItem]:
        with self._items_lock:
            return list(self._items.values())

    def get(self, item_id: str) -> Optional[DownloadItem]:
        with self._items_lock:
            return self._items.get(item_id)

    async def subscribe(self) -> asyncio.Queue:
        # Build the snapshot BEFORE registering the queue so a concurrent
        # publish can't interleave a live delta between snapshot events.
        q: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        snapshot = self.snapshot()
        for item in snapshot:
            await q.put({"type": "item", "item": item_to_dict(item)})
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _publish(self, payload: dict) -> None:
        if not self._loop:
            return
        # "remove" and "downloaded" events aren't idempotent — dropping one
        # leaves the UI with a ghost row or a missing Saved badge forever.
        # `item` events are snapshots so losing one is harmless.
        idempotent = payload.get("type") == "item"

        def dispatch() -> None:
            for q in list(self._subs):
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    # Slow consumer. We drop an *old* item event to free
                    # space — never a remove/downloaded. If the only events
                    # in the queue are non-idempotent ones, we'd rather
                    # drop the NEW item event than lose state.
                    if not _drop_one_item_event(q):
                        if not idempotent:
                            # Best effort: try once more, may still fail.
                            try:
                                q.put_nowait(payload)
                            except Exception:
                                pass
                        # Else: new event is also just an item; swallow.
                        continue
                    try:
                        q.put_nowait(payload)
                    except Exception:
                        pass
                except Exception:
                    pass

        self._loop.call_soon_threadsafe(dispatch)

    def on_add(self, item: DownloadItem) -> None:
        with self._items_lock:
            self._items[item.item_id] = item
        self._publish({"type": "item", "item": item_to_dict(item)})

    def on_update(self, item: DownloadItem) -> None:
        with self._items_lock:
            self._items[item.item_id] = item
        self._publish({"type": "item", "item": item_to_dict(item)})

    def on_remove(self, item_id: str) -> None:
        with self._items_lock:
            self._items.pop(item_id, None)
        self._publish({"type": "remove", "id": item_id})

    def clear_completed(self) -> None:
        terminal = {DownloadStatus.COMPLETE, DownloadStatus.FAILED}
        with self._items_lock:
            to_remove = [i for i, it in self._items.items() if it.status in terminal]
            for i in to_remove:
                self._items.pop(i, None)
            remaining = list(self._items.values())
        self._publish({"type": "reset", "items": [item_to_dict(i) for i in remaining]})


broker = DownloadBroker()
local_index = LocalIndex()


def _on_file_ready(track_id: str, path: Path) -> None:
    local_index.add(track_id, path)
    # Push a live event so open clients can flip the "downloaded" dot on
    # every row for this track ID without polling.
    broker._publish({"type": "downloaded", "track_id": track_id})


downloader = Downloader(
    tidal,
    settings,
    broker.on_add,
    broker.on_update,
    on_remove=broker.on_remove,
    on_file_ready=_on_file_ready,
)


def _apply_settings_quality(s: Settings) -> None:
    """Align the shared tidalapi session with the user's default quality.

    Downloads that don't pass an explicit per-item quality fall back to
    session.config.quality, so this is how Settings actually takes effect.
    """
    try:
        with downloader.quality_lock:
            tidal.session.config.quality = tidalapi.Quality[s.quality]
    except (KeyError, AttributeError):
        # Invalid saved quality — leave session at its default.
        pass


_apply_settings_quality(settings)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    broker.bind_loop(asyncio.get_running_loop())
    local_index.start_scan(Path(settings.output_dir).expanduser())
    try:
        yield
    finally:
        # Close the shared requests session so sockets in its connection pool
        # are released cleanly on reload/shutdown.
        try:
            SESSION.close()
        except Exception:
            pass


app = FastAPI(title="Tidal Downloader", lifespan=lifespan)

# Localhost-only tool: restrict CORS to the Vite dev server origin and list
# only the methods/headers we actually use. In production (single-origin
# serving from FastAPI) this middleware is effectively a no-op.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
    allow_credentials=True,
)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def item_to_dict(item: DownloadItem) -> dict:
    return {
        "id": item.item_id,
        "title": item.title,
        "artist": item.artist,
        "album": item.album,
        "track_num": item.track_num,
        "status": item.status.value,
        "progress": item.progress,
        "error": item.error,
        "file_path": item.file_path,
    }


def _first(fn):
    try:
        return fn()
    except Exception:
        return None


def _image_url(obj, size: int = 320) -> Optional[str]:
    for candidate in (size, 640, 320, 160, 750, 1080):
        try:
            url = obj.image(candidate)
            if url:
                return url
        except Exception:
            continue
    try:
        pic = getattr(obj, "picture", None)
        if pic:
            return f"https://resources.tidal.com/images/{pic.replace('-', '/')}/{size}x{size}.jpg"
    except Exception:
        pass
    return None


def _artists(obj) -> list[dict]:
    out: list[dict] = []
    try:
        for a in obj.artists or []:
            out.append({"id": str(a.id), "name": a.name})
    except Exception:
        pass
    if not out:
        try:
            a = obj.artist
            if a is not None:
                out.append({"id": str(a.id), "name": a.name})
        except Exception:
            pass
    return out


def track_to_dict(t) -> dict:
    album = _first(lambda: t.album)
    return {
        "kind": "track",
        "id": str(t.id),
        "name": t.name,
        "duration": _first(lambda: t.duration) or 0,
        "track_num": _first(lambda: t.track_num) or 0,
        "explicit": bool(_first(lambda: t.explicit)),
        "artists": _artists(t),
        "album": {
            "id": str(album.id),
            "name": album.name,
            "cover": _image_url(album, 320),
        } if album else None,
    }


def album_to_dict(a) -> dict:
    return {
        "kind": "album",
        "id": str(a.id),
        "name": a.name,
        "num_tracks": _first(lambda: a.num_tracks) or 0,
        "year": _first(lambda: a.year),
        "duration": _first(lambda: a.duration) or 0,
        "cover": _image_url(a, 640),
        "artists": _artists(a),
        "explicit": bool(_first(lambda: a.explicit)),
    }


def artist_to_dict(a) -> dict:
    return {
        "kind": "artist",
        "id": str(a.id),
        "name": a.name,
        "picture": _image_url(a, 750),
    }


def playlist_to_dict(p) -> dict:
    creator_name = _first(lambda: p.creator.name) if _first(lambda: p.creator) else None
    return {
        "kind": "playlist",
        "id": str(p.id),
        "name": p.name,
        "description": _first(lambda: p.description) or "",
        "num_tracks": _first(lambda: p.num_tracks) or 0,
        "duration": _first(lambda: p.duration) or 0,
        "cover": _image_url(p, 750),
        "creator": creator_name,
        "owned": tidal.owns_playlist(p),
    }


def _require_auth() -> None:
    if not _is_logged_in():
        raise HTTPException(status_code=401, detail="Not authenticated")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@app.get("/api/auth/status")
def auth_status() -> dict:
    logged_in = _is_logged_in()
    return {
        "logged_in": logged_in,
        "username": tidal.get_user_info() if logged_in else None,
    }


@app.post("/api/auth/login/start")
def auth_login_start() -> dict:
    with _oauth_lock:
        existing = _oauth_state.get("future")
        if existing is not None and not existing.done():
            return {
                "url": _oauth_state["url"],
                "user_code": _oauth_state["user_code"],
            }
        url, user_code, future = tidal.start_oauth_login()
        _oauth_state.update(url=url, user_code=user_code, future=future)

    def _wait_and_save() -> None:
        try:
            ok = tidal.complete_login(future)
        except Exception:
            ok = False
        if ok:
            # If the user never polls, we still need to flush any stale
            # session-bound caches or the next preview/auth hit will use
            # data from the prior session.
            _invalidate_auth_cache()
            _invalidate_preview_cache()
            _apply_settings_quality(settings)

    threading.Thread(target=_wait_and_save, daemon=True).start()
    return {"url": url, "user_code": user_code}


@app.get("/api/auth/login/poll")
def auth_login_poll() -> dict:
    with _oauth_lock:
        future = _oauth_state.get("future")
    if future is None:
        return {"status": "idle"}
    if not future.done():
        return {"status": "pending"}
    try:
        logged_in = tidal.session.check_login()
    except Exception:
        logged_in = False
    with _oauth_lock:
        _oauth_state.update(url=None, user_code=None, future=None)
    _invalidate_auth_cache()
    if logged_in:
        # New login may be a different user / refreshed tokens; old signed
        # preview URLs are no longer trustworthy.
        _invalidate_preview_cache()
        # Reapply saved quality to the new session (logout creates a fresh
        # session with the hardcoded default).
        _apply_settings_quality(settings)
        return {"status": "ok", "username": tidal.get_user_info()}
    return {"status": "failed"}


@app.post("/api/auth/logout")
def auth_logout() -> dict:
    # Order matters: tear down the session, then invalidate every cache that
    # could still vend data tied to it.
    tidal.logout()
    _invalidate_auth_cache()
    _invalidate_preview_cache()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@app.get("/api/search")
def search(q: str, limit: int = 25) -> dict:
    _require_auth()
    if not q.strip():
        return {"tracks": [], "albums": [], "artists": [], "playlists": []}
    limit = max(1, min(limit, 100))
    try:
        results = tidal.search(q, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Search failed: {exc}")
    return {
        "tracks": [track_to_dict(t) for t in results.get("tracks", [])],
        "albums": [album_to_dict(a) for a in results.get("albums", [])],
        "artists": [artist_to_dict(a) for a in results.get("artists", [])],
        "playlists": [playlist_to_dict(p) for p in results.get("playlists", [])],
    }


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------


@app.get("/api/library/tracks")
def library_tracks() -> list[dict]:
    _require_auth()
    return [track_to_dict(t) for t in tidal.get_favorite_tracks()]


@app.get("/api/library/albums")
def library_albums() -> list[dict]:
    _require_auth()
    return [album_to_dict(a) for a in tidal.get_favorite_albums()]


@app.get("/api/library/artists")
def library_artists() -> list[dict]:
    _require_auth()
    return [artist_to_dict(a) for a in tidal.get_favorite_artists()]


@app.get("/api/library/playlists")
def library_playlists() -> list[dict]:
    _require_auth()
    faves = tidal.get_favorite_playlists()
    mine = tidal.get_user_playlists()
    seen: set[str] = set()
    out: list[dict] = []
    for p in list(mine) + list(faves):
        pid = str(getattr(p, "id", "") or "")
        if pid and pid not in seen:
            seen.add(pid)
            out.append(playlist_to_dict(p))
    return out


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@app.get("/api/album/{album_id}")
def album_detail(album_id: int) -> dict:
    _require_auth()
    try:
        album = tidal.session.album(album_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    # Similar is best-effort; fails for some albums and we don't want that
    # to 500 the whole page.
    try:
        similar = [album_to_dict(a) for a in album.similar()][:12]
    except Exception:
        similar = []
    return {
        **album_to_dict(album),
        "tracks": [track_to_dict(t) for t in tidal.get_album_tracks(album)],
        "similar": similar,
    }


@app.get("/api/artist/{artist_id}")
def artist_detail(artist_id: int) -> dict:
    _require_auth()
    try:
        artist = tidal.session.artist(artist_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    # Bio + similar are best-effort; they fail for some artists and we'd
    # rather render the page without them than 500 the whole request.
    try:
        bio = artist.get_bio()
    except Exception:
        bio = None
    try:
        similar = [artist_to_dict(a) for a in artist.get_similar()][:12]
    except Exception:
        similar = []
    return {
        **artist_to_dict(artist),
        "top_tracks": [track_to_dict(t) for t in tidal.get_artist_top_tracks(artist)],
        "albums": [album_to_dict(a) for a in tidal.get_artist_albums(artist)],
        "bio": bio,
        "similar": similar,
    }


@app.get("/api/track/{track_id}/lyrics")
def track_lyrics(track_id: int) -> dict:
    """Return lyrics for a track if Tidal has them.

    Shape: {
      "synced": [{"time": 12.3, "text": "..."}]?,  // if time-coded available
      "text": "..."?,                              // plain text fallback
    }
    """
    _require_auth()
    try:
        track = tidal.session.track(track_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        lyrics = track.lyrics()
    except Exception:
        return {"synced": None, "text": None}

    text = getattr(lyrics, "text", None) or None
    subtitles = getattr(lyrics, "subtitles", None)
    synced: Optional[list[dict]] = None
    if subtitles:
        # tidalapi exposes subtitles as an LRC-like string with [mm:ss.xx] cues.
        synced = _parse_lrc(subtitles)
    return {"synced": synced, "text": text}


def _parse_lrc(raw: str) -> list[dict]:
    import re

    out: list[dict] = []
    for line in raw.splitlines():
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)", line)
        if not m:
            continue
        minutes, seconds, text = m.groups()
        t = int(minutes) * 60 + float(seconds)
        stripped = text.strip()
        if stripped:
            out.append({"time": t, "text": stripped})
    return out


@app.get("/api/track/{track_id}/radio")
def track_radio(track_id: int) -> list[dict]:
    """Tracks similar to this one — Tidal's 'Track Radio' seed expansion."""
    _require_auth()
    try:
        track = tidal.session.track(track_id)
        radio = track.get_track_radio()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return [track_to_dict(t) for t in radio]


@app.get("/api/mix/{mix_id}")
def mix_detail(mix_id: str) -> dict:
    """Return a Tidal mix (playlist-like collection) with its tracks."""
    _require_auth()
    try:
        mix = tidal.session.mix(mix_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        items = list(mix.items())
    except Exception:
        items = []
    tracks = [track_to_dict(t) for t in items if type(t).__name__ == "Track"]
    return {
        "kind": "mix",
        "id": mix_id,
        "name": getattr(mix, "title", None) or "",
        "subtitle": getattr(mix, "sub_title", None) or "",
        "cover": _first(lambda: mix.image(640)) or _first(lambda: mix.image(480)),
        "tracks": tracks,
    }


@app.get("/api/playlist/{playlist_id}")
def playlist_detail(playlist_id: str) -> dict:
    _require_auth()
    try:
        playlist = tidal.session.playlist(playlist_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        tracks = list(playlist.tracks())
    except Exception:
        tracks = []
    return {
        **playlist_to_dict(playlist),
        "tracks": [track_to_dict(t) for t in tracks],
    }


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


class DownloadRequest(BaseModel):
    kind: str  # track | album | playlist
    id: str
    quality: Optional[str] = None  # tidalapi Quality enum name, e.g. "high_lossless"


class UrlDownloadRequest(BaseModel):
    url: str
    quality: Optional[str] = None


@app.post("/api/downloads/url")
def enqueue_from_url(req: UrlDownloadRequest) -> dict:
    _require_auth()
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is empty")
    try:
        tidal.parse_url(url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    downloader.submit(url, quality=req.quality)
    return {"ok": True}


class RevealRequest(BaseModel):
    path: str


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


@app.post("/api/reveal")
def reveal_in_finder(req: RevealRequest) -> dict:
    try:
        target = Path(req.path).expanduser().resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Confine reveals to the configured output directory. Prevents the
    # endpoint from being abused to poke around the user's whole filesystem.
    output_root = Path(settings.output_dir).expanduser().resolve()
    if not _is_within(target, output_root):
        raise HTTPException(status_code=403, detail="Path is outside the downloads folder")

    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)])
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(target.parent)])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            raise HTTPException(status_code=501, detail=f"Unsupported platform: {sys.platform}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True}


def _resolve_preview_url(track_id: int) -> str:
    """Return a signed Tidal AAC-320 stream URL for `track_id`, cached."""
    import time

    now = time.monotonic()
    with _preview_cache_lock:
        cached = _preview_cache.get(track_id)
        if cached and (now - cached[0]) < _PREVIEW_CACHE_TTL:
            return cached[1]

    try:
        track = tidal.session.track(track_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    with downloader.quality_lock:
        original = tidal.session.config.quality
        try:
            tidal.session.config.quality = tidalapi.Quality.low_320k
            url = track.get_url()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        finally:
            tidal.session.config.quality = original

    if not url:
        raise HTTPException(status_code=502, detail="Tidal returned no stream URL")

    with _preview_cache_lock:
        _preview_cache[track_id] = (now, url)
    return url


@app.get("/api/preview/{track_id}")
def preview_stream(track_id: int) -> RedirectResponse:
    """Redirect to a Tidal preview stream (AAC 320) — low-enough bitrate
    for universal browser playback and ignores local files."""
    _require_auth()
    return RedirectResponse(_resolve_preview_url(track_id), status_code=307)


@app.get("/api/play/{track_id}")
def play_track(track_id: int):
    """Unified playback endpoint: serves the local file at full quality if
    we have one for this Tidal track, otherwise falls back to the Tidal
    preview stream. FileResponse emits Range-capable headers so the browser
    can seek without buffering the whole file."""
    _require_auth()
    local = local_index.get(str(track_id))
    if local is not None:
        return FileResponse(str(local))
    return RedirectResponse(_resolve_preview_url(track_id), status_code=307)


@app.get("/api/downloaded")
def downloaded_ids() -> dict:
    """Return the set of Tidal track IDs the local index knows about.

    Frontend calls this once on boot and then updates live via the
    `downloaded` SSE event type.
    """
    return {"ids": sorted(local_index.ids())}


def _resolve_quality(req_quality: Optional[str]) -> Optional[str]:
    """Resolve the effective per-item quality.

    Explicit request wins. Otherwise fall back to settings.quality, so the
    user's Settings choice actually matters when they pick "Use default".
    Returning None means "use whatever the session has" (safety net).
    """
    if req_quality:
        return req_quality
    fallback = getattr(settings, "quality", None)
    if fallback and fallback in tidalapi.Quality.__members__:
        return fallback
    return None


@app.post("/api/downloads")
def enqueue_download(req: DownloadRequest) -> dict:
    _require_auth()
    try:
        if req.kind == "track":
            obj = tidal.session.track(int(req.id))
        elif req.kind == "album":
            obj = tidal.session.album(int(req.id))
        elif req.kind == "playlist":
            obj = tidal.session.playlist(req.id)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported kind: {req.kind}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    downloader.submit_object(obj, req.kind, quality=_resolve_quality(req.quality))
    return {"ok": True}


@app.get("/api/downloads")
def list_downloads() -> list[dict]:
    return [item_to_dict(i) for i in broker.snapshot()]


@app.post("/api/downloads/{item_id}/retry")
def retry_download(item_id: str) -> dict:
    item = broker.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    downloader.retry(item)
    return {"ok": True}


@app.delete("/api/downloads/completed")
def clear_completed() -> dict:
    broker.clear_completed()
    return {"ok": True}


@app.get("/api/downloads/stream")
async def downloads_stream(request: Request) -> EventSourceResponse:
    q = await broker.subscribe()

    async def event_gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                    yield {"event": "download", "data": json.dumps(payload)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "1"}
        finally:
            broker.unsubscribe(q)

    return EventSourceResponse(event_gen())


# ---------------------------------------------------------------------------
# Quality catalog — canonical list with bitrate/codec labels for the UI
# ---------------------------------------------------------------------------


QUALITIES = [
    {
        "value": "low_96k",
        "label": "Low",
        "codec": "AAC",
        "bitrate": "~96 kbps",
        "description": "Data-saver streaming quality.",
    },
    {
        "value": "low_320k",
        "label": "High",
        "codec": "AAC",
        "bitrate": "~320 kbps",
        "description": "High-quality lossy streaming.",
    },
    {
        "value": "high_lossless",
        "label": "Lossless",
        "codec": "FLAC",
        "bitrate": "~1411 kbps (16-bit / 44.1 kHz)",
        "description": "CD-quality lossless.",
    },
    {
        "value": "hi_res_lossless",
        "label": "Max",
        "codec": "FLAC",
        "bitrate": "up to 9216 kbps (24-bit / 192 kHz)",
        "description": "Hi-res lossless — requires a MAX subscription.",
    },
]


@app.get("/api/qualities")
def list_qualities() -> list[dict]:
    return QUALITIES


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class SettingsPayload(BaseModel):
    output_dir: Optional[str] = None
    quality: Optional[str] = None
    filename_template: Optional[str] = None
    create_album_folders: Optional[bool] = None
    skip_existing: Optional[bool] = None


@app.get("/api/settings")
def get_settings() -> dict:
    return asdict(settings)


@app.put("/api/settings")
def update_settings(payload: SettingsPayload) -> dict:
    global settings
    patch = payload.model_dump(exclude_unset=True)

    # Validate quality before writing: an unknown value would silently
    # disable per-item quality resolution later.
    if "quality" in patch and patch["quality"] not in tidalapi.Quality.__members__:
        raise HTTPException(status_code=400, detail=f"Unknown quality: {patch['quality']}")

    with _settings_lock:
        data = asdict(settings)
        data.update(patch)
        new_settings = Settings(**data)
        save_settings(new_settings)
        settings = new_settings
        downloader.settings = new_settings
    # Apply quality outside the settings lock — _apply_settings_quality
    # takes the downloader.quality_lock, which a worker may currently hold.
    # Holding both in a different order than workers would risk deadlock.
    _apply_settings_quality(new_settings)
    return asdict(new_settings)


# ---------------------------------------------------------------------------
# Editorial pages (home, explore, genres, moods, drill-downs)
#
# Tidal's apps fetch these same pages from the API that tidalapi wraps. Each
# Page contains a flat list of "categories" (rows), each of which has a type
# (horizontal list, track list, shortcut list, page links, etc.) and items.
# ---------------------------------------------------------------------------


def _serialize_page_item(item) -> Optional[dict]:
    """Turn one item in a page row into a JSON-friendly dict.

    Returns None for types we can't render (videos, text blocks, etc.) so
    the caller can filter them out.
    """
    import tidalapi

    if isinstance(item, tidalapi.Track):
        return track_to_dict(item)
    if isinstance(item, tidalapi.Album):
        return album_to_dict(item)
    if isinstance(item, tidalapi.Artist):
        return artist_to_dict(item)
    if isinstance(item, tidalapi.Playlist):
        return playlist_to_dict(item)
    # Mix — lives in tidalapi.mix.Mix (or MixV2)
    name = type(item).__name__
    if name in ("Mix", "MixV2"):
        try:
            return {
                "kind": "mix",
                "id": str(getattr(item, "id", "") or ""),
                "name": getattr(item, "title", None) or getattr(item, "name", "") or "",
                "subtitle": getattr(item, "sub_title", None) or "",
                "cover": _first(lambda: item.image(640)) or _first(lambda: item.image(480)),
            }
        except Exception:
            return None
    # PageLink — clickable category (genre, mood)
    if name == "PageLink":
        return {
            "kind": "pagelink",
            "title": getattr(item, "title", "") or "",
            "path": getattr(item, "api_path", "") or "",
            "icon": getattr(item, "icon", None) or "",
        }
    return None


def _serialize_page(page) -> dict:
    categories: list[dict] = []
    for cat in getattr(page, "categories", []) or []:
        cat_type = type(cat).__name__
        if cat_type == "TextBlock":
            # Editorial copy — not useful to render in our UI.
            continue
        title = getattr(cat, "title", None) or ""
        raw_items = list(getattr(cat, "items", []) or [])
        items = [d for d in (_serialize_page_item(i) for i in raw_items) if d]
        if not items:
            continue
        categories.append({"type": cat_type, "title": title, "items": items})
    return {"categories": categories}


# Well-known page names the frontend can request directly.
_KNOWN_PAGES = {
    "home": lambda: tidal.session.home(),
    "explore": lambda: tidal.session.explore(),
    "genres": lambda: tidal.session.genres(),
    "moods": lambda: tidal.session.moods(),
    "hires": lambda: tidal.session.hires_page(),
}


@app.get("/api/page/{name}")
def editorial_page(name: str) -> dict:
    _require_auth()
    loader = _KNOWN_PAGES.get(name)
    if loader is None:
        raise HTTPException(status_code=404, detail=f"Unknown page: {name}")
    try:
        page = loader()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return _serialize_page(page)


class PagePathRequest(BaseModel):
    path: str


@app.post("/api/page/resolve")
def resolve_page(req: PagePathRequest) -> dict:
    """Drill into a PageLink by the api_path it returned (e.g.
    ``pages/genre_hip_hop``). POST rather than GET because the path contains
    slashes that get awkward in URL path segments.
    """
    _require_auth()
    path = req.path.strip()
    if not path or not path.startswith("pages/"):
        raise HTTPException(status_code=400, detail="path must start with 'pages/'")
    try:
        page = tidal.session.page.get(path)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return _serialize_page(page)


# ---------------------------------------------------------------------------
# Favorites (like / follow)
# ---------------------------------------------------------------------------


FAVORITE_KINDS = {"track", "album", "artist", "playlist"}


@app.get("/api/favorites")
def favorites_snapshot() -> dict:
    _require_auth()
    return tidal.favorites_snapshot()


@app.post("/api/favorites/{kind}/{obj_id}")
def favorite_add(kind: str, obj_id: str) -> dict:
    _require_auth()
    if kind not in FAVORITE_KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown kind: {kind}")
    try:
        tidal.favorite(kind, obj_id, add=True)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


@app.delete("/api/favorites/{kind}/{obj_id}")
def favorite_remove(kind: str, obj_id: str) -> dict:
    _require_auth()
    if kind not in FAVORITE_KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown kind: {kind}")
    try:
        tidal.favorite(kind, obj_id, add=False)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Playlist CRUD (owner-only for mutations)
# ---------------------------------------------------------------------------


class CreatePlaylistRequest(BaseModel):
    title: str
    description: str = ""


class AddTracksRequest(BaseModel):
    track_ids: list[str]


@app.get("/api/playlists/mine")
def my_playlists() -> list[dict]:
    """Just the user's own playlists — used for the Add-to-Playlist menu."""
    _require_auth()
    return [playlist_to_dict(p) for p in tidal.get_user_playlists()]


@app.post("/api/playlists")
def create_playlist(req: CreatePlaylistRequest) -> dict:
    _require_auth()
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title required")
    try:
        playlist = tidal.create_playlist(title, req.description or "")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return playlist_to_dict(playlist)


def _get_owned_playlist(playlist_id: str):
    try:
        playlist = tidal.session.playlist(playlist_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not tidal.owns_playlist(playlist):
        raise HTTPException(status_code=403, detail="You can only modify your own playlists")
    return playlist


@app.delete("/api/playlists/{playlist_id}")
def delete_playlist(playlist_id: str) -> dict:
    _require_auth()
    playlist = _get_owned_playlist(playlist_id)
    try:
        playlist.delete()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


class EditPlaylistRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None


@app.put("/api/playlists/{playlist_id}")
def edit_playlist(playlist_id: str, req: EditPlaylistRequest) -> dict:
    _require_auth()
    playlist = _get_owned_playlist(playlist_id)
    title = (req.title or "").strip()
    description = req.description
    if not title and description is None:
        raise HTTPException(status_code=400, detail="Nothing to update")
    try:
        # tidalapi's edit requires both args; fall back to existing values.
        playlist.edit(
            title=title or playlist.name,
            description=description if description is not None else (playlist.description or ""),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    # Re-fetch so the response carries the persisted values.
    try:
        fresh = tidal.session.playlist(playlist_id)
        return playlist_to_dict(fresh)
    except Exception:
        return {"ok": True}


@app.post("/api/playlists/{playlist_id}/tracks")
def add_tracks_to_playlist(playlist_id: str, req: AddTracksRequest) -> dict:
    _require_auth()
    playlist = _get_owned_playlist(playlist_id)
    try:
        ids = [int(t) for t in req.track_ids]
    except ValueError:
        raise HTTPException(status_code=400, detail="track_ids must be numeric")
    try:
        playlist.add(ids)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, "added": len(ids)}


@app.delete("/api/playlists/{playlist_id}/tracks/{index}")
def remove_track_from_playlist(playlist_id: str, index: int) -> dict:
    _require_auth()
    playlist = _get_owned_playlist(playlist_id)
    try:
        playlist.remove_by_index(index)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


class MoveTrackRequest(BaseModel):
    media_id: str
    position: int


@app.post("/api/playlists/{playlist_id}/tracks/move")
def move_track_in_playlist(playlist_id: str, req: MoveTrackRequest) -> dict:
    """Reorder a track within a user-owned playlist.

    `media_id` is the Tidal track ID; `position` is the 0-based target index.
    tidalapi's UserPlaylist.move_by_id handles the wire protocol.
    """
    _require_auth()
    playlist = _get_owned_playlist(playlist_id)
    try:
        playlist.move_by_id(req.media_id, req.position)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Image proxy — avoids CORS issues and keeps covers uniform. Restricted to
# known Tidal CDN hosts so the endpoint can't be turned into an SSRF probe
# against arbitrary internal services.
# ---------------------------------------------------------------------------


MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB — covers even oversized Tidal covers


@app.get("/api/image")
def image_proxy(url: str) -> Response:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="Only https URLs allowed")
    if parsed.hostname not in ALLOWED_IMAGE_HOSTS:
        raise HTTPException(status_code=403, detail=f"Host not allowed: {parsed.hostname}")
    try:
        with SESSION.get(url, timeout=10, stream=True) as resp:
            resp.raise_for_status()
            # Stop if the server advertises an oversized payload.
            declared = int(resp.headers.get("Content-Length") or 0)
            if declared and declared > MAX_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="Image too large")
            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=65536):
                buf.extend(chunk)
                if len(buf) > MAX_IMAGE_BYTES:
                    raise HTTPException(status_code=413, detail="Image too large")
            content_type = resp.headers.get("Content-Type", "image/jpeg")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return Response(
        content=bytes(buf),
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            # Explicit CORS header so fast-average-color on the frontend
            # can read pixel data even when the image is cross-origin in dev.
            "Access-Control-Allow-Origin": "*",
        },
    )
