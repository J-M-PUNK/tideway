"""FastAPI backend for the Tidal Downloader web UI.

Wraps the existing `app/` package (TidalClient, Downloader, Settings) and
exposes it over HTTP + SSE so a React frontend can drive it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import webbrowser
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional
from urllib.parse import urlparse

import tidalapi
import tidalapi.page as _tidal_page
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app import player as native_player
from app.downloader import DownloadItem, DownloadStatus, Downloader
from app.http import SESSION
from app.lastfm import LastFmClient
from app.local_index import LocalIndex
from app.paths import bundled_resource_dir
from app.play_reporter import PlayReporter, PlaySession
from app.settings import Settings, load_settings, save_settings
from app.tidal_client import TidalClient


logger = logging.getLogger("tidal-downloader.server")
# Uvicorn doesn't configure our namespace by default; attach to the same
# stderr handler it uses so our warnings/errors actually show up next to
# the access log lines instead of being silently dropped.
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


# Tidal's V2 home feed delivers "Because you liked X" / "Because you
# listened to Y" modules as HORIZONTAL_LIST_WITH_CONTEXT with the
# related album/artist/track nested under `header.data`. tidalapi's
# PageCategoryV2._parse_base only copies title/subtitle/description
# off the raw dict and drops `header`, so our server sees an empty
# subtitle and the UI renders "Because you liked" with nothing after
# it. Patch the base parser to synthesize a subtitle from the header
# when the category didn't ship one explicitly.
_orig_parse_base = _tidal_page.PageCategoryV2._parse_base


def _header_context_label(header: dict) -> Optional[str]:
    data = header.get("data") or {}
    htype = (header.get("type") or "").upper()
    if htype in ("ALBUM", "TRACK", "PLAYLIST", "MIX"):
        title = data.get("title")
        artists = data.get("artists") or []
        artist = artists[0].get("name") if artists and isinstance(artists[0], dict) else None
        if title and artist:
            return f"{title} · {artist}"
        return title
    if htype == "ARTIST":
        return data.get("name")
    return None


def _patched_parse_base(self, list_item):
    _orig_parse_base(self, list_item)
    # Stash the raw header so _serialize_page can build a clickable
    # context badge ("Because you liked X" with X's cover).
    # (viewAll / showMore are already captured by _parse_base into
    # self._more.api_path — no need to copy them ourselves.)
    header = list_item.get("header")
    if isinstance(header, dict):
        self._raw_header = header
        if not self.subtitle:
            label = _header_context_label(header)
            if label:
                self.subtitle = label


_tidal_page.PageCategoryV2._parse_base = _patched_parse_base


tidal = TidalClient()
lastfm = LastFmClient()
play_reporter = PlayReporter(tidal)
settings: Settings = load_settings()
# Guards the `settings` rebind + downloader.settings swap so workers never
# see a torn state (new global, old downloader field or vice versa).
_settings_lock = threading.Lock()
tidal.load_session()

# Shared single-worker pool for bulk endpoints. Keeping it at max_workers=1
# serializes Tidal RPCs across all bulk requests — tidalapi isn't
# documented thread-safe for concurrent token refresh, and sequentially
# running a batch is what the UI expects anyway. Using a pool (rather
# than spawning a fresh thread per request) also bounds the total
# concurrent work a client can trigger: a second bulk call is queued
# behind the first instead of racing it.
_BULK_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bulk")

_oauth_lock = threading.Lock()
_oauth_state: dict[str, Any] = {"url": None, "user_code": None, "future": None}

# Hosts we're willing to proxy images from. Keep tight to avoid turning the
# proxy into a general-purpose SSRF primitive. Last.fm CDN hosts are here
# because artist/album/user avatars from `user.getRecentTracks` and the
# stats/popular endpoints come from Fastly/Akamai, not Tidal.
ALLOWED_IMAGE_HOSTS = {
    "resources.tidal.com",
    "images.tidal.com",
    "lastfm.freetls.fastly.net",
    "lastfm-img2.akamaized.net",
    "lastfm.akamaized.net",
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
_preview_cache: dict[tuple[int, str], tuple[float, str]] = {}
_preview_cache_lock = threading.Lock()

# Multi-segment DASH tracks get buffered to a temp file so Range/seek work
# and the scrub bar tracks duration. Cache the buffered file per
# (track_id, quality) so scrub-seeks (which fire fresh Range requests)
# don't re-download every segment, and replaying the same track within a
# few minutes is instant. TTL is long enough to cover a full track play
# plus some idle time.
_STREAM_FILE_CACHE_TTL = 600.0
# (ts, path, mime) — mime is stored so cache-hit path doesn't need the
# manifest's ext hint to pick Content-Type.
_stream_file_cache: dict[tuple[int, str], tuple[float, Path, str]] = {}
_stream_file_cache_lock = threading.Lock()

# Manifest (urls + ext) cache. Tidal signs segment URLs for several
# minutes; a short TTL here means repeated clicks on the same track
# (quality-switch, play-again, etc.) skip the tidalapi round-trip.
_MANIFEST_CACHE_TTL = 90.0
_manifest_cache: dict[
    tuple[int, str], tuple[float, list[str], Optional[str]]
] = {}
_manifest_cache_lock = threading.Lock()


def _evict_expired_stream_files(now: float) -> None:
    """Drop and unlink any cached temp files past TTL. Called lazily on
    cache access — a periodic sweeper thread would be cleaner but this
    keeps the bookkeeping in one place."""
    stale: list[tuple[tuple[int, str], Path]] = []
    with _stream_file_cache_lock:
        for key, (ts, path, _mime) in list(_stream_file_cache.items()):
            if now - ts > _STREAM_FILE_CACHE_TTL:
                stale.append((key, path))
                _stream_file_cache.pop(key, None)
    for _, path in stale:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


def _lookup_stream_cache(
    key: tuple[int, str],
) -> Optional[tuple[Path, str]]:
    """Cache-hit lookup: returns (path, mime) if the cached file still
    exists and hasn't expired, else None. Touches the timestamp on hit
    so active tracks stay warm."""
    now = time.monotonic()
    _evict_expired_stream_files(now)
    with _stream_file_cache_lock:
        cached = _stream_file_cache.get(key)
        if cached and cached[1].exists():
            _stream_file_cache[key] = (now, cached[1], cached[2])
            return cached[1], cached[2]
    return None


def _install_stream_cache(key: tuple[int, str], path: Path, mime: str) -> None:
    """Install a freshly-buffered temp file into the cache. If an older
    entry existed (rare — two concurrent first-plays racing), unlink
    the stale file so we don't leak a tempfile until TTL sweep."""
    with _stream_file_cache_lock:
        old = _stream_file_cache.get(key)
        _stream_file_cache[key] = (time.monotonic(), path, mime)
    if old and old[1] != path:
        try:
            old[1].unlink(missing_ok=True)
        except Exception:
            pass


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


def _drain(q: asyncio.Queue) -> None:
    """Remove all pending events from `q` without blocking."""
    while True:
        try:
            q.get_nowait()
        except Exception:
            break


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
        # Deliver the whole snapshot as a SINGLE reset event so a client
        # reconnecting after a backend restart or network blip wipes any
        # ghost items left over from the previous session — replaying
        # individual `item` events would leave stale rows intact.
        q: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        snapshot = self.snapshot()
        await q.put(
            {"type": "reset", "items": [item_to_dict(i) for i in snapshot]}
        )
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
                            # Still can't fit a state-changing event. Rather
                            # than silently drop (which leaves the client
                            # permanently out of sync), drain the queue and
                            # push a desync marker — event_gen breaks on
                            # that marker, EventSource reconnects, and
                            # subscribe() re-sends a fresh reset snapshot.
                            _drain(q)
                            try:
                                q.put_nowait({"type": "__desync__"})
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

# Re-enqueue anything that was still pending when we shut down. We gate
# on check_login() because submits that fire without a valid session
# will each fail in their expand thread and surface as FAILED rows —
# which is loud, confusing, and not helpful (the user can't do anything
# about it until they sign in). Instead, leave the queue file alone and
# let the user's next session's restore pick them up.
try:
    if tidal.session.check_login():
        downloader.restore()
except Exception as _restore_exc:  # noqa: BLE001
    import sys as _sys
    print(
        f"[server] downloader.restore() failed: {_restore_exc!r}",
        file=_sys.stderr,
        flush=True,
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


def _cleanup_part_files(root: Path) -> None:
    """Remove orphaned *.part files left behind by a crashed process.

    The downloader writes atomically via `<name>.part` → rename. If the
    process is killed mid-download (OOM, SIGKILL, reboot), the `.part`
    never gets cleaned. `_find_existing` only matches completed extensions
    so these files accumulate invisibly over time.
    """
    if not root.exists():
        return
    try:
        for p in root.rglob("*.part"):
            try:
                p.unlink()
            except OSError:
                # Not fatal — another process may hold it, or the user may
                # have tightened permissions. Skip and move on.
                continue
    except OSError:
        pass


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    broker.bind_loop(asyncio.get_running_loop())
    output_root = Path(settings.output_dir).expanduser()
    _cleanup_part_files(output_root)
    local_index.start_scan(output_root)
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
        "share_url": _first(lambda: t.share_url),
    }


def album_to_dict(a) -> dict:
    release_date = _first(lambda: a.release_date)
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
        "share_url": _first(lambda: a.share_url),
        # Release date as an ISO date string (YYYY-MM-DD); the Tidal
        # object exposes it as a datetime.date. Frontend formats it.
        "release_date": str(release_date) if release_date else None,
        # Copyright line, usually "℗ 2024 <Label>" — we show it at the
        # bottom of the album page the way Tidal does.
        "copyright": _first(lambda: a.copyright) or None,
    }


def artist_to_dict(a) -> dict:
    return {
        "kind": "artist",
        "id": str(a.id),
        "name": a.name,
        "picture": _image_url(a, 750),
    }


def playlist_to_dict(p) -> dict:
    creator_obj = _first(lambda: p.creator)
    creator_name = _first(lambda: creator_obj.name) if creator_obj else None
    # Pass creator_id through even when it's 0 so the frontend can
    # inspect it; the frontend filters out the 0-sentinel (Tidal
    # editorial accounts) before rendering a profile link. Kept raw
    # so future debugging can tell "no creator" from "editorial
    # creator".
    creator_id_raw = getattr(creator_obj, "id", None) if creator_obj else None
    creator_id = str(creator_id_raw) if creator_id_raw is not None else None
    return {
        "kind": "playlist",
        "id": str(p.id),
        "name": p.name,
        "description": _first(lambda: p.description) or "",
        "num_tracks": _first(lambda: p.num_tracks) or 0,
        "duration": _first(lambda: p.duration) or 0,
        "cover": _image_url(p, 750),
        "creator": creator_name,
        "creator_id": creator_id,
        "owned": tidal.owns_playlist(p),
        "share_url": _first(lambda: p.share_url),
    }


def _require_auth() -> None:
    if not _is_logged_in():
        raise HTTPException(status_code=401, detail="Not authenticated")


def _require_local_access() -> None:
    """Allow access when the user is logged in OR offline mode is on.

    Used for endpoints that only touch local state (on-disk library,
    cached playback, settings, stats, reveal). When offline_mode is set,
    a signed-out user can still browse and play what they've already
    downloaded — that's the whole point of the toggle.
    """
    if _is_logged_in():
        return
    if getattr(settings, "offline_mode", False):
        return
    raise HTTPException(status_code=401, detail="Not authenticated")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


# Marker the desktop launcher uses to confirm a localhost port is occupied
# by *this* app rather than some unrelated server squatting on the port.
_HEALTH_MARKER = "tidal-downloader"

# Set by the desktop launcher so /api/_internal/focus can raise the window.
# The launcher registers a callable that runs on the pywebview thread; if
# nobody registered one (web-only dev run) the endpoint no-ops.
_focus_callback: Optional[Callable[[], None]] = None


def register_focus_callback(fn: Callable[[], None]) -> None:
    global _focus_callback
    _focus_callback = fn


@app.get("/api/health")
def health() -> dict:
    """Liveness probe AND single-instance detection marker.

    The desktop launcher probes this endpoint before binding its own
    port; an existing healthy response (with `app` == _HEALTH_MARKER)
    means another copy is already running and the second launch should
    exit instead of crashing on EADDRINUSE.
    """
    return {"ok": True, "app": _HEALTH_MARKER}


@app.post("/api/_internal/focus", include_in_schema=False)
def focus_window(request: Request) -> dict:
    """Ask the running pywebview window to raise/focus itself.

    Called by a second launch of the app after it detects the first is
    already running. Restricted to loopback because the only legitimate
    caller is a sibling process on the same machine.
    """
    client = request.client
    host = client.host if client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403)
    if _focus_callback is None:
        return {"ok": False, "reason": "no window"}
    try:
        _focus_callback()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


@app.get("/api/auth/status")
def auth_status() -> dict:
    logged_in = _is_logged_in()
    user_id: Optional[str] = None
    if logged_in:
        try:
            u = getattr(tidal.session, "user", None)
            if u is not None:
                raw = getattr(u, "id", None)
                # 0 is Tidal's sentinel for non-user creators; treat
                # as "unknown" so the profile link / self-compare
                # logic doesn't try to resolve it.
                if raw is not None and int(raw) > 0:
                    user_id = str(raw)
        except Exception:
            user_id = None
    return {
        "logged_in": logged_in,
        "username": tidal.get_user_info() if logged_in else None,
        "avatar": tidal.get_user_avatar_url() if logged_in else None,
        "user_id": user_id,
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


@app.get("/api/auth/pkce/url")
def auth_pkce_url() -> dict:
    """Return the browser URL for PKCE login.

    PKCE is the only login flow tidalapi supports that can stream hi-res
    (Max) audio — the device-code flow uses a `client_id` that Tidal
    caps at Lossless regardless of subscription. Tidal has no redirect
    handler for third-party apps, so after the user logs in they'll
    land on an 'Oops' page; they copy that URL back to us and we
    exchange the code in `/api/auth/pkce/complete`.
    """
    return {"url": tidal.pkce_login_url()}


_OPEN_EXTERNAL_HOSTS = {
    "tidal.com",
    "www.tidal.com",
    "listen.tidal.com",
    "login.tidal.com",
    "link.tidal.com",
    "auth.tidal.com",
    # Last.fm auth + API-account pages — users open these during the
    # scrobbling setup flow from inside Settings.
    "last.fm",
    "www.last.fm",
}


class OpenExternalRequest(BaseModel):
    url: str


@app.post("/api/open-external")
def open_external(req: OpenExternalRequest) -> dict:
    """Open a URL in the user's default system browser.

    Exists because pywebview's embedded WKWebView on macOS (and the
    equivalent WebView2 on Windows) silently drops `window.open(url,
    "_blank")` for navigations outside the app — the frontend can't
    break out to the real browser on its own. We do it from Python
    with `webbrowser.open()` which spawns the system default.

    Host-allowlisted to Tidal domains so a mischievous page on localhost
    can't weaponize this into a generic URL-opener.
    """
    parsed = urlparse(req.url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only http/https URLs allowed")
    if parsed.hostname not in _OPEN_EXTERNAL_HOSTS:
        raise HTTPException(
            status_code=403,
            detail=f"Host not allowed: {parsed.hostname}",
        )
    try:
        ok = webbrowser.open(req.url, new=2)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=500, detail="No browser available")
    return {"ok": True}


class PkceCompleteRequest(BaseModel):
    redirect_url: str


@app.post("/api/auth/pkce/complete")
def auth_pkce_complete(req: PkceCompleteRequest) -> dict:
    """Exchange the pasted 'Oops' redirect URL for hi-res-entitled
    tokens and persist the session."""
    if not req.redirect_url or "code=" not in req.redirect_url:
        raise HTTPException(
            status_code=400,
            detail="Paste the full URL from the Oops page (must contain a ?code=… query param).",
        )
    ok = tidal.complete_pkce_login(req.redirect_url.strip())
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="PKCE login failed. Double-check that you pasted the URL from the Oops page immediately after logging in.",
        )
    _invalidate_auth_cache()
    _invalidate_preview_cache()
    _apply_settings_quality(settings)
    return {"status": "ok", "username": tidal.get_user_info()}


# ---------------------------------------------------------------------------
# Last.fm scrobbling — optional integration. Stores the user's own
# api_key/api_secret (registered at last.fm/api/account/create), runs the
# standard desktop auth flow, and exposes scrobble + now-playing calls
# the frontend player hits on each track.
# ---------------------------------------------------------------------------


@app.get("/api/lastfm/status")
def lastfm_status() -> dict:
    return lastfm.status()


class LastFmCredentialsRequest(BaseModel):
    api_key: str
    api_secret: str


@app.put("/api/lastfm/credentials")
def lastfm_set_credentials(req: LastFmCredentialsRequest) -> dict:
    _require_auth()
    if not req.api_key.strip() or not req.api_secret.strip():
        raise HTTPException(
            status_code=400,
            detail="Both API key and API secret are required.",
        )
    lastfm.set_credentials(req.api_key, req.api_secret)
    return lastfm.status()


@app.post("/api/lastfm/connect/start")
def lastfm_connect_start() -> dict:
    _require_auth()
    try:
        url, token = lastfm.get_auth_url()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"auth_url": url, "token": token}


class LastFmCompleteRequest(BaseModel):
    token: str


@app.post("/api/lastfm/connect/complete")
def lastfm_connect_complete(req: LastFmCompleteRequest) -> dict:
    _require_auth()
    try:
        username = lastfm.complete_auth(req.token.strip())
    except Exception as exc:
        # The most common failure mode is "Unauthorized Token" — user
        # clicked Continue before actually approving in the browser.
        raise HTTPException(status_code=400, detail=str(exc))
    return {"connected": True, "username": username}


@app.post("/api/lastfm/disconnect")
def lastfm_disconnect() -> dict:
    _require_auth()
    lastfm.disconnect()
    return lastfm.status()


@app.get("/api/lastfm/recent-tracks")
def lastfm_recent_tracks(limit: int = 100) -> list[dict]:
    """Proxy ``user.getRecentTracks`` so the frontend can render the
    user's cross-device listening history on the History page. Public
    Last.fm endpoint, only needs the username + api_key."""
    _require_auth()
    return lastfm.get_recent_tracks(limit=limit)


_VALID_LASTFM_PERIODS = {"overall", "7day", "1month", "3month", "6month", "12month"}


def _validate_period(period: str) -> str:
    if period not in _VALID_LASTFM_PERIODS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown period. Valid: {', '.join(sorted(_VALID_LASTFM_PERIODS))}",
        )
    return period


@app.get("/api/lastfm/user-info")
def lastfm_user_info() -> dict:
    """Header profile data for the Stats page — playcount, registered
    date, avatar. Empty dict if Last.fm isn't connected."""
    _require_auth()
    return lastfm.get_user_info()


@app.get("/api/lastfm/top-artists")
def lastfm_top_artists(period: str = "overall", limit: int = 50) -> list[dict]:
    _require_auth()
    return lastfm.get_top_artists(period=_validate_period(period), limit=limit)


@app.get("/api/lastfm/top-tracks")
def lastfm_top_tracks(period: str = "overall", limit: int = 50) -> list[dict]:
    _require_auth()
    return lastfm.get_top_tracks(period=_validate_period(period), limit=limit)


@app.get("/api/lastfm/top-albums")
def lastfm_top_albums(period: str = "overall", limit: int = 50) -> list[dict]:
    _require_auth()
    return lastfm.get_top_albums(period=_validate_period(period), limit=limit)


@app.get("/api/lastfm/loved-tracks")
def lastfm_loved_tracks(limit: int = 50) -> list[dict]:
    _require_auth()
    return lastfm.get_loved_tracks(limit=limit)


@app.get("/api/lastfm/artist-playcount")
def lastfm_artist_playcount(artist: str) -> dict:
    _require_auth()
    if not artist:
        raise HTTPException(status_code=400, detail="artist is required")
    return lastfm.get_artist_playcount(artist)


@app.get("/api/lastfm/album-playcount")
def lastfm_album_playcount(artist: str, album: str) -> dict:
    _require_auth()
    if not artist or not album:
        raise HTTPException(status_code=400, detail="artist and album are required")
    return lastfm.get_album_playcount(artist, album)


@app.get("/api/lastfm/track-playcount")
def lastfm_track_playcount(artist: str, track: str) -> dict:
    _require_auth()
    if not artist or not track:
        raise HTTPException(status_code=400, detail="artist and track are required")
    return lastfm.get_track_playcount(artist, track)


@app.get("/api/lastfm/chart/top-artists")
def lastfm_chart_top_artists(limit: int = 50) -> list[dict]:
    _require_auth()
    return lastfm.get_chart_top_artists(limit=limit)


@app.get("/api/lastfm/chart/top-tracks")
def lastfm_chart_top_tracks(limit: int = 50) -> list[dict]:
    _require_auth()
    return lastfm.get_chart_top_tracks(limit=limit)


@app.get("/api/lastfm/chart/top-tags")
def lastfm_chart_top_tags(limit: int = 50) -> list[dict]:
    _require_auth()
    return lastfm.get_chart_top_tags(limit=limit)


_weekly_scrobbles_cache: dict[str, tuple[float, list]] = {}
_weekly_scrobbles_lock = threading.Lock()
_WEEKLY_SCROBBLES_TTL_SEC = 900.0  # 15 minutes — cheap enough to refresh.


@app.get("/api/lastfm/weekly-scrobbles")
def lastfm_weekly_scrobbles(weeks: int = 52) -> list[dict]:
    """Scrobble counts per week for the last N weeks. Backs the
    listening-activity chart on the Stats page. Cached because a 52-week
    fetch is 52 Last.fm requests — we can't afford to re-run it on
    every page visit."""
    _require_auth()
    weeks = max(1, min(104, weeks))
    # Cache key: username + weeks count. Username because disconnecting
    # and reconnecting to a different account should invalidate; weeks
    # because the caller may request different ranges.
    status = lastfm.status()
    username = status.get("username") or ""
    key = f"{username}:{weeks}"
    now = time.monotonic()
    with _weekly_scrobbles_lock:
        cached = _weekly_scrobbles_cache.get(key)
        if cached and (now - cached[0]) < _WEEKLY_SCROBBLES_TTL_SEC:
            return cached[1]
    data = lastfm.get_weekly_scrobbles(weeks=weeks)
    with _weekly_scrobbles_lock:
        _weekly_scrobbles_cache[key] = (now, data)
    return data


class LastFmTrackRequest(BaseModel):
    artist: str
    track: str
    album: str = ""
    duration: int = 0
    timestamp: Optional[int] = None


@app.post("/api/lastfm/now-playing")
def lastfm_now_playing(req: LastFmTrackRequest) -> dict:
    _require_auth()
    try:
        lastfm.now_playing(
            artist=req.artist,
            track=req.track,
            album=req.album,
            duration=req.duration,
        )
    except RuntimeError:
        # Not connected or bad credentials — the frontend fires this
        # on every track start, so returning a clean 200 with ok=false
        # avoids spamming toasts / console when scrobbling is simply
        # disabled.
        return {"ok": False}
    return {"ok": True}


@app.post("/api/lastfm/scrobble")
def lastfm_scrobble(req: LastFmTrackRequest) -> dict:
    _require_auth()
    try:
        lastfm.scrobble(
            artist=req.artist,
            track=req.track,
            album=req.album,
            duration=req.duration,
            timestamp=req.timestamp,
        )
    except RuntimeError:
        return {"ok": False}
    return {"ok": True}


# ---------------------------------------------------------------------------
# Play reporting to Tidal's Event Producer
#
# Without this, plays through our client don't count for Tidal's Recently
# Played, recommendations, or royalty accounting. `tidalapi` doesn't wrap
# the event-producer endpoint, so `app/play_reporter.py` does it directly.
# Frontend calls /start at track-play time, /stop when the track ends or is
# skipped. A single `playback_session` event captures both actions.
# ---------------------------------------------------------------------------


class PlayReportStopRequest(BaseModel):
    session_id: str
    track_id: str
    quality: str
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    start_ts_ms: int
    end_ts_ms: int
    start_position_s: float
    end_position_s: float


@app.post("/api/play-report/start")
def play_report_start(req: dict) -> dict:
    """Hand the caller a session_id for a new play. No network traffic.

    The real event is sent at /stop time so it contains both actions in
    one message — that's how Tidal's own SDKs structure `playback_session`.
    """
    _require_auth()
    return {"session_id": str(uuid.uuid4()), "ts_ms": int(time.time() * 1000)}


@app.post("/api/play-report/stop")
def play_report_stop(req: PlayReportStopRequest) -> dict:
    _require_auth()
    play_reporter.record(
        PlaySession(
            session_id=req.session_id,
            track_id=str(req.track_id),
            quality=req.quality,
            source_type=req.source_type,
            source_id=req.source_id,
            start_ts_ms=req.start_ts_ms,
            end_ts_ms=req.end_ts_ms,
            start_position_s=req.start_position_s,
            end_position_s=req.end_position_s,
        )
    )
    return {"ok": True}


@app.post("/api/auth/logout")
def auth_logout() -> dict:
    # Order matters: tear down the session, then invalidate every cache that
    # could still vend data tied to it.
    tidal.logout()
    _invalidate_auth_cache()
    _invalidate_preview_cache()
    # Drop the persisted download queue too — it's keyed to the now-
    # logged-out account and a different user signing in next should
    # NOT inherit someone else's pending queue. The in-memory broker
    # state is separate; cancel_all_active handles that only when the
    # user explicitly requests it.
    from app.downloader import QUEUE_STATE_FILE as _QSF
    try:
        _QSF.unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True}


# ---------------------------------------------------------------------------
# User profiles + follow graph
#
# tidalapi only wraps `session.get_user(id)` and the logged-in user's
# own playlists. The rest of the social surface (arbitrary-user
# playlists, follow/unfollow, followers/following) isn't in the
# library, so we hit Tidal's v2 REST directly. These endpoints are
# undocumented — we keep every call in a try/except and return empty
# lists on error so the UI can degrade gracefully.
# ---------------------------------------------------------------------------


def _user_image_url(user) -> Optional[str]:
    """Best-available profile picture URL for a tidalapi User. The
    `image()` helper requires one of a fixed set of sizes; pick the
    mid-large one and fall back to smaller if the larger 404s."""
    for size in (600, 210, 100):
        try:
            return user.image(size)
        except Exception:
            continue
    return None


def user_to_dict(u) -> dict:
    first = getattr(u, "first_name", None) or ""
    last = getattr(u, "last_name", None) or ""
    full = (first + " " + last).strip() or getattr(u, "username", None) or ""
    return {
        "id": str(u.id),
        "name": full,
        "first_name": first,
        "last_name": last,
        "picture": _user_image_url(u),
    }


@app.get("/api/user/{user_id}")
def user_profile(user_id: int) -> dict:
    """Fetch a user's profile. Tries multiple endpoints because
    Tidal's v1 `/users/{id}` 404s for users who've restricted their
    top-level profile visibility — even when their public playlists
    and follower graph are still exposed via separate endpoints.

    When every path fails, we still return a stub with the numeric
    id + empty fields so the frontend can render the profile page
    with its playlists / followers / following sections (which use
    their own endpoints and often succeed when the top-level one
    doesn't). Better UX than blanking the whole page.
    """
    _require_auth()
    # Path 1: tidalapi's v1 `/users/{id}` — works for most profiles.
    try:
        u = tidal.session.get_user(user_id)
        return user_to_dict(u)
    except Exception:
        pass
    # Path 2: v2 profile endpoint — some users only expose metadata
    # via the newer profile surface. Shape differs; parse defensively.
    try:
        resp = tidal.session.request.request(
            "GET",
            f"user-profiles/{user_id}",
            base_url=tidal.session.config.api_v2_location,
        )
        if resp.status_code < 400:
            data = resp.json()
            attrs = (
                data.get("data", {}).get("attributes")
                if isinstance(data, dict)
                else None
            ) or (data if isinstance(data, dict) else {})
            name = (
                attrs.get("name")
                or f"{attrs.get('firstName') or ''} {attrs.get('lastName') or ''}".strip()
            )
            picture = attrs.get("pictureUrl") or attrs.get("picture")
            if name or picture:
                return {
                    "id": str(user_id),
                    "name": name or f"User {user_id}",
                    "first_name": attrs.get("firstName") or "",
                    "last_name": attrs.get("lastName") or "",
                    "picture": picture,
                }
    except Exception:
        pass
    # Path 3: harvest profile info from the user's public playlists.
    # Tidal embeds the full creator object (firstName, lastName,
    # picture uuid) on every playlist in the public-playlists
    # response, so we can synthesize a profile even when both direct
    # user endpoints have refused us. Worst case (no playlists) we
    # fall through to a numeric-only stub.
    try:
        resp = tidal.session.request.request(
            "GET",
            f"user-playlists/{user_id}/public",
            params={"limit": 1, "offset": 0},
        )
        if resp.status_code < 400:
            payload = resp.json()
            items = payload.get("items") if isinstance(payload, dict) else None
            if isinstance(items, list) and items:
                first_item = items[0] if isinstance(items[0], dict) else {}
                pl = first_item.get("playlist") or first_item
                creator_data = pl.get("creator") if isinstance(pl, dict) else None
                if isinstance(creator_data, dict):
                    fn = creator_data.get("firstName") or ""
                    ln = creator_data.get("lastName") or ""
                    name = (f"{fn} {ln}").strip() or creator_data.get("name")
                    # Picture UUIDs follow the same pattern as every
                    # other Tidal image — hyphens → slashes, size
                    # suffix. tidalapi's User.image() helper uses
                    # 100/210/600 as valid sizes; 600 gives a clean
                    # avatar without being huge.
                    pic_uuid = creator_data.get("picture")
                    picture = (
                        f"https://resources.tidal.com/images/{pic_uuid.replace('-', '/')}/600x600.jpg"
                        if pic_uuid
                        else None
                    )
                    return {
                        "id": str(user_id),
                        "name": name or f"User {user_id}",
                        "first_name": fn,
                        "last_name": ln,
                        "picture": picture,
                    }
    except Exception:
        pass
    # Final fallback: numeric-only stub. Follower / following /
    # playlist sections still populate on the frontend.
    return {
        "id": str(user_id),
        "name": f"User {user_id}",
        "first_name": "",
        "last_name": "",
        "picture": None,
    }


@app.get("/api/user/{user_id}/playlists")
def user_playlists(user_id: int, limit: int = 50) -> list[dict]:
    """Public playlists created by a user. Works for both the logged-
    in user (goes via tidalapi) and arbitrary users (v2 REST). Returns
    an empty list rather than 4xx when the user has no public
    playlists so the UI doesn't have to special-case it."""
    _require_auth()
    try:
        me = getattr(tidal.session, "user", None)
        if me is not None and int(getattr(me, "id", 0) or 0) == int(user_id):
            # Logged-in user — use the tidalapi helper, which also
            # returns private playlists (fine for your own profile).
            playlists = me.public_playlists(limit=limit, offset=0)
            return [playlist_to_dict(p) for p in playlists or []]
    except Exception:
        pass
    # Arbitrary user — v2 REST.
    try:
        resp = tidal.session.request.request(
            "GET",
            f"user-playlists/{user_id}/public",
            params={"limit": limit, "offset": 0},
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return []
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        # Tidal nests the playlist under `playlist` in some response
        # shapes and not others; try both.
        pl = row.get("playlist") if isinstance(row.get("playlist"), dict) else row
        if not isinstance(pl, dict):
            continue
        pid = pl.get("uuid") or pl.get("id")
        if not pid:
            continue
        # Parse inline — the response already carries everything we
        # display on a card (name, track count, duration, cover UUID,
        # creator). Avoids N network calls to `session.playlist(pid)`.
        creator_data = pl.get("creator") if isinstance(pl.get("creator"), dict) else None
        creator_name = None
        creator_id = None
        if creator_data:
            first = creator_data.get("firstName") or ""
            last = creator_data.get("lastName") or ""
            creator_name = (first + " " + last).strip() or creator_data.get("name")
            cid = creator_data.get("id")
            if cid is not None:
                creator_id = str(cid)
        cover_uuid = pl.get("squareImage") or pl.get("image")
        cover = (
            _cover_url_from_uuid(cover_uuid, 750)
            if isinstance(cover_uuid, str)
            else None
        )
        out.append(
            {
                "kind": "playlist",
                "id": str(pid),
                "name": pl.get("title") or pl.get("name") or "",
                "description": pl.get("description") or "",
                "num_tracks": pl.get("numberOfTracks") or pl.get("num_tracks") or 0,
                "duration": pl.get("duration") or 0,
                "cover": cover,
                "creator": creator_name,
                "creator_id": creator_id,
                "owned": False,
                "share_url": pl.get("url")
                or (
                    f"https://tidal.com/browse/playlist/{pid}"
                    if pid
                    else None
                ),
            }
        )
    return out


def _picture_url_from_uuid(uuid: Optional[str], size: int = 210) -> Optional[str]:
    """Turn a raw Tidal picture UUID into a CDN URL. Matches the
    format `tidalapi.User.image()` builds (hyphens → slashes, size
    suffix)."""
    if not uuid or not isinstance(uuid, str):
        return None
    return f"https://resources.tidal.com/images/{uuid.replace('-', '/')}/{size}x{size}.jpg"


def _follow_list_page(path: str, limit: int, offset: int = 0) -> list[dict]:
    """Parse one page of a followers/following response into
    user_to_dict rows.

    Critical perf fix: the v1/v2 response already embeds `firstName`,
    `lastName`, and `picture` UUID on every row, so we build the row
    directly instead of round-tripping `session.get_user(id)` for each
    one. For a popular profile the old path did ~200 serial Tidal
    calls just to render the list.
    """
    try:
        resp = tidal.session.request.request(
            "GET", path, params={"limit": limit, "offset": offset}
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return []
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        user_data = row.get("profile") or row.get("user") or row
        if not isinstance(user_data, dict):
            continue
        user_id = user_data.get("userId") or user_data.get("id")
        if not user_id:
            continue
        first = user_data.get("firstName") or ""
        last = user_data.get("lastName") or ""
        name = (first + " " + last).strip()
        out.append(
            {
                "id": str(user_id),
                "name": name or f"User {user_id}",
                "first_name": first,
                "last_name": last,
                "picture": _picture_url_from_uuid(user_data.get("picture")),
            }
        )
    return out


def _follow_list(path: str, limit: int) -> list[dict]:
    """Back-compat wrapper for callers that only need the first page."""
    return _follow_list_page(path, limit=limit, offset=0)


@app.get("/api/user/{user_id}/counts")
def user_social_counts(user_id: int) -> dict:
    """Cheap two-count endpoint for profile headers — fetch the raw
    payloads in parallel threads and read `totalNumberOfItems` off
    each instead of materializing two full user lists just to call
    `.length` on them.
    """
    _require_auth()

    def _count(path: str) -> int:
        try:
            resp = tidal.session.request.request(
                "GET", path, params={"limit": 1, "offset": 0}
            )
            if resp.status_code >= 400:
                return 0
            data = resp.json()
            total = (
                data.get("totalNumberOfItems")
                if isinstance(data, dict)
                else None
            )
            if isinstance(total, int):
                return total
            items = data.get("items") if isinstance(data, dict) else None
            return len(items) if isinstance(items, list) else 0
        except Exception:
            return 0

    return {
        "followers": _count(f"users/{user_id}/followers"),
        "following": _count(f"users/{user_id}/following"),
    }


@app.get("/api/user/{user_id}/followers")
def user_followers(user_id: int, limit: int = 50) -> list[dict]:
    _require_auth()
    return _follow_list(f"users/{user_id}/followers", limit)


@app.get("/api/user/{user_id}/following")
def user_following(user_id: int, limit: int = 50) -> list[dict]:
    _require_auth()
    return _follow_list(f"users/{user_id}/following", limit)


@app.post("/api/user/{user_id}/follow")
def follow_user(user_id: int) -> dict:
    """Follow a user. Endpoint is undocumented — we try the pattern
    Tidal's own web client uses. Returns `{ok: bool, error?: str}`."""
    _require_auth()
    try:
        resp = tidal.session.request.request(
            "PUT", f"users/{user_id}/follow", params={}
        )
        if resp.status_code >= 400:
            # Try the POST form — some tenants use one, some the other.
            resp = tidal.session.request.request(
                "POST", f"users/{user_id}/follow", params={}
            )
        if resp.status_code >= 400:
            return {
                "ok": False,
                "error": f"Tidal returned HTTP {resp.status_code}",
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


@app.delete("/api/user/{user_id}/follow")
def unfollow_user(user_id: int) -> dict:
    _require_auth()
    try:
        resp = tidal.session.request.request(
            "DELETE", f"users/{user_id}/follow", params={}
        )
        if resp.status_code >= 400:
            return {
                "ok": False,
                "error": f"Tidal returned HTTP {resp.status_code}",
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


@app.get("/api/me/following/status/{user_id}")
def is_following(user_id: int) -> dict:
    """Whether the logged-in user is following `user_id`.

    Tidal has no direct "am-I-following" endpoint (probed every plausible
    shape — 404 on each), so we scan the logged-in user's `following`
    list. Page through in chunks of 200 with early-exit when we find a
    match; cap at 2000 entries (10 pages) to bound worst-case latency.
    False negative above that cap is acceptable — the follow button
    will just show "Follow" and clicking it silently no-ops the server
    side (already-following is idempotent on Tidal's end).
    """
    _require_auth()
    try:
        me = tidal.session.user
        my_id = int(getattr(me, "id", 0) or 0)
        if not my_id:
            return {"following": False}
        target = str(user_id)
        page_size = 200
        hard_cap_pages = 10
        for page in range(hard_cap_pages):
            offset = page * page_size
            rows = _follow_list_page(
                f"users/{my_id}/following", limit=page_size, offset=offset
            )
            if any(u.get("id") == target for u in rows):
                return {"following": True}
            if len(rows) < page_size:
                return {"following": False}
        return {"following": False}
    except Exception:
        return {"following": False}


# ---------------------------------------------------------------------------
# Native audio player (libvlc backend)
#
# The browser `<audio>` element can't decode Atmos / MQA / 360, so this
# route surface exposes a parallel playback engine driven by libvlc on
# the server. The frontend is a remote control: it POSTs commands and
# reads state via GET /api/player/state (one-shot) or subscribes to
# GET /api/player/events (SSE at ~4Hz during playback).
# ---------------------------------------------------------------------------


class _PlayerLoadRequest(BaseModel):
    track_id: str
    quality: Optional[str] = None


class _PlayerSeekRequest(BaseModel):
    fraction: float  # 0..1


class _PlayerVolumeRequest(BaseModel):
    volume: int  # 0..100


class _PlayerMutedRequest(BaseModel):
    muted: bool


def _native_player():
    if not native_player.is_available():
        raise HTTPException(
            status_code=503,
            detail="Native audio engine unavailable (libvlc not loaded)",
        )
    return native_player.get_player(lambda: tidal.session)


def _snapshot_dict(snap: native_player.PlayerSnapshot) -> dict:
    return {
        "state": snap.state,
        "track_id": snap.track_id,
        "position_ms": snap.position_ms,
        "duration_ms": snap.duration_ms,
        "volume": snap.volume,
        "muted": snap.muted,
        "error": snap.error,
        "seq": snap.seq,
    }


@app.get("/api/player/available")
def player_available() -> dict:
    """Feature-probe endpoint. Frontend reads this once at startup to
    decide whether to offer the native-engine toggle in settings."""
    return {"available": native_player.is_available()}


@app.get("/api/player/state")
def player_state() -> dict:
    _require_auth()
    return _snapshot_dict(_native_player().snapshot())


@app.post("/api/player/load")
def player_load(req: _PlayerLoadRequest) -> dict:
    _require_auth()
    snap = _native_player().load(req.track_id, quality=req.quality)
    return _snapshot_dict(snap)


@app.post("/api/player/play")
def player_play() -> dict:
    _require_auth()
    return _snapshot_dict(_native_player().play())


@app.post("/api/player/pause")
def player_pause() -> dict:
    _require_auth()
    return _snapshot_dict(_native_player().pause())


@app.post("/api/player/resume")
def player_resume() -> dict:
    _require_auth()
    return _snapshot_dict(_native_player().resume())


@app.post("/api/player/stop")
def player_stop() -> dict:
    _require_auth()
    return _snapshot_dict(_native_player().stop())


@app.post("/api/player/seek")
def player_seek(req: _PlayerSeekRequest) -> dict:
    _require_auth()
    return _snapshot_dict(_native_player().seek(req.fraction))


@app.post("/api/player/volume")
def player_volume(req: _PlayerVolumeRequest) -> dict:
    _require_auth()
    return _snapshot_dict(_native_player().set_volume(req.volume))


@app.post("/api/player/muted")
def player_muted(req: _PlayerMutedRequest) -> dict:
    _require_auth()
    return _snapshot_dict(_native_player().set_muted(req.muted))


@app.get("/api/player/events")
async def player_events(request: Request):
    """SSE stream of player snapshots.

    libvlc fires state-change callbacks immediately (play/pause/ended);
    position has to be polled because libvlc doesn't emit a
    time-changed callback synchronously. We combine both: every
    subscribe-fired snapshot is sent right away, and in between we poll
    at 4Hz so the frontend gets smooth position updates during
    playback. When paused/idle we drop to a 1Hz heartbeat to keep the
    connection alive without wasting cycles.
    """
    _require_auth()
    player = _native_player()
    queue: asyncio.Queue[Optional[dict]] = asyncio.Queue(maxsize=32)
    loop = asyncio.get_running_loop()

    def _on_snapshot(snap: native_player.PlayerSnapshot) -> None:
        payload = _snapshot_dict(snap)
        try:
            loop.call_soon_threadsafe(queue.put_nowait, payload)
        except Exception:
            pass

    unsubscribe = player.subscribe(_on_snapshot)

    async def _gen():
        try:
            # Send the current state immediately so the client has a
            # snapshot without waiting for the first change event.
            yield f"data: {json.dumps(_snapshot_dict(player.snapshot()))}\n\n"
            last_seq = -1
            while True:
                if await request.is_disconnected():
                    break
                active = player.snapshot().state == "playing"
                timeout = 0.25 if active else 1.0
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    payload = _snapshot_dict(player.snapshot())
                if payload is None:
                    break
                seq = payload.get("seq", 0)
                if seq == last_seq and payload.get("state") != "playing":
                    # Dedupe keepalive ticks while nothing is happening.
                    continue
                last_seq = seq
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            unsubscribe()

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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
# Playlist folders
#
# `tidalapi` exposes folder support via `session.user.playlist_folders()`,
# `session.user.create_folder()`, and the Folder class (rename, remove,
# move_items_to_folder). Folder IDs are UUIDs; the special ID "root" is
# the top-level container. Playlist IDs become `trn:playlist:<id>` when
# used in move calls — we handle the prefixing here so the frontend can
# work with plain IDs.
# ---------------------------------------------------------------------------


def folder_to_dict(f) -> dict:
    return {
        "id": str(getattr(f, "id", "") or ""),
        "name": getattr(f, "name", "") or "",
        "parent_id": getattr(f, "parent_folder_id", "root") or "root",
        "num_items": int(getattr(f, "total_number_of_items", 0) or 0),
    }


def _ensure_playlist_trns(ids: list[str]) -> list[str]:
    """Tidal's folder-move endpoint wants `trn:playlist:<id>` TRNs. The
    frontend sends bare IDs, so prefix them here when missing."""
    out: list[str] = []
    for pid in ids:
        if not pid:
            continue
        out.append(pid if pid.startswith("trn:playlist:") else f"trn:playlist:{pid}")
    return out


@app.get("/api/library/folders")
def list_folders(parent_id: str = "root") -> list[dict]:
    _require_auth()
    try:
        folders = tidal.session.user.playlist_folders(
            limit=50, parent_folder_id=parent_id
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return [folder_to_dict(f) for f in folders]


@app.get("/api/library/folders/{folder_id}/playlists")
def list_folder_playlists(folder_id: str) -> list[dict]:
    _require_auth()
    try:
        folder = _get_folder(folder_id)
        items = folder.items(offset=0, limit=50)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return [playlist_to_dict(p) for p in items]


class CreateFolderRequest(BaseModel):
    name: str
    parent_id: str = "root"


@app.post("/api/library/folders")
def create_folder(req: CreateFolderRequest) -> dict:
    _require_auth()
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Folder name is required")
    try:
        folder = tidal.session.user.create_folder(
            title=req.name.strip(), parent_id=req.parent_id or "root"
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return folder_to_dict(folder)


class RenameFolderRequest(BaseModel):
    name: str


@app.patch("/api/library/folders/{folder_id}")
def rename_folder(folder_id: str, req: RenameFolderRequest) -> dict:
    _require_auth()
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Folder name is required")
    try:
        folder = _get_folder(folder_id)
        folder.rename(req.name.strip())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


@app.delete("/api/library/folders/{folder_id}")
def delete_folder(folder_id: str) -> dict:
    _require_auth()
    try:
        folder = _get_folder(folder_id)
        folder.remove()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


class MovePlaylistsRequest(BaseModel):
    playlist_ids: list[str]


@app.post("/api/library/folders/{folder_id}/playlists")
def add_playlists_to_folder(folder_id: str, req: MovePlaylistsRequest) -> dict:
    """Move one or more playlists into `folder_id`. Use "root" to move
    them out of any folder back to the top level."""
    _require_auth()
    trns = _ensure_playlist_trns(req.playlist_ids)
    if not trns:
        return {"ok": True}
    try:
        # `tidalapi.Folder.move_items_to_folder` needs an instance, but
        # we only need one to call the method — "root" has no real
        # instance to load, so we find any existing folder and call
        # from there. If none exist, create a throwaway instance.
        any_folder = _first_folder_or_throwaway()
        any_folder.move_items_to_folder(trns, folder=folder_id or "root")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


def _get_folder(folder_id: str):
    """Load a Folder instance by ID. tidalapi doesn't expose a direct
    getter, so we list the user's folders and find the match."""
    import tidalapi

    if folder_id == "root":
        raise HTTPException(status_code=400, detail="'root' is not a real folder")
    for f in tidal.session.user.playlist_folders(limit=50, parent_folder_id="root"):
        if str(getattr(f, "id", "")) == folder_id:
            return f
    # Nested — fall back to instantiating directly. tidalapi's Folder
    # constructor triggers a fetch that populates the rest of the fields.
    return tidalapi.Folder(session=tidal.session, folder_id=folder_id)


def _first_folder_or_throwaway():
    """Return any Folder instance we can call move/rename methods on.
    We don't actually care which — the instance is just the receiver
    for the REST call; the target folder is passed as an argument."""
    import tidalapi

    existing = tidal.session.user.playlist_folders(limit=1, parent_folder_id="root")
    if existing:
        return existing[0]
    # No user folders yet. Construct a bare instance pointing at "root"
    # so the method resolves — tidalapi's Folder methods post to fixed
    # endpoints and only use `self.trn` for a couple of operations, not
    # move_items_to_folder.
    return tidalapi.Folder(session=tidal.session, folder_id="root")


# Cache of (path, mtime_ns, size) -> tags dict, shared across /api/library/local
# calls so repeat loads don't re-open every file. Keyed by absolute path; an
# mtime mismatch invalidates the entry (covers re-tags, file replacements).
_LOCAL_TAG_CACHE: dict[str, tuple[int, int, dict]] = {}
_LOCAL_TAG_CACHE_LOCK = threading.Lock()


def _read_cached_tags(path: Path, stat_result) -> Optional[dict]:
    from app.metadata import read_track_tags

    key = str(path)
    mtime_ns = getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000))
    size = stat_result.st_size
    with _LOCAL_TAG_CACHE_LOCK:
        cached = _LOCAL_TAG_CACHE.get(key)
        if cached and cached[0] == mtime_ns and cached[1] == size:
            return cached[2]
    tags = read_track_tags(path)
    if tags is None:
        return None
    with _LOCAL_TAG_CACHE_LOCK:
        _LOCAL_TAG_CACHE[key] = (mtime_ns, size, tags)
    return tags


@app.get("/api/library/local")
def library_local() -> dict:
    """List the user's downloaded audio files with their tags. The
    frontend's Local Library page groups these by artist/album so the
    user can browse what's actually on disk (as opposed to what they've
    favorited in Tidal).

    Reads tags via mutagen, cached by (path, mtime, size) so a second
    load is effectively free. Files without usable tags are skipped
    rather than surfaced as "Unknown Artist" rows.
    """
    _require_local_access()
    import os as _os

    root = Path(settings.output_dir).expanduser()
    files: list[dict] = []
    if not root.is_dir():
        return {"output_dir": str(root), "files": []}
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            with _os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        ext = _os.path.splitext(entry.name)[1].lower()
                        if ext not in _AUDIO_EXTENSIONS:
                            continue
                        st = entry.stat()
                        path = Path(entry.path)
                        tags = _read_cached_tags(path, st)
                        if tags is None:
                            continue
                        # Fall back to folder names for untagged or
                        # partially-tagged files — better than dropping them.
                        parent = path.parent
                        artist = tags.get("artist") or (parent.parent.name if parent != root else "")
                        album = tags.get("album") or parent.name
                        title = tags.get("title") or path.stem
                        if not artist:
                            continue
                        try:
                            rel = str(path.relative_to(root))
                        except ValueError:
                            rel = entry.name
                        files.append({
                            "path": str(path),
                            "relative_path": rel,
                            "title": title,
                            "artist": artist,
                            "album": album,
                            "track_num": tags.get("track_num") or 0,
                            "tidal_id": tags.get("tidal_id"),
                            "duration": tags.get("duration") or 0,
                            "size_bytes": st.st_size,
                            "ext": ext,
                        })
                    except OSError:
                        continue
        except OSError:
            continue
    # Sort deterministically: artist → album → track_num → title. This is
    # what the frontend expects to render without re-sorting on every tab
    # switch.
    files.sort(key=lambda f: (
        f["artist"].lower(),
        (f["album"] or "").lower(),
        f["track_num"],
        f["title"].lower(),
    ))
    return {"output_dir": str(root), "files": files}


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
    # Similar / review / more-by-artist / related-artists are all best-
    # effort. Tidal 404s on many of these for non-editorial content; we'd
    # rather render the page without a given section than 500 the whole
    # request.
    try:
        similar = [album_to_dict(a) for a in album.similar()][:12]
    except Exception:
        similar = []
    try:
        review = album.review() or None
    except Exception:
        review = None

    # Other albums by the primary artist, excluding the current album.
    # We take full-lengths first, then pad with EP/singles so the row
    # always has something to show when we can fetch anything at all.
    more_by: list[dict] = []
    related_artists: list[dict] = []
    primary = _first(lambda: album.artist) or (
        album.artists[0] if getattr(album, "artists", None) else None
    )
    if primary is not None:
        try:
            full = list(tidal.get_artist_albums(primary)) or []
        except Exception:
            full = []
        try:
            eps = list(primary.get_ep_singles(limit=20)) or []
        except Exception:
            eps = []
        combined = full + eps
        current_id = str(album.id)
        seen: set[str] = set()
        for a in combined:
            aid = str(getattr(a, "id", "") or "")
            if not aid or aid == current_id or aid in seen:
                continue
            seen.add(aid)
            more_by.append(album_to_dict(a))
            if len(more_by) >= 12:
                break
        try:
            related_artists = [
                artist_to_dict(x) for x in primary.get_similar()
            ][:12]
        except Exception:
            related_artists = []

    return {
        **album_to_dict(album),
        "tracks": [track_to_dict(t) for t in tidal.get_album_tracks(album)],
        "similar": similar,
        "review": review,
        "more_by_artist": more_by,
        "related_artists": related_artists,
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
    # tidalapi doesn't expose the EP/single-only lists as part of the
    # default get_albums query — tidal's own client shows those on the
    # artist page (below the full-length discography), so include them.
    try:
        ep_singles = [album_to_dict(a) for a in artist.get_ep_singles(limit=40)]
    except Exception:
        ep_singles = []
    try:
        appears_on = [album_to_dict(a) for a in artist.get_other(limit=40)]
    except Exception:
        appears_on = []
    return {
        **artist_to_dict(artist),
        "top_tracks": [track_to_dict(t) for t in tidal.get_artist_top_tracks(artist)],
        "albums": [album_to_dict(a) for a in tidal.get_artist_albums(artist)],
        "ep_singles": ep_singles,
        "appears_on": appears_on,
        "bio": bio,
        "similar": similar,
        # Stable share URL for the copy/open-in-Tidal actions in the UI.
        "share_url": getattr(artist, "share_url", None)
        or f"https://tidal.com/browse/artist/{artist.id}",
    }


@app.get("/api/artist/{artist_id}/radio")
def artist_radio(artist_id: int) -> list[dict]:
    """Tidal's 'Artist Radio' mix — a long list of tracks similar to the
    artist, mixed across their catalog. Used by the Artist page's radio
    button to seed a listening session."""
    _require_auth()
    try:
        artist = tidal.session.artist(artist_id)
        tracks = artist.get_radio(limit=100)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return [track_to_dict(t) for t in tracks]


# ---------------------------------------------------------------------------
# Videos — music videos on an artist page, played via HLS in a modal.
#
# tidalapi exposes Video metadata + `Video.get_url()` which returns an HLS
# `.m3u8` manifest URL (not a JSON envelope, not a direct MP4). We pass
# that URL straight through to the frontend; WKWebView plays HLS
# natively on macOS, and hls.js can pick up the slack on other hosts.
# ---------------------------------------------------------------------------


def _video_image_url(video, size: tuple[int, int] = (750, 500)) -> Optional[str]:
    """Build a cover URL for a Video. tidalapi's `.image(w,h)` helper
    requires one of the supported dims; we pick the sensible
    medium-large default and let any errors collapse to None."""
    try:
        return video.image(size[0], size[1])
    except Exception:
        return None


def video_to_dict(v) -> dict:
    """Serializer mirroring track_to_dict / album_to_dict shapes so the
    frontend can render videos in the same card grids as other media."""
    artist = _first(lambda: v.artist)
    return {
        "kind": "video",
        "id": str(v.id),
        "name": getattr(v, "title", None) or getattr(v, "name", "") or "",
        "duration": _first(lambda: v.duration) or 0,
        "cover": _video_image_url(v, (750, 500)) or _video_image_url(v, (480, 320)),
        "artist": (
            {"id": str(artist.id), "name": artist.name} if artist else None
        ),
        "release_date": _first(lambda: str(v.release_date) if v.release_date else None),
        "explicit": bool(_first(lambda: v.explicit)),
        "quality": _first(lambda: getattr(v, "video_quality", None)) or "",
        "share_url": _first(lambda: v.share_url),
    }


@app.get("/api/artist/{artist_id}/videos")
def artist_videos(artist_id: int, limit: int = 50) -> list[dict]:
    """Music videos an artist has released. Returns an empty list if
    the artist has no videos rather than 404'ing — keeps the UI
    simple (the Videos section just hides itself)."""
    _require_auth()
    try:
        artist = tidal.session.artist(artist_id)
        videos = artist.get_videos(limit=limit)
    except Exception:
        return []
    return [video_to_dict(v) for v in videos or []]


@app.get("/api/video/{video_id}")
def video_detail(video_id: int) -> dict:
    _require_auth()
    try:
        video = tidal.session.video(video_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return video_to_dict(video)


@app.get("/api/video/{video_id}/credits")
def video_credits(video_id: int) -> list[dict]:
    """Credits for a music video. Tries Tidal's private REST endpoint;
    falls back to empty on 404 / error so the UI hides the section."""
    _require_auth()
    try:
        resp = tidal.session.request.request(
            "GET", f"videos/{video_id}/credits", params={"limit": 50}
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    out: list[dict] = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        contributors = row.get("contributors") or []
        role = row.get("type") or ""
        if not role:
            continue
        out.append(
            {
                "role": role,
                "contributors": [
                    {
                        "name": c.get("name") or "",
                        "id": str(c["id"]) if c.get("id") is not None else None,
                    }
                    for c in contributors
                    if isinstance(c, dict) and c.get("name")
                ],
            }
        )
    return out


@app.get("/api/video/{video_id}/similar")
def video_similar(video_id: int, limit: int = 20) -> list[dict]:
    """Videos similar to a given one. Prefers Tidal's undocumented
    `videos/{id}/recommendations` endpoint; when that's unavailable we
    fall back to the artist's other videos (minus the current one) so
    the "Similar videos" panel is never empty for a valid video."""
    _require_auth()
    try:
        resp = tidal.session.request.request(
            "GET",
            f"videos/{video_id}/recommendations",
            params={"limit": limit, "offset": 0},
        )
        if resp.status_code != 404:
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items") if isinstance(data, dict) else data
            if isinstance(items, list) and items:
                out: list[dict] = []
                for row in items:
                    if not isinstance(row, dict):
                        continue
                    vid = row.get("id") or (row.get("item") or {}).get("id")
                    if not vid:
                        continue
                    try:
                        v = tidal.session.video(vid)
                        out.append(video_to_dict(v))
                    except Exception:
                        continue
                if out:
                    return out
    except Exception:
        pass

    # Fallback: other videos from the same artist.
    try:
        video = tidal.session.video(video_id)
        artist = getattr(video, "artist", None)
        if artist is None:
            return []
        siblings = tidal.session.artist(artist.id).get_videos(limit=limit + 5)
    except Exception:
        return []
    current_id = str(video_id)
    return [video_to_dict(v) for v in (siblings or []) if str(v.id) != current_id][:limit]


_VALID_VIDEO_QUALITIES = {"HIGH", "MEDIUM", "LOW", "AUDIO_ONLY"}


@app.get("/api/video/{video_id}/stream")
def video_stream(video_id: int, quality: Optional[str] = None) -> dict:
    """Return the HLS manifest URL for a video. The frontend feeds this
    into a `<video>` element — WKWebView (macOS) plays HLS natively.

    When `quality` is omitted we use the user's default from session
    config (what tidalapi's `video.get_url()` does). When passed, we
    hit the underlying `/videos/{id}/urlpostpaywall` endpoint directly
    with the requested quality so the quality-picker dropdown can swap
    streams without changing global session state.
    """
    _require_auth()
    if quality and quality.upper() not in _VALID_VIDEO_QUALITIES:
        raise HTTPException(status_code=400, detail=f"Invalid quality: {quality}")
    try:
        if quality:
            resp = tidal.session.request.request(
                "GET",
                f"videos/{video_id}/urlpostpaywall",
                params={
                    "urlusagemode": "STREAM",
                    "videoquality": quality.upper(),
                    "assetpresentation": "FULL",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            urls = payload.get("urls") if isinstance(payload, dict) else None
            url = urls[0] if isinstance(urls, list) and urls else None
        else:
            video = tidal.session.video(video_id)
            url = video.get_url()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if not url:
        raise HTTPException(status_code=404, detail="No playback URL available")
    return {"url": url}


@app.get("/api/track/{track_id}/credits")
def track_credits(track_id: int) -> list[dict]:
    """List songwriter / producer / engineer / etc. credits for a track.

    tidalapi doesn't expose credits directly, but the underlying REST API
    has a /tracks/{id}/credits endpoint that returns a list of role groups.
    Each group has a `type` (e.g. "Producer") and `contributors` (list of
    {name, id?}). We pass through that shape — it's already clean JSON.
    """
    _require_auth()
    try:
        resp = tidal.session.request.request(
            "GET", f"tracks/{track_id}/credits", params={"limit": 50}
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    # Normalize to a small shape the frontend can render without guessing.
    result: list[dict] = []
    for row in data if isinstance(data, list) else []:
        contributors = row.get("contributors") or []
        result.append(
            {
                "role": row.get("type") or "",
                "contributors": [
                    {
                        "name": c.get("name") or "",
                        "id": str(c["id"]) if c.get("id") is not None else None,
                    }
                    for c in contributors
                ],
            }
        )
    return result


@app.get("/api/album/{album_id}/credits")
def album_credits(album_id: int) -> list[dict]:
    """Per-track credits for every track on an album — the shape
    Tidal's own Album Credits view uses (a card per track, each card
    listing roles + contributors). We page through
    `/albums/{id}/items/credits?includeContributors=true` and return
    one entry per track:

        [{track_id, track_num, title, artists:[{id,name}],
          credits:[{role, contributors:[{name,id}]}]}]

    Graceful fallback: 404 / unexpected shape → `[]`, the UI hides
    the Credits button entirely.
    """
    _require_auth()
    out: list[dict] = []
    try:
        offset = 0
        limit = 100
        while True:
            resp = tidal.session.request.request(
                "GET",
                f"albums/{album_id}/items/credits",
                params={
                    "offset": offset,
                    "limit": limit,
                    "includeContributors": "true",
                    "replace": "true",
                },
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            payload = resp.json()
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list) or len(items) == 0:
                break
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                inner = entry.get("item") if isinstance(entry.get("item"), dict) else {}
                track_id = inner.get("id") or entry.get("id")
                if not track_id:
                    continue
                title = inner.get("title") or entry.get("title") or ""
                track_num = inner.get("trackNumber") or entry.get("trackNumber") or 0
                artists_raw = inner.get("artists") or entry.get("artists") or []
                artists = [
                    {
                        "id": str(a.get("id")) if a.get("id") is not None else None,
                        "name": a.get("name") or "",
                    }
                    for a in artists_raw
                    if isinstance(a, dict)
                ]
                credits_raw = entry.get("credits") or inner.get("credits") or []
                credits: list[dict] = []
                for row in credits_raw:
                    if not isinstance(row, dict):
                        continue
                    role = row.get("type") or ""
                    if not role:
                        continue
                    contributors = [
                        {
                            "name": c.get("name") or "",
                            "id": str(c["id"]) if c.get("id") is not None else None,
                        }
                        for c in (row.get("contributors") or [])
                        if isinstance(c, dict) and c.get("name")
                    ]
                    if contributors:
                        credits.append({"role": role, "contributors": contributors})
                out.append(
                    {
                        "track_id": str(track_id),
                        "track_num": int(track_num or 0),
                        "title": title,
                        "artists": artists,
                        "credits": credits,
                    }
                )
            total = payload.get("totalNumberOfItems") if isinstance(payload, dict) else None
            offset += limit
            if isinstance(total, int) and offset >= total:
                break
            if offset >= 2000:  # safety cap
                break
    except Exception:
        return []

    # Preserve track order — Tidal returns items in album order already,
    # but a client could reasonably expect trackNumber-sorted output.
    out.sort(key=lambda x: x.get("track_num") or 0)
    return out


@app.get("/api/artist/{artist_id}/credits")
def artist_credits(artist_id: int, limit: int = 20) -> list[dict]:
    """List tracks where this artist is credited in any role — the
    equivalent of Tidal's artist-page "Credits" section (writer,
    producer, engineer, featured, etc.). Returns serialized Track rows
    with their role annotated so the frontend can group by role.

    Graceful fallback: Tidal's `/artists/{id}/credits` endpoint is
    undocumented. If it 404s or the response is unexpected, we return
    an empty list and the section simply won't render.
    """
    _require_auth()
    try:
        resp = tidal.session.request.request(
            "GET", f"artists/{artist_id}/credits", params={"limit": limit, "offset": 0}
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        # Endpoint not available for this artist / region / account tier.
        # Return empty rather than 500 — the section hides itself.
        return []

    # Tidal's response shape here isn't officially documented. From the
    # observed envelope, `items` is a list of {role, track, contributors}
    # dicts. We be defensive about every field.
    raw_items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(raw_items, list):
        return []
    out: list[dict] = []
    for row in raw_items:
        if not isinstance(row, dict):
            continue
        role = row.get("role") or row.get("type") or ""
        track_data = row.get("track") or row.get("item") or {}
        track_id = track_data.get("id") if isinstance(track_data, dict) else None
        if not track_id:
            continue
        try:
            track = tidal.session.track(track_id)
        except Exception:
            continue
        out.append({**track_to_dict(track), "role": role})
    return out


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
        secs = float(seconds)
        # Reject malformed cues. A well-formed LRC line has seconds in
        # [0, 60); anything else is either a metadata tag ([ar:…]) that
        # didn't match our regex, or a corrupted line — silently skipping
        # is safer than mis-seeking the user five minutes into a track.
        if secs >= 60:
            continue
        t = int(minutes) * 60 + secs
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


@app.get("/api/mixes")
def my_mixes() -> list[dict]:
    """The user's personalized mixes (Daily Mix 1/2/3, Discovery Mix, etc.).

    `session.mixes()` returns a Page object whose categories each contain
    a list of Mix items. We flatten into a single list so the Home row
    doesn't have to care about Tidal's category grouping.
    """
    _require_auth()
    try:
        page = tidal.session.mixes()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    out: list[dict] = []
    seen: set[str] = set()
    for category in getattr(page, "categories", []) or []:
        items = getattr(category, "items", None) or []
        for item in items:
            serialized = _serialize_page_item(item)
            if not serialized or serialized.get("kind") != "mix":
                continue
            mix_id = serialized.get("id") or ""
            if not mix_id or mix_id in seen:
                continue
            seen.add(mix_id)
            out.append(serialized)
    return out


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
    _require_local_access()
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

    # Detach the reveal process so it doesn't leave zombies each time a
    # user clicks "Show in Finder". `open`/`xdg-open`/`explorer` all return
    # near-instantly, and without start_new_session + DEVNULL the parent
    # keeps defunct children around until it reaps SIGCHLD. Redirecting
    # stdio also keeps GUI error spam out of the server log.
    _popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
    }
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)], **_popen_kwargs)
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(target.parent)], **_popen_kwargs)
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", str(target)], **_popen_kwargs)
        else:
            raise HTTPException(status_code=501, detail=f"Unsupported platform: {sys.platform}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True}


_STREAMABLE_QUALITIES = {"low_96k", "low_320k", "high_lossless", "hi_res_lossless"}


def _resolve_stream_sources(
    track_id: int, quality: str
) -> tuple[list[str], Optional[str]]:
    """Return (urls, ext_hint) for a track.

    * Device-code sessions return a single progressive URL.
    * PKCE sessions return a manifest whose `urls` list is either one
      progressive URL (BTS) or many per-segment URLs (DASH). DASH
      segments are byte-concatenable — the downloader already relies on
      this — so the caller can either redirect (1 URL) or stream the
      concatenated bytes back (multi-segment).
    """
    key = (track_id, quality)
    now = time.monotonic()
    with _manifest_cache_lock:
        cached = _manifest_cache.get(key)
        if cached and (now - cached[0]) < _MANIFEST_CACHE_TTL:
            return (list(cached[1]), cached[2])

    try:
        track = tidal.session.track(track_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"{type(exc).__name__}: {exc}")

    def _fetch_once() -> tuple[list[str], Optional[str]]:
        if getattr(tidal.session, "is_pkce", False):
            stream = track.get_stream()
            manifest = stream.get_stream_manifest()
            if getattr(manifest, "is_encrypted", False):
                # Encrypted streams would need per-segment decryption
                # keys we don't have — refuse rather than stream noise.
                raise HTTPException(
                    status_code=415,
                    detail="Encrypted stream — not playable in the browser.",
                )
            urls = [u for u in list(manifest.urls or []) if u]
            ext = getattr(manifest, "file_extension", None)
            return (urls, ext)
        url = track.get_url()
        return ([url] if url else [], None)

    with downloader.quality_lock:
        original = tidal.session.config.quality
        try:
            tidal.session.config.quality = tidalapi.Quality[quality]
            try:
                urls, ext = _fetch_once()
            except HTTPException:
                raise
            except Exception as exc:
                # Tidal occasionally 5xxs or times out under load — one
                # retry turns a lot of transient 502s into successes
                # without masking real failures for long.
                logger.warning(
                    "stream resolve retry for track_id=%s quality=%s: %s: %s",
                    track_id, quality, type(exc).__name__, exc,
                )
                try:
                    urls, ext = _fetch_once()
                except HTTPException:
                    raise
                except Exception as exc2:
                    logger.error(
                        "stream resolve failed for track_id=%s quality=%s\n%s",
                        track_id, quality, traceback.format_exc(),
                    )
                    raise HTTPException(
                        status_code=502,
                        detail=f"{type(exc2).__name__}: {exc2}",
                    )
            with _manifest_cache_lock:
                _manifest_cache[key] = (time.monotonic(), list(urls), ext)
            return (urls, ext)
        finally:
            tidal.session.config.quality = original


def _resolve_stream_url(track_id: int, quality: str) -> str:
    """Single-URL variant for endpoints that redirect (e.g. /api/preview).
    Errors 415 on multi-segment manifests — callers that need to handle
    DASH should use `_resolve_stream_sources` directly."""
    import time

    key = (track_id, quality)
    now = time.monotonic()
    with _preview_cache_lock:
        cached = _preview_cache.get(key)
        if cached and (now - cached[0]) < _PREVIEW_CACHE_TTL:
            return cached[1]

    urls, _ext = _resolve_stream_sources(track_id, quality)
    if len(urls) == 0:
        raise HTTPException(status_code=502, detail="Tidal returned no stream URL")
    if len(urls) > 1:
        raise HTTPException(
            status_code=415,
            detail=(
                "This quality isn't streamable via redirect. "
                "Use /api/play which concats segments server-side."
            ),
        )
    url = urls[0]
    if not url:
        raise HTTPException(status_code=502, detail="Tidal returned no stream URL")
    with _preview_cache_lock:
        _preview_cache[key] = (now, url)
    return url


# Maps the manifest's file_extension hint to the Content-Type the browser
# needs to dispatch the concatenated bytes to the right decoder. Lossless
# via PKCE usually comes back as "flac"; m4a covers AAC in MP4.
_EXT_TO_MIME = {
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "mp3": "audio/mpeg",
    "aac": "audio/aac",
}


def _mime_for_stream(ext: Optional[str], quality: str) -> str:
    """Pick the right Content-Type for a multi-segment stream. Falls back
    to the quality tier when the manifest doesn't return a usable
    extension hint — Lossless/Max are always FLAC, Low is AAC. Serving
    the wrong MIME makes `<audio>` treat FLAC as MP3 and the scrub bar
    falls apart."""
    if ext:
        norm = ext.lower().lstrip(".")
        if norm in _EXT_TO_MIME:
            return _EXT_TO_MIME[norm]
    if quality in ("high_lossless", "hi_res_lossless"):
        return "audio/flac"
    return "audio/mp4"


def _fetch_segment(url: str) -> bytes:
    """Download a single DASH segment to memory. Segments are small
    (typically 200-800 KB) so in-memory is fine, and keeping each one
    whole lets us run the fetches in parallel and then write them to
    disk in the right order."""
    with SESSION.get(url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        chunks: list[bytes] = []
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                chunks.append(chunk)
        return b"".join(chunks)


# Bounded so we don't open a hundred parallel sockets to Tidal's CDN on a
# long track — 16 keeps the pipe saturated without being abusive.
_STREAM_FETCH_WORKERS = 16


def _probe_segment_size(url: str) -> int:
    """Probe a single segment's byte length via HEAD. Tidal's CDN
    honors HEAD on signed segment URLs and returns Content-Length, so
    this is much cheaper than a full GET — the response has no body."""
    resp = SESSION.head(url, timeout=10, allow_redirects=True)
    resp.raise_for_status()
    cl = resp.headers.get("Content-Length")
    if cl and cl.isdigit():
        return int(cl)
    # Some CDNs strip Content-Length on HEAD. Fall back to a 1-byte
    # Range GET: the Content-Range header carries the total size.
    with SESSION.get(
        url, headers={"Range": "bytes=0-0"}, stream=True, timeout=10
    ) as r:
        r.raise_for_status()
        cr = r.headers.get("Content-Range") or ""
        if "/" in cr:
            total = cr.rsplit("/", 1)[1].strip()
            if total.isdigit():
                return int(total)
        cl2 = r.headers.get("Content-Length")
        if cl2 and cl2.isdigit():
            return int(cl2)
    raise RuntimeError("no size header from segment probe")


def _probe_total_bytes(urls: list[str]) -> Optional[int]:
    """Sum segment byte sizes via parallel HEAD probes so we can set
    Content-Length on the streaming response — without it, browsers
    see duration=Infinity and the scrub bar goes dead. Runs with a
    larger pool than the fetcher because probes are tiny; the whole
    phase typically finishes in one round-trip's worth of time.

    Returns None on any probe failure; caller streams without
    Content-Length and accepts the scrub-bar degradation rather than
    failing the play outright.
    """
    if not urls:
        return None
    workers = min(_STREAM_FETCH_WORKERS * 2, max(1, len(urls)))
    try:
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="stream-probe"
        ) as pool:
            sizes = list(pool.map(_probe_segment_size, urls))
        return sum(sizes)
    except Exception as exc:
        logger.warning(
            "segment size probe failed, streaming without Content-Length: %s: %s",
            type(exc).__name__, exc,
        )
        return None


def _multisegment_suffix(ext: Optional[str], quality: str) -> str:
    if ext:
        norm = ext.lower().lstrip(".")
        if norm:
            return "." + norm
    return ".flac" if quality in ("high_lossless", "hi_res_lossless") else ".m4a"


def _build_streaming_response(
    track_id: int, quality: str, urls: list[str], ext: Optional[str]
) -> StreamingResponse:
    """Stream a multi-segment DASH track to the client in order *as*
    segments are fetched — first byte goes out after ~one segment's
    worth of latency instead of waiting for the entire track to buffer.
    Writes the full track to a temp file in parallel, then installs it
    in the stream cache on successful completion so subsequent plays
    (which hit the cache) get FileResponse with Range/seek.

    Tidal's DASH FLAC segments are byte-joinable — the first segment
    carries STREAMINFO — so a plain concat produces a valid FLAC that
    `<audio>` can decode progressively.
    """
    import tempfile

    key = (track_id, quality)
    mime = _mime_for_stream(ext, quality)
    suffix = _multisegment_suffix(ext, quality)

    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=suffix, prefix="tidal-stream-"
    )
    tmp_path = Path(tmp.name)
    tmp.close()

    # Probe segment sizes in parallel BEFORE starting full fetches so
    # we can advertise Content-Length on the response. HEAD probes are
    # tiny — the whole phase typically adds one HTTP round-trip of
    # latency before first byte (say 100-250 ms) but gives the browser
    # a finite duration so the scrub bar displays correctly.
    total_bytes = _probe_total_bytes(urls)

    workers = min(_STREAM_FETCH_WORKERS, max(1, len(urls)))
    pool = ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="stream-fetch"
    )
    # Submit all fetches up front. The pool processes 16 at a time, so
    # by the time the client reads segment N the next 15 are already
    # downloaded or in flight — yielding each .result() in order is
    # near-instant once the first segment lands.
    futures = [pool.submit(_fetch_segment, u) for u in urls]

    def gen():
        completed = False
        try:
            with open(tmp_path, "wb") as f:
                for fut in futures:
                    chunk = fut.result()
                    f.write(chunk)
                    yield chunk
            completed = True
        except Exception:
            logger.error(
                "stream segment fetch failed for track_id=%s quality=%s\n%s",
                track_id, quality, traceback.format_exc(),
            )
            raise
        finally:
            for fut in futures:
                fut.cancel()
            pool.shutdown(wait=False)
            if completed:
                _install_stream_cache(key, tmp_path, mime)
            else:
                # Client disconnected mid-stream, or a segment fetch
                # errored — discard the partial tempfile.
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

    headers = {"Cache-Control": "no-store, private"}
    if total_bytes is not None:
        # Preserves a finite `<audio>.duration` and drives the scrub
        # bar on first play. We don't set Accept-Ranges: bytes — this
        # response can't honor Range, so advertising range support
        # would let the browser issue seeks we can't serve; the scrub
        # bar still displays, seek just restarts from 0 until the
        # cache warms (subsequent plays go through FileResponse).
        headers["Content-Length"] = str(total_bytes)
    return StreamingResponse(
        gen(),
        media_type=mime,
        headers=headers,
    )


def _pick_stream_quality(requested: Optional[str]) -> str:
    """Resolve the effective streaming quality. All four PKCE-reachable
    tiers (low_96k / low_320k / high_lossless / hi_res_lossless) are
    streamable in-browser via the DASH segment-concat path. Anything
    else (legacy/unknown token) falls back to high_lossless. None falls
    back to AAC 320 which every browser can play without question and
    is the cheapest bandwidth default."""
    if not requested:
        return "low_320k"
    q = requested.lower()
    if q not in _STREAMABLE_QUALITIES:
        return "high_lossless"
    return q


@app.get("/api/preview/{track_id}")
def preview_stream(track_id: int, quality: Optional[str] = None) -> RedirectResponse:
    """Redirect to a Tidal stream URL at the requested quality. Defaults
    to AAC 320 if no quality is supplied (every browser plays it). The
    URL is signed and short-lived, so we send no-store to keep proxies
    from caching the redirect past the signed URL's TTL."""
    _require_auth()
    q = _pick_stream_quality(quality)
    return RedirectResponse(
        _resolve_stream_url(track_id, q),
        status_code=307,
        headers={"Cache-Control": "no-store, private"},
    )


@app.get("/api/play/{track_id}")
def play_track(track_id: int, quality: Optional[str] = None):
    """Unified playback endpoint: serves the local file at full quality if
    we have one for this Tidal track, otherwise falls back to the Tidal
    stream. FileResponse emits Range-capable headers so the browser can
    seek without buffering the whole file. Accepts an optional `quality`
    for the streaming path.

    For PKCE sessions, Lossless can come back as a multi-segment DASH
    manifest. Rather than rejecting it (which would strand the user at
    320k AAC), we concatenate the segments on the fly — Tidal's FLAC
    DASH segments are byte-joinable into a valid single file, same
    trick the downloader uses. Seek is unavailable while buffering
    since we're streaming, not serving a known-length file.
    """
    # Local files are playable without auth when offline mode is on;
    # streaming still requires a live Tidal session.
    _require_local_access()
    local = local_index.get(str(track_id))
    if local is not None:
        return FileResponse(str(local))
    if not _is_logged_in():
        raise HTTPException(status_code=401, detail="Not authenticated")
    q = _pick_stream_quality(quality)

    # Fast path: if a recent play already buffered this track+quality,
    # serve the cached file with FileResponse — Content-Length and
    # Range/seek work, and we skip both the manifest fetch and the
    # segment downloads entirely. Browsers fire lots of Range requests
    # (every scrub-seek, every pause/resume), so this is the hot path
    # after a track's been played once.
    cache_hit = _lookup_stream_cache((track_id, q))
    if cache_hit is not None:
        path, mime = cache_hit
        return FileResponse(
            str(path),
            media_type=mime,
            headers={"Cache-Control": "no-store, private"},
        )

    urls, ext = _resolve_stream_sources(track_id, q)
    if not urls:
        raise HTTPException(status_code=502, detail="Tidal returned no stream URL")
    if len(urls) == 1:
        # Single URL — redirect so the browser gets Range/seek straight
        # from the Tidal CDN. Also warm the preview cache so a repeat
        # request within the TTL skips the session lock.
        with _preview_cache_lock:
            _preview_cache[(track_id, q)] = (time.monotonic(), urls[0])
        return RedirectResponse(
            urls[0],
            status_code=307,
            headers={"Cache-Control": "no-store, private"},
        )
    # Multi-segment, first play — stream segments to the client as they
    # arrive (fast first-byte), and tee to a temp file that we install
    # in the cache on successful completion so the NEXT play hits the
    # seekable FileResponse path above.
    return _build_streaming_response(track_id, q, urls, ext)


@app.get("/api/downloaded")
def downloaded_ids() -> dict:
    """Return the set of Tidal track IDs the local index knows about.

    Frontend calls this once on boot and then updates live via the
    `downloaded` SSE event type.
    """
    _require_local_access()
    return {"ids": sorted(local_index.ids())}


_QUALITY_ORDER_SERVER = [
    "low_96k",
    "low_320k",
    "high_lossless",
    "hi_res_lossless",
]


def _clamp_quality_to_subscription(requested: Optional[str]) -> Optional[str]:
    """Downgrade `requested` to the highest tier the account actually
    supports. Without this, a user whose UI offers 'Max' (e.g. a stale
    cached list from before the subscription filter shipped) would hit
    an inevitable 401 from Tidal's /urlpostpaywall endpoint. Silent
    downgrade is much better than a cryptic auth error the user can't
    do anything about.
    """
    if not requested:
        return requested
    max_quality = tidal.get_max_quality()
    if not max_quality:
        return requested
    try:
        req_idx = _QUALITY_ORDER_SERVER.index(requested)
        max_idx = _QUALITY_ORDER_SERVER.index(max_quality)
    except ValueError:
        return requested
    if req_idx <= max_idx:
        return requested
    print(
        f"[quality] clamping {requested!r} -> {max_quality!r} "
        "(subscription ceiling)",
        file=sys.stderr,
        flush=True,
    )
    return max_quality


def _resolve_quality(req_quality: Optional[str]) -> Optional[str]:
    """Resolve the effective per-item quality.

    Explicit request wins. Otherwise fall back to settings.quality, so the
    user's Settings choice actually matters when they pick "Use default".
    Returning None means "use whatever the session has" (safety net).

    Final step clamps to the account's subscription tier.
    """
    if req_quality:
        return _clamp_quality_to_subscription(req_quality)
    fallback = getattr(settings, "quality", None)
    if fallback and fallback in tidalapi.Quality.__members__:
        return _clamp_quality_to_subscription(fallback)
    return None


def _looks_like_401(exc: Exception) -> bool:
    """Best-effort detection of a Tidal auth error. tidalapi wraps these
    as requests.HTTPError with .response.status_code == 401, or sometimes
    surfaces them as RuntimeError whose str() contains '401'."""
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) in (401, 403):
        return True
    msg = str(exc)
    return "401" in msg or "Unauthorized" in msg


def _fetch_tidal_object(kind: str, obj_id: str):
    """Fetch a track/album/playlist, retrying once on auth failure.

    tidalapi only auto-refreshes when the 401 body contains the exact
    string 'The token has expired.' — Tidal's real responses don't
    always match, so a stale access token surfaces as a raw 401 to the
    user. We explicitly force a refresh and retry once before giving up.
    """
    def _call():
        if kind == "track":
            return tidal.session.track(int(obj_id))
        if kind == "album":
            return tidal.session.album(int(obj_id))
        if kind == "playlist":
            return tidal.session.playlist(obj_id)
        raise HTTPException(status_code=400, detail=f"Unsupported kind: {kind}")

    try:
        return _call()
    except HTTPException:
        raise
    except Exception as exc:
        print(
            f"[download] {kind}/{obj_id} initial fetch failed: {exc!r}",
            file=sys.stderr,
            flush=True,
        )
        if _looks_like_401(exc):
            if tidal.force_refresh():
                _invalidate_auth_cache()
                return _call()
            # Refresh didn't work — the refresh token itself is dead.
            # Invalidate the cached auth state so the next /auth/status
            # call returns logged_in=false and the frontend bounces to
            # the Login screen automatically.
            _invalidate_auth_cache()
            raise HTTPException(
                status_code=401,
                detail="Tidal session expired. Please log out and log back in.",
            )
        raise


@app.post("/api/downloads")
def enqueue_download(req: DownloadRequest) -> dict:
    _require_auth()
    resolved_quality = _resolve_quality(req.quality)
    print(
        f"[api/downloads] enqueue kind={req.kind} id={req.id} "
        f"req_quality={req.quality!r} resolved={resolved_quality!r}",
        file=sys.stderr,
        flush=True,
    )
    try:
        obj = _fetch_tidal_object(req.kind, req.id)
    except HTTPException:
        raise
    except Exception as exc:
        print(
            f"[api/downloads] _fetch_tidal_object FAILED kind={req.kind} "
            f"id={req.id} exc={exc!r}",
            file=sys.stderr,
            flush=True,
        )
        raise HTTPException(status_code=404, detail=str(exc))
    downloader.submit_object(obj, req.kind, quality=resolved_quality)
    return {"ok": True}


class BulkDownloadItem(BaseModel):
    kind: str  # track | album | playlist
    id: str


class BulkDownloadRequest(BaseModel):
    items: list[BulkDownloadItem]
    quality: Optional[str] = None


@app.post("/api/downloads/bulk")
def enqueue_bulk(req: BulkDownloadRequest) -> dict:
    """Enqueue many items without blocking the request thread.

    Each item requires a Tidal lookup (e.g. `session.track(id)`), which is
    a synchronous HTTP round-trip. For a 1000-track "download all liked
    songs" batch, doing those lookups serially in the request handler
    would hold the HTTP connection open for minutes and pin a FastAPI
    worker thread. Instead we hand the list to a background thread that
    submits items as each lookup completes; the downloader is already
    async-friendly via the SSE broker so the UI sees items appear live.
    """
    _require_auth()
    if not req.items:
        return {"submitted": 0}
    quality = _resolve_quality(req.quality)
    items_snapshot = list(req.items)  # copy before leaving the request scope

    def _enqueue_batch() -> None:
        for item in items_snapshot:
            try:
                obj = _fetch_tidal_object(item.kind, item.id)
                downloader.submit_object(obj, item.kind, quality=quality)
            except Exception:
                # Individual failures don't abort the batch. The download
                # never materializes, so the user simply sees fewer items
                # in the queue than they asked for.
                continue

    _BULK_EXECUTOR.submit(_enqueue_batch)
    return {"submitted": len(items_snapshot)}


@app.get("/api/downloads")
def list_downloads() -> list[dict]:
    _require_local_access()
    return [item_to_dict(i) for i in broker.snapshot()]


class RetryRequest(BaseModel):
    quality: Optional[str] = None


@app.post("/api/downloads/{item_id}/retry")
def retry_download(
    item_id: str,
    req: RetryRequest = Body(default_factory=RetryRequest),
) -> dict:
    """Retry a failed item. `quality` in the body optionally overrides the
    original item's quality — useful when a hi-res download failed because
    of a subscription tier and the user wants to step down. Body itself is
    optional: `Body(default_factory=...)` accepts an empty POST as well as
    `{"quality": "high_lossless"}`."""
    _require_auth()
    item = broker.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    downloader.retry(item, quality=_resolve_quality(req.quality))
    return {"ok": True}


@app.delete("/api/downloads/completed")
def clear_completed() -> dict:
    _require_local_access()
    broker.clear_completed()
    return {"ok": True}


@app.delete("/api/downloads/active")
def cancel_all_active() -> dict:
    """Cancel every non-terminal item in one shot. Used by the 'Cancel
    all' button on the Downloads page when the user wants to abandon a
    large queue without clicking each row individually."""
    _require_local_access()
    from app.downloader import DownloadStatus as _DS

    terminal = {_DS.COMPLETE, _DS.FAILED}
    targets = [i.item_id for i in broker.snapshot() if i.status not in terminal]
    for iid in targets:
        downloader.cancel(iid)
    return {"cancelled": len(targets)}


@app.delete("/api/downloads/{item_id}")
def cancel_download(item_id: str) -> dict:
    """Cancel a single in-flight or pending download. No-op (still 200)
    if the item is already terminal or unknown — the UI can fire this
    optimistically without pre-checking."""
    _require_local_access()
    downloader.cancel(item_id)
    return {"ok": True}


@app.get("/api/downloads/state")
def download_state() -> dict:
    _require_local_access()
    return {"paused": downloader.paused}


@app.post("/api/downloads/pause")
def pause_downloads() -> dict:
    _require_auth()
    downloader.pause()
    return {"paused": True}


@app.post("/api/downloads/resume")
def resume_downloads() -> dict:
    _require_auth()
    downloader.resume()
    return {"paused": False}


_AUDIO_EXTENSIONS = {".flac", ".m4a", ".mp4", ".mp3", ".ogg", ".opus", ".aac", ".wav"}


@app.get("/api/downloads/stats")
def download_stats() -> dict:
    """Aggregate size + file count of audio files under the configured
    output directory. Used by the Downloads page to show a "4.2 GB,
    312 files" header so the user can see at a glance how much they've
    pulled down.

    Walks the tree lazily with ``os.scandir`` (faster than rglob for
    large libraries) and only counts files with audio extensions to
    avoid inflating totals with stray cover art or .part files."""
    _require_local_access()
    import os

    root = Path(settings.output_dir).expanduser()
    total_bytes = 0
    file_count = 0
    if root.is_dir():
        # Iterative DFS — recursion would blow the stack on deep trees
        # and rglob() is ~4x slower on Windows in practice.
        stack: list[Path] = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                ext = os.path.splitext(entry.name)[1].lower()
                                if ext in _AUDIO_EXTENSIONS:
                                    total_bytes += entry.stat().st_size
                                    file_count += 1
                        except OSError:
                            continue
            except OSError:
                continue
    return {
        "output_dir": str(root),
        "total_bytes": total_bytes,
        "file_count": file_count,
    }


@app.get("/api/downloads/stream")
async def downloads_stream(request: Request) -> EventSourceResponse:
    _require_local_access()
    q = await broker.subscribe()

    async def event_gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "1"}
                    continue
                # Slow-consumer fallback: if the broker couldn't fit a
                # state-changing event, it drained the queue and pushed
                # this marker. Close the stream so EventSource reconnects
                # and gets a fresh reset snapshot via subscribe().
                if isinstance(payload, dict) and payload.get("type") == "__desync__":
                    break
                yield {"event": "download", "data": json.dumps(payload)}
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


def _clamp_settings_to_subscription(max_quality: Optional[str]) -> None:
    """If the saved default quality exceeds what the account can stream,
    downgrade it to the ceiling and persist. Called whenever we freshly
    learn the subscription tier (on /api/qualities or after login) so a
    user who had 'Max' selected before their trial ended — or who
    defaults to hi_res_lossless but is on the Lossless tier — doesn't
    keep seeing 'Use default (Max)' when Max is unreachable.
    """
    global settings
    if not max_quality:
        return
    try:
        ceiling_idx = _QUALITY_ORDER_SERVER.index(max_quality)
    except ValueError:
        return
    current = getattr(settings, "quality", None)
    if current not in _QUALITY_ORDER_SERVER:
        return
    if _QUALITY_ORDER_SERVER.index(current) <= ceiling_idx:
        return
    with _settings_lock:
        data = asdict(settings)
        data["quality"] = max_quality
        new_settings = Settings(**data)
        save_settings(new_settings)
        settings = new_settings
        downloader.settings = new_settings
    _apply_settings_quality(new_settings)
    print(
        f"[settings] clamped default quality {current!r} -> {max_quality!r} "
        "(was above subscription ceiling)",
        file=sys.stderr,
        flush=True,
    )


@app.get("/api/qualities")
def list_qualities() -> list[dict]:
    _require_local_access()
    # Filter to the qualities the account can actually stream. Without
    # this, the UI offers e.g. "Max (hi-res)" to HiFi-tier users and
    # every download at that quality 401s. If the subscription lookup
    # fails (network, stale token), fall back to the full list rather
    # than hide options the user might actually have.
    max_quality = tidal.get_max_quality()
    if not max_quality:
        print(
            "[api/qualities] max_quality unknown — returning full list",
            file=sys.stderr,
            flush=True,
        )
        return QUALITIES
    # Auto-downgrade the saved default so the UI's "Use default" label
    # always reflects something the user can actually stream.
    _clamp_settings_to_subscription(max_quality)
    try:
        ceiling = _QUALITY_ORDER_SERVER.index(max_quality)
    except ValueError:
        print(
            f"[api/qualities] unrecognized max_quality {max_quality!r} — "
            "returning full list",
            file=sys.stderr,
            flush=True,
        )
        return QUALITIES
    allowed = set(_QUALITY_ORDER_SERVER[: ceiling + 1])
    filtered = [q for q in QUALITIES if q["value"] in allowed]
    print(
        f"[api/qualities] max={max_quality} allowed={sorted(allowed)} "
        f"returned={[q['value'] for q in filtered]}",
        file=sys.stderr,
        flush=True,
    )
    return filtered


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class SettingsPayload(BaseModel):
    output_dir: Optional[str] = None
    quality: Optional[str] = None
    filename_template: Optional[str] = None
    create_album_folders: Optional[bool] = None
    skip_existing: Optional[bool] = None
    concurrent_downloads: Optional[int] = None
    offline_mode: Optional[bool] = None
    notify_on_complete: Optional[bool] = None


@app.get("/api/settings")
def get_settings() -> dict:
    _require_local_access()
    return asdict(settings)


@app.put("/api/settings")
def update_settings(payload: SettingsPayload) -> dict:
    _require_local_access()
    global settings
    patch = payload.model_dump(exclude_unset=True)

    # Validate quality before writing: an unknown value would silently
    # disable per-item quality resolution later.
    if "quality" in patch and patch["quality"] not in tidalapi.Quality.__members__:
        raise HTTPException(status_code=400, detail=f"Unknown quality: {patch['quality']}")
    # Validate output_dir: must be an existing writable directory. Without
    # this, a PUT with `{"output_dir": "/"}` would quietly persist and all
    # future downloads would either fail or escape the intended sandbox.
    if "output_dir" in patch:
        raw = patch["output_dir"]
        if not isinstance(raw, str) or not raw.strip():
            raise HTTPException(status_code=400, detail="output_dir must be a non-empty string")
        resolved = Path(raw).expanduser()
        try:
            resolved = resolved.resolve(strict=True)
        except (FileNotFoundError, RuntimeError):
            raise HTTPException(status_code=400, detail=f"output_dir does not exist: {raw}")
        if not resolved.is_dir():
            raise HTTPException(status_code=400, detail=f"output_dir is not a directory: {raw}")
        # Guard against obviously-dangerous paths. Writing album folders
        # into root or system bin dirs is never what the user wants.
        forbidden = {Path("/"), Path("/etc"), Path("/bin"), Path("/usr"), Path("/sbin"), Path("/var")}
        if resolved in forbidden:
            raise HTTPException(status_code=400, detail=f"output_dir not allowed: {resolved}")
        # Writability check. A read-only path would silently persist and
        # every future download would fail with an ambiguous OS error —
        # better to reject at Save time.
        import os as _os
        if not _os.access(str(resolved), _os.W_OK):
            raise HTTPException(
                status_code=400, detail=f"output_dir is not writable: {resolved}"
            )
        patch["output_dir"] = str(resolved)
    # Clamp concurrent_downloads to [1, MAX_WORKER_THREADS] so the UI
    # can't push past the worker-pool ceiling.
    if "concurrent_downloads" in patch:
        try:
            n = int(patch["concurrent_downloads"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="concurrent_downloads must be an integer")
        from app.downloader import MAX_WORKER_THREADS as _MAX
        if n < 1 or n > _MAX:
            raise HTTPException(
                status_code=400,
                detail=f"concurrent_downloads must be between 1 and {_MAX}",
            )
        patch["concurrent_downloads"] = n

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
    downloader.gate.set_limit(new_settings.concurrent_downloads)
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


_CONTEXT_KIND_MAP = {
    "ALBUM": "album",
    "TRACK": "track",
    "PLAYLIST": "playlist",
    "MIX": "mix",
    "ARTIST": "artist",
}


def _cover_url_from_uuid(uuid: Optional[str], size: int = 160) -> Optional[str]:
    """Build an image URL from a bare Tidal UUID (as shipped in header.data.cover)."""
    if not uuid or not isinstance(uuid, str):
        return None
    return f"https://resources.tidal.com/images/{uuid.replace('-', '/')}/{size}x{size}.jpg"


def _header_context(header: dict) -> Optional[dict]:
    """Turn a V2 category header dict into a clickable entity ref so the UI
    can render an album/artist/playlist thumbnail next to "Because you liked"."""
    data = header.get("data") or {}
    htype = (header.get("type") or "").upper()
    kind = _CONTEXT_KIND_MAP.get(htype)
    if not kind:
        return None
    ent_id = data.get("id") or data.get("uuid")
    if ent_id is None:
        return None
    if kind == "artist":
        title = data.get("name") or ""
        cover = _cover_url_from_uuid(data.get("picture"))
    else:
        title = data.get("title") or ""
        cover = _cover_url_from_uuid(data.get("cover") or data.get("image"))
    return {"kind": kind, "id": str(ent_id), "title": title, "cover": cover}


def _fetch_v2_view_all(path: str) -> dict:
    """Fetch a V2 "view-all" path (e.g.
    ``home/pages/NEW_ALBUM_SUGGESTIONS/view-all``) and serialize it as
    a single-category Page so the frontend can render it with PageView.

    The view-all response shape is different from a regular Page — it's
    a flat {"items": [...]} where each item has a ``type`` like
    "ALBUM"/"TRACK"/"ARTIST"/"PLAYLIST"/"MIX" and a ``data`` payload.
    tidalapi's Page parser expects category-typed rows, so we map items
    ourselves using session.parse_* helpers and emit one synthetic
    "HorizontalList" row.

    Also: tidalapi's basic_request auto-injects ``sessionId`` and
    ``limit=1000`` — that combination trips a 400 (subStatus 1002) on
    these endpoints, so we drive ``request_session`` directly with just
    the query params Tidal's web client sends."""
    from urllib.parse import urljoin

    session = tidal.session
    url = urljoin(session.config.api_v2_location, path)
    headers = {
        "x-tidal-client-version": session.request.client_version,
        "User-Agent": session.request.user_agent,
        "Authorization": f"{session.token_type} {session.access_token}",
    }
    params = {
        "countryCode": session.country_code,
        "deviceType": "BROWSER",
        "locale": session.locale,
        "platform": "WEB",
    }
    resp = session.request_session.request("GET", url, params=params, headers=headers)
    resp.raise_for_status()
    body = resp.json()

    raw_items = body.get("items") or []
    title = body.get("title") or ""
    out: list[dict] = []
    for entry in raw_items:
        item_type = (entry.get("type") or "").upper()
        data = entry.get("data") or entry
        try:
            if item_type == "TRACK":
                obj = session.parse_track(data)
            elif item_type == "ALBUM":
                obj = session.parse_album(data)
            elif item_type == "ARTIST":
                obj = session.parse_artist(data)
            elif item_type == "PLAYLIST":
                obj = session.parse_playlist(data)
            elif item_type == "MIX":
                obj = session.parse_mix(data)
            else:
                continue
        except Exception:
            continue
        serialized = _serialize_page_item(obj)
        if serialized:
            out.append(serialized)

    return {
        "title": title,
        "categories": [
            {"type": "HorizontalList", "title": "", "items": out},
        ],
    }


def _category_view_all_path(cat) -> Optional[str]:
    """Return the api_path for this category's "View more" page, if any.

    tidalapi's `More.parse` already handles both the `viewAll`
    (bare-path) and `showMore` (dict with apiPath) shapes; we just read
    the parsed attribute off the category. V1 categories use `.more`
    instead of `._more`, so fall back."""
    more = getattr(cat, "_more", None) or getattr(cat, "more", None)
    if not more:
        return None
    api_path = getattr(more, "api_path", None)
    if isinstance(api_path, str) and api_path:
        return api_path
    return None


def _serialize_page(page) -> dict:
    categories: list[dict] = []
    for cat in getattr(page, "categories", []) or []:
        cat_type = type(cat).__name__
        if cat_type == "TextBlock":
            # Editorial copy — not useful to render in our UI.
            continue
        title = getattr(cat, "title", None) or ""
        # Tidal attaches the related entity name ("Daft Punk - Get Lucky"
        # for "Because you liked", an artist name for "Because you
        # listened to", etc.) in either `subtitle` (V2 categories) or
        # `description` (some V1 categories). tidalapi's V2 _parse_base
        # defaults description to title, so only keep it when it's
        # distinct and non-empty — otherwise the UI would show the same
        # string twice.
        subtitle_raw = getattr(cat, "subtitle", None) or ""
        description_raw = getattr(cat, "description", None) or ""
        subtitle = ""
        for candidate in (subtitle_raw, description_raw):
            if candidate and candidate != title:
                subtitle = candidate
                break
        raw_items = list(getattr(cat, "items", []) or [])
        items = [d for d in (_serialize_page_item(i) for i in raw_items) if d]
        if not items:
            continue
        entry: dict = {"type": cat_type, "title": title, "items": items}
        if subtitle:
            entry["subtitle"] = subtitle
        raw_header = getattr(cat, "_raw_header", None)
        if isinstance(raw_header, dict):
            ctx = _header_context(raw_header)
            if ctx:
                entry["context"] = ctx
        view_all_path = _category_view_all_path(cat)
        if view_all_path:
            entry["viewAllPath"] = view_all_path
        categories.append(entry)
    return {"title": getattr(page, "title", "") or "", "categories": categories}


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
    """Drill into any api_path returned by Tidal.

    Tidal emits two shapes of "view more" paths:
      - V1 pages (``pages/genre_hip_hop``, ``pages/home``) — live under
        ``api.tidal.com/v1/`` and parse into the classic row-based
        Page shape. tidalapi's ``page.get`` handles these.
      - V2 view-alls (``home/pages/NEW_ALBUM_SUGGESTIONS/view-all``,
        ``home/feed/static``) — live under ``api.tidal.com/v2/`` and
        return the items-array V2 shape. tidalapi has no helper for
        arbitrary V2 paths, so we do the request ourselves and hand
        the JSON to the same Page parser.

    We distinguish by prefix: ``pages/…`` → V1; everything else → V2.
    POST (not GET) so path slashes don't need URL encoding."""
    _require_auth()
    path = req.path.strip().lstrip("/")
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    if "://" in path:
        raise HTTPException(status_code=400, detail="path must be a relative api_path")
    try:
        if path.startswith("pages/"):
            page = tidal.session.page.get(path)
        else:
            # V2 view-all path — returns a JSON dict directly, no Page
            # object to serialize.
            return _fetch_v2_view_all(path)
    except Exception as exc:
        # Tidal's 400/404 bodies usually contain a JSON error that tells
        # us exactly which parameter it's rejecting — the HTTPError
        # itself only says "400 Bad Request". Grab the latest response
        # tidalapi cached and log both.
        body = ""
        try:
            resp = tidal.session.request.latest_err_response
            if resp is not None:
                body = (resp.text or "")[:500]
        except Exception:
            pass
        print(
            f"[page/resolve] failed path={path!r}: {exc} | body={body!r}",
            flush=True,
        )
        raise HTTPException(status_code=502, detail=f"{path}: {exc} | {body}")
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


class BulkFavoriteRequest(BaseModel):
    kind: str
    ids: list[str]
    add: bool = True


@app.post("/api/favorites/bulk")
def favorites_bulk(req: BulkFavoriteRequest) -> dict:
    """Add or remove many favorites in one call.

    Runs sequentially on a background thread so the client isn't blocked
    AND we don't fan out N parallel requests to Tidal (rate-limit risk).
    Returns immediately with the submitted count — success/failure is
    best-effort for the batch.
    """
    _require_auth()
    if req.kind not in FAVORITE_KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown kind: {req.kind}")
    ids = list(req.ids)

    def _run() -> None:
        for obj_id in ids:
            try:
                tidal.favorite(req.kind, obj_id, add=req.add)
            except Exception:
                continue

    _BULK_EXECUTOR.submit(_run)
    return {"submitted": len(ids)}


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


# ---------------------------------------------------------------------------
# Feed — aggregated new releases from the user's favorite artists, combined
# with Tidal's editorial For You page. The goal is to mirror the useful
# subset of Tidal's feed surface for a download-focused client.
# ---------------------------------------------------------------------------


_FEED_WINDOW_DAYS = 90
_FEED_TTL_SEC = 900.0  # 15 minutes — releases don't come out that often.
_FEED_MAX_ITEMS = 300
_feed_cache: dict[str, Any] = {"at": 0.0, "value": None}
_feed_lock = threading.Lock()


def _album_release_at(album) -> Optional[datetime]:
    """Return the most meaningful release timestamp for an album —
    `streamStartDate` takes precedence over the original `releaseDate`
    so that a late-added catalog release surfaces in the feed when it
    actually landed on Tidal."""
    for attr in ("tidal_release_date", "release_date"):
        value = getattr(album, attr, None)
        if value is not None:
            return value
    return None


def _build_feed() -> list[dict]:
    """Fan-out to every favorite + watched artist, collect recent albums,
    dedupe, and return newest-first. Runs on a short-lived thread pool
    so a 50-favorite library doesn't serialize 50 network calls."""
    cutoff = datetime.now() - timedelta(days=_FEED_WINDOW_DAYS)

    artist_ids: set[str] = set()
    try:
        for a in tidal.get_favorite_artists():
            aid = getattr(a, "id", None)
            if aid:
                artist_ids.add(str(aid))
    except Exception:
        pass
    if not artist_ids:
        return []

    def _fetch(aid: str) -> list:
        try:
            artist = tidal.session.artist(int(aid))
            # get_artist_releases includes albums + EPs + singles. The
            # full Tidal client shows all three on its new-releases
            # surface; using get_albums alone silently drops singles.
            return tidal.get_artist_releases(artist, limit=30)
        except Exception:
            return []

    # Cap fan-out: tidalapi isn't documented thread-safe so keep it modest.
    seen_album_ids: set[str] = set()
    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for albums in pool.map(_fetch, artist_ids):
            for album in albums:
                aid = str(getattr(album, "id", "") or "")
                if not aid or aid in seen_album_ids:
                    continue
                seen_album_ids.add(aid)
                released = _album_release_at(album)
                if released is None:
                    continue
                # Normalize to naive for comparison with our naive cutoff.
                released_cmp = released.replace(tzinfo=None) if released.tzinfo else released
                if released_cmp < cutoff:
                    continue
                entry = {
                    **album_to_dict(album),
                    "released_at": released_cmp.isoformat(),
                }
                items.append(entry)

    items.sort(key=lambda it: it.get("released_at") or "", reverse=True)
    return items[:_FEED_MAX_ITEMS]


def _build_feed_editorial() -> Optional[dict]:
    """Fetch Tidal's personalized 'For You' page and serialize it.

    The real Tidal client's feed surface is a mix of (a) new releases from
    artists you follow (our curated items above) and (b) Tidal's editorial
    recommendations. Exposing the For You page here lets the UI render
    Tidal's own sections below the curated ones so the user sees the
    same content they'd see in the official app.
    """
    try:
        page = tidal.session.for_you()
        return _serialize_page(page)
    except Exception as exc:
        print(
            f"[feed] for_you fetch failed: {exc!r}",
            file=sys.stderr,
            flush=True,
        )
        return None


@app.get("/api/feed")
def feed() -> dict:
    """Recent releases from the user's favorite + watched artists, plus
    Tidal's editorial For You page below."""
    _require_auth()
    with _feed_lock:
        cached = _feed_cache["value"]
        if cached is not None and (time.monotonic() - _feed_cache["at"]) < _FEED_TTL_SEC:
            return cached
    items = _build_feed()
    editorial = _build_feed_editorial()
    payload = {"items": items, "editorial": editorial}
    with _feed_lock:
        _feed_cache["at"] = time.monotonic()
        _feed_cache["value"] = payload
    return payload


# ---------------------------------------------------------------------------
# Playlist folders — minimal CRUD. tidalapi exposes create_folder + Folder
# methods but doesn't have a clean "list all folders" surface, so the
# sidebar doesn't render them yet. These endpoints let the UI create/rename/
# delete folders once that listing gap is closed (probably via a raw API
# call when we have a live account to test against).
# ---------------------------------------------------------------------------


class CreateFolderRequest(BaseModel):
    title: str
    parent_id: str = "root"


@app.post("/api/folders")
def create_folder(req: CreateFolderRequest) -> dict:
    _require_auth()
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title required")
    try:
        folder = tidal.session.user.create_folder(title, req.parent_id or "root")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"id": folder.id, "name": folder.name}


def _get_folder(folder_id: str):
    try:
        # `user.folder` is the ROOT folder; for any other id we reach into
        # tidalapi's Folder constructor via the public factory.
        if folder_id == "root":
            return tidal.session.user.folder
        return tidalapi.playlist.Folder(tidal.session, folder_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))


class RenameFolderRequest(BaseModel):
    title: str


@app.put("/api/folders/{folder_id}")
def rename_folder(folder_id: str, req: RenameFolderRequest) -> dict:
    _require_auth()
    folder = _get_folder(folder_id)
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title required")
    try:
        folder.rename(title)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: str) -> dict:
    _require_auth()
    folder = _get_folder(folder_id)
    try:
        folder.remove()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


class AddPlaylistsToFolderRequest(BaseModel):
    playlist_ids: list[str]


@app.post("/api/folders/{folder_id}/playlists")
def add_playlists_to_folder(folder_id: str, req: AddPlaylistsToFolderRequest) -> dict:
    _require_auth()
    folder = _get_folder(folder_id)
    try:
        folder.add_items(req.playlist_ids)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, "added": len(req.playlist_ids)}


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
    _require_auth()
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="Only https URLs allowed")
    if parsed.username or parsed.password:
        # URLs with embedded credentials are a classic SSRF bypass — the
        # allowlist check against parsed.hostname can be sidestepped by some
        # parsers. We reject them outright; Tidal never includes userinfo.
        raise HTTPException(status_code=400, detail="URL must not contain userinfo")
    if parsed.hostname not in ALLOWED_IMAGE_HOSTS:
        raise HTTPException(status_code=403, detail=f"Host not allowed: {parsed.hostname}")
    try:
        # allow_redirects=False — a redirect from a Tidal CDN to an internal
        # host would otherwise be followed by requests and turn this into an
        # SSRF probe. Tidal covers are direct URLs so this should never fire
        # on legitimate traffic.
        with SESSION.get(url, timeout=10, stream=True, allow_redirects=False) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                raise HTTPException(status_code=502, detail="Upstream redirect refused")
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


# ---------------------------------------------------------------------------
# Static frontend (packaged builds)
#
# When a Vite build exists at <resource_dir>/web/dist, serve it as the
# frontend: hashed assets under /assets (with far-future caching) and an
# SPA fallback that returns index.html for any unmatched GET so React
# Router can handle client-side routes like /search/foo or /settings.
#
# Registered AFTER every /api/* route above — order matters because the
# fallback matches {full_path:path} and would otherwise shadow real API
# endpoints. In dev (vite serves :5173 directly) the dist/ dir doesn't
# exist and this whole block no-ops.
# ---------------------------------------------------------------------------


_DIST_DIR = bundled_resource_dir() / "web" / "dist"

if _DIST_DIR.is_dir():
    _ASSETS_DIR = _DIST_DIR / "assets"
    if _ASSETS_DIR.is_dir():
        # Vite emits hashed filenames under /assets — safe to cache forever.
        app.mount(
            "/assets",
            StaticFiles(directory=_ASSETS_DIR),
            name="assets",
        )

    _INDEX_HTML = _DIST_DIR / "index.html"
    _DIST_ROOT_RESOLVED = _DIST_DIR.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str) -> Response:
        # /api and /assets are already routed above; anything landing here
        # is either a top-level static file (favicon.ico, robots.txt) or
        # a client-side route. Resolve-and-check keeps path traversal
        # (`..`) from escaping _DIST_DIR even if Starlette's routing
        # normalization misses something.
        # Unknown /api/* paths should 404, not silently serve the SPA shell —
        # that would make typos in API clients very confusing to debug.
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        if full_path:
            candidate = (_DIST_DIR / full_path).resolve()
            try:
                candidate.relative_to(_DIST_ROOT_RESOLVED)
            except ValueError:
                candidate = None
            if candidate and candidate.is_file():
                return FileResponse(candidate)
        if _INDEX_HTML.is_file():
            # no-store on index.html so a user who updates the app doesn't
            # get stuck on a cached shell pointing at stale hashed bundles.
            return FileResponse(
                _INDEX_HTML, headers={"Cache-Control": "no-store"}
            )
        raise HTTPException(status_code=404)
