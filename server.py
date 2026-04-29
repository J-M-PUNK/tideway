"""FastAPI backend for the Tideway web UI.

Wraps the existing `app/` package (TidalClient, Downloader, Settings) and
exposes it over HTTP + SSE so a React frontend can drive it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import subprocess
import tempfile
import webbrowser
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Generator, Optional
from urllib.parse import quote, urljoin, urlparse

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

from app import deezer_import
from app import global_keys as global_keys_mod
from app.audio.macos_now_playing import MacOSNowPlayingBridge
from app.audio.player import PCMPlayer
from app import playlist_import
from app import spotify_import
from app.downloader import DownloadItem, DownloadStatus, Downloader
from app.http import SESSION
from app.lastfm import LastFmClient
from app.local_index import LocalIndex
from app import now_playing_state
from app.paths import bundled_resource_dir
from app.play_reporter import PlayReporter, PlaySession, recent_log as play_report_recent_log
from app.settings import Settings, load_settings, save_settings
from app.tidal_client import (
    TidalBackoffError,
    TidalClient,
    tidal_backoff_state,
    tidal_jitter_sleep,
)


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


# tidalapi's SimpleList.get_item silently returns None for any item type
# it doesn't recognize, and only logs at WARNING level on its own
# "tidalapi.page" logger which we don't forward. Wrap it so both the
# "type not implemented" case and the "parse raised an exception" case
# surface to stderr with the raw shape, so a row that suddenly renders
# short tells us which item types got dropped.
_orig_get_item = _tidal_page.SimpleList.get_item


def _patched_get_item(self, json_obj):
    try:
        result = _orig_get_item(self, json_obj)
    except Exception as exc:
        try:
            data_preview = json.dumps(json_obj)[:400]
        except Exception:
            data_preview = repr(json_obj)[:400]
        print(
            f"[page] SimpleList.get_item raised on "
            f"type={json_obj.get('type')!r}: {exc} | data={data_preview}",
            file=sys.stderr,
            flush=True,
        )
        return None
    if result is None:
        print(
            f"[page] SimpleList.get_item dropped item type="
            f"{json_obj.get('type')!r} (not in item_types map)",
            file=sys.stderr,
            flush=True,
        )
    return result


_tidal_page.SimpleList.get_item = _patched_get_item


tidal = TidalClient()
lastfm = LastFmClient()
play_reporter = PlayReporter(tidal)
# macOS Now Playing bridge — claims the system "active media player"
# role so media keys route to Tideway instead of Apple Music when our
# window isn't focused. Constructed empty; the lifespan startup hook
# fills in base_url and calls .start(). No-ops on non-macOS so this
# is safe to instantiate unconditionally.
macos_now_playing_bridge = MacOSNowPlayingBridge()
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
    _hotkey_bus.bind_loop(asyncio.get_running_loop())
    output_root = Path(settings.output_dir).expanduser()
    _cleanup_part_files(output_root)
    local_index.start_scan(output_root)

    # Start the global media-key listener. Publishes events to
    # _hotkey_bus → /api/hotkey/events SSE → frontend maps to
    # usePlayer actions. On macOS, pynput needs Accessibility
    # permission; when it doesn't have it, start() succeeds but no
    # events arrive. The user can grant permission later without a
    # restart (the listener picks it up automatically).
    stop_hotkeys = None
    try:
        port = int(os.environ.get("TIDAL_DL_PORT", "47823"))
        stop_hotkeys = global_keys_mod.start_global_hotkeys(port)
    except Exception as exc:
        print(f"[global-keys] startup failed: {exc}", flush=True)

    # Register Tideway with macOS Now Playing so media keys
    # (Cmd-F8, hardware keys on a connected keyboard, the Touch
    # Bar's play/pause button, the Control Center widget) route
    # to us instead of Apple Music when Tideway isn't focused.
    # No-ops on non-macOS. See app/audio/macos_now_playing.py.
    try:
        port = int(os.environ.get("TIDAL_DL_PORT", "47823"))
        macos_now_playing_bridge.set_base_url(f"http://127.0.0.1:{port}")
        macos_now_playing_bridge.start()
    except Exception as exc:
        print(f"[macos-np] startup failed: {exc}", flush=True)

    # Begin Cast device discovery in the background. Cheap — opens
    # one zeroconf browser thread that gets pruned on shutdown. The
    # picker reads from `cast_manager.list_devices()` lazily, so
    # there's no hot loop here, just a continuously-updated cache.
    # See app/audio/cast.py.
    try:
        from app.audio.cast import cast_manager as _cast_manager
        _cast_manager.start_discovery()
        # Wire the local-output silencer so PCMPlayer mutes its
        # sounddevice output while a Cast session is open. PCM tap
        # to the Cast encoder happens BEFORE the silencer in the
        # callback ordering, so the device still gets full audio
        # while local goes quiet.
        try:
            _cast_manager.set_local_silencer(
                _native_player().set_external_output_active
            )
        except Exception as exc:
            print(f"[cast] silencer wire failed: {exc}", flush=True)
    except Exception as exc:
        print(f"[cast] startup failed: {exc}", flush=True)

    # Wire the Tidal Connect manager to the audio engine. Silencer
    # mutes local sounddevice output while a TC session is open
    # (shares the same PCMPlayer flag Cast uses, so they don't
    # fight over the local-output state). Track URL resolver lets
    # the manager mint signed Tidal stream URLs via tidalapi and
    # pack them into the DIDL-Lite metadata that's handed to the
    # OpenHome Playlist.Insert call.
    try:
        from app.audio.tidal_connect import get_manager as _tc_get_manager
        from app.audio.openhome import TrackMetadata as _TrackMetadata
        _tc_mgr = _tc_get_manager()
        _tc_mgr.set_local_silencer(
            _native_player().set_external_output_active
        )

        def _tc_url_resolver(track_id: int):
            """Tidal track id → (stream_url, TrackMetadata).

            Goes through the same tidalapi path the local player
            uses for stream resolution. Hands the device the FIRST
            URL from the manifest — for DASH/HLS that's the
            manifest playlist URL, which OpenHome devices that
            speak DASH/HLS internally will fetch and play.

            This is the bet from the scoping doc: Tidal Connect
            targets accept signed Tidal URLs as DIDL-Lite <res>
            content. If hardware testing shows the bet is wrong,
            fix is localized to this resolver. We'll know in the
            first minute of real-device testing.
            """
            track = tidal.session.track(int(track_id))
            stream = track.get_stream()
            manifest = stream.get_stream_manifest()
            urls = list(getattr(manifest, "urls", []) or [])
            if not urls:
                raise RuntimeError(
                    f"track {track_id}: manifest has no URLs"
                )
            stream_url = urls[0]
            artists = getattr(track, "artists", None) or []
            artist_name = (
                artists[0].name if artists and getattr(artists[0], "name", None)
                else getattr(track, "artist", None)
                and getattr(track.artist, "name", "")
                or ""
            )
            album = getattr(track, "album", None)
            album_name = getattr(album, "name", "") if album else ""
            cover_id = getattr(album, "cover", None) if album else None
            cover_url = (
                f"https://resources.tidal.com/images/"
                f"{(cover_id or '').replace('-', '/')}/640x640.jpg"
                if cover_id
                else ""
            )
            duration_s = int(getattr(track, "duration", 0) or 0)
            codec = (
                getattr(manifest, "codecs", None)
                or getattr(manifest, "get_codecs", lambda: None)()
                or ""
            )
            mime_type = (
                "audio/flac" if "flac" in str(codec).lower()
                else "audio/mp4"
            )
            metadata = _TrackMetadata(
                title=getattr(track, "name", "") or "",
                artist=artist_name,
                album=album_name,
                duration_s=duration_s,
                cover_url=cover_url,
                track_uri=stream_url,
                mime_type=mime_type,
            )
            return (stream_url, metadata)

        _tc_mgr.set_track_url_resolver(_tc_url_resolver)
    except Exception as exc:
        print(f"[tidal-connect] startup wiring failed: {exc}", flush=True)

    try:
        yield
    finally:
        # Disconnect any active Tidal Connect session before
        # shutting down. Sends Stop to the device so it doesn't
        # keep playing whatever was loaded.
        try:
            from app.audio.tidal_connect import get_manager as _tc_get_manager
            _tc_get_manager().disconnect()
        except Exception:
            pass
        if stop_hotkeys is not None:
            try:
                stop_hotkeys()
            except Exception:
                pass
        # Stop Cast discovery — releases the zeroconf socket and the
        # browser thread. Best-effort; we don't block shutdown on it.
        try:
            from app.audio.cast import cast_manager as _cast_manager
            _cast_manager.stop_discovery()
        except Exception:
            pass
        # Close the shared requests session so sockets in its connection pool
        # are released cleanly on reload/shutdown.
        try:
            SESSION.close()
        except Exception:
            pass


app = FastAPI(title="Tideway", lifespan=lifespan)


# Any Tidal request inside a backoff window raises this. FastAPI would
# otherwise render it as a raw 500 — convert to a clean 503 with a
# Retry-After-style message. Anything asking for Tidal data lands here
# so the UI can fail gracefully (and the TidalBackoffBanner is already
# explaining the situation at the top of the screen).
from fastapi.requests import Request as _FastAPIRequest
from fastapi.responses import JSONResponse as _FastAPIJSONResponse
@app.exception_handler(TidalBackoffError)
async def _tidal_backoff_handler(request: _FastAPIRequest, exc: TidalBackoffError):
    return _FastAPIJSONResponse(
        status_code=503,
        content={
            "detail": (
                "Tideway is holding off Tidal requests after a rate-limit "
                "or abuse-detection response. Try again in "
                f"{int(exc.seconds_remaining)}s."
            ),
            "tidal_backoff": True,
            "seconds_remaining": exc.seconds_remaining,
            "reason": exc.reason,
        },
        headers={"Retry-After": str(max(1, int(exc.seconds_remaining)))},
    )

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
        # Realtime throughput in bytes/sec while the row is in
        # IN_PROGRESS, otherwise 0. Frontend formats as MB/s for
        # display in the Downloads panel; older clients that don't
        # know this key just ignore it.
        "speed_bps": getattr(item, "speed_bps", 0.0),
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
    def _ref(a) -> dict:
        # Pull the picture UUID off the embedded artist when Tidal
        # ships one. Most track/album payloads include it for each
        # artist entry; the album-page pill and similar chrome read
        # this so they don't have to round-trip to /api/artist for
        # just an avatar.
        pic_uuid = getattr(a, "picture", None)
        picture = (
            _cover_url_from_uuid(pic_uuid, 160)
            if isinstance(pic_uuid, str) and pic_uuid
            else None
        )
        return {"id": str(a.id), "name": a.name, "picture": picture}

    out: list[dict] = []
    try:
        for a in obj.artists or []:
            out.append(_ref(a))
    except Exception:
        pass
    if not out:
        try:
            a = obj.artist
            if a is not None:
                out.append(_ref(a))
        except Exception:
            pass
    return out


def track_to_dict(t) -> dict:
    album = _first(lambda: t.album)
    # tidalapi populates `mixes` from the raw track payload — it's a
    # dict keyed by mix type ("TRACK_MIX" for the per-track radio).
    # Pass the id through so the frontend can navigate straight to
    # Tidal's proper mix page (with composite cover + metadata) from
    # any track menu, no extra API round-trip needed.
    mixes = _first(lambda: t.mixes) or {}
    track_mix_id = (
        mixes.get("TRACK_MIX") if isinstance(mixes, dict) else None
    )
    # media_metadata_tags — e.g. ['HIRES_LOSSLESS'] or ['LOSSLESS']. The
    # Library / search format filter + download-dropdown badge use this
    # to tell hi-res releases from CD-res. We don't surface audio_modes
    # (DOLBY_ATMOS / SONY_360RA) — Tidal won't serve those streams to
    # our client_id anyway.
    media_tags = _first(lambda: t.media_metadata_tags) or []
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
        "track_mix_id": track_mix_id,
        "media_tags": [m for m in media_tags if m] if media_tags else [],
        # International Standard Recording Code — universal track id
        # shared across Spotify / Tidal / Apple / etc. Used by the
        # Spotify-enrichment path to resolve a Tidal track to its
        # Spotify counterpart (and thus to global play counts).
        "isrc": _first(lambda: t.isrc),
    }


def album_to_dict(a) -> dict:
    release_date = _first(lambda: a.release_date)
    media_tags = _first(lambda: a.media_metadata_tags) or []
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
        # Format tags for the library / search filter chip row +
        # download-dropdown Max/Lossless annotation.
        "media_tags": [m for m in media_tags if m] if media_tags else [],
    }


def _norm_title(s: Optional[str]) -> str:
    """Normalise an album / track name for explicit-dupe matching. Drop
    anything after a final '(Clean)' / '(Explicit)' marker so we treat
    'Rodeo' and 'Rodeo (Clean)' as the same record."""
    if not s:
        return ""
    base = s.strip().lower()
    base = re.sub(r"\s*\((clean|explicit)\)\s*$", "", base)
    return base


def filter_explicit_dupes(items: list, preference: str, *, kind: str) -> list:
    """Collapse explicit / clean pairs of the same album or track.

    `preference` is whatever is stored in settings.explicit_content_preference:
    'explicit' (default), 'clean', or 'both'. 'both' returns the list
    unchanged; the other two drop the unwanted edition when a matching
    pair exists, and leave solo entries alone.

    Items are matched on (normalised_name, version, primary_artist_id)
    for albums and (normalised_name, normalised_album, primary_artist_id)
    for tracks, so a Deluxe re-release never merges into its original
    and the same song on two different albums stays distinct."""
    if preference not in ("explicit", "clean"):
        return list(items)

    def _primary_artist_id(item) -> str:
        try:
            artists = getattr(item, "artists", None) or []
            if artists:
                aid = getattr(artists[0], "id", None)
                if aid is not None:
                    return str(aid)
        except Exception:
            pass
        try:
            aid = getattr(getattr(item, "artist", None), "id", None)
            return str(aid) if aid is not None else ""
        except Exception:
            return ""

    def _key(item):
        primary = _primary_artist_id(item)
        name = _norm_title(getattr(item, "name", None))
        if kind == "album":
            version = (getattr(item, "version", "") or "").strip().lower()
            return ("album", name, version, primary)
        album_obj = getattr(item, "album", None)
        album_name = _norm_title(getattr(album_obj, "name", None)) if album_obj else ""
        return ("track", name, album_name, primary)

    # Group items by key preserving first-seen order. If more than one
    # edition exists under the same key, pick the preferred one.
    buckets: dict[tuple, list] = {}
    order: list[tuple] = []
    for it in items:
        key = _key(it)
        if key not in buckets:
            order.append(key)
            buckets[key] = []
        buckets[key].append(it)

    out: list = []
    want_explicit = preference == "explicit"
    for key in order:
        bucket = buckets[key]
        if len(bucket) == 1:
            out.append(bucket[0])
            continue
        # Prefer the requested edition; fall back to the first-seen when
        # the preferred one isn't present.
        preferred = next(
            (x for x in bucket if bool(getattr(x, "explicit", False)) == want_explicit),
            bucket[0],
        )
        out.append(preferred)
    return out


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

# Set by the desktop launcher so /api/_internal/quit can tear the app
# down from the UI. Needed because close-to-tray intercepts the red-X
# button, so Cmd+Q / the in-app Quit menu need a different path to an
# actual window.destroy().
_quit_callback: Optional[Callable[[], None]] = None

# Set by the desktop launcher so /api/_internal/mini_player can spawn
# a second pywebview window. No-op in plain-browser dev mode.
_mini_player_callback: Optional[Callable[[], None]] = None

# Set by the desktop launcher so /api/auth/login/inapp/start can
# open a pywebview child window pointed at Tidal's PKCE login URL
# and intercept the tidal:// redirect automatically, skipping the
# copy-the-Oops-URL paste step the dev-mode login still needs.
_inapp_login_callback: Optional[Callable[[str], None]] = None

# In-app login state surface. The frontend polls
# /api/auth/login/inapp/state alongside /api/auth/status so when
# the shell aborts a login early (SSO provider detected, timeout,
# user closed the window) the UI switches out of the spinner
# state immediately instead of hanging for 10 minutes.
_inapp_login_state: dict[str, object] = {"phase": "idle"}


def set_inapp_login_phase(phase: str) -> None:
    """Called by desktop.py to flag state transitions on the in-
    app login. Valid phases: idle, active, aborted_sso, closed,
    unauthorized."""
    _inapp_login_state["phase"] = phase


def register_focus_callback(fn: Callable[[], None]) -> None:
    global _focus_callback
    _focus_callback = fn


def register_quit_callback(fn: Callable[[], None]) -> None:
    global _quit_callback
    _quit_callback = fn


def register_mini_player_callback(fn: Callable[[], None]) -> None:
    global _mini_player_callback
    _mini_player_callback = fn


def register_inapp_login_callback(fn: Callable[[str], None]) -> None:
    global _inapp_login_callback
    _inapp_login_callback = fn


# ---------------------------------------------------------------------------
# App version + update check
# ---------------------------------------------------------------------------

# Read from repo-root VERSION at startup. Same file the mac spec's
# Info.plist reads from, so everything agrees. When running frozen
# (packaged), _MEIPASS is the Resources root — VERSION lives at the
# bundle root via the spec's datas entry.
def _read_app_version() -> str:
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", ""))
        if meipass.is_dir():
            candidates.append(meipass / "VERSION")
    candidates.append(Path(__file__).resolve().parent / "VERSION")
    for p in candidates:
        try:
            if p.is_file():
                v = p.read_text().strip()
                if v:
                    return v
        except Exception:
            continue
    return "0.0.0"


APP_VERSION = _read_app_version()

# GitHub repo we check for the newest release. Public and
# unauthenticated, so the rate limit is 60 requests per hour per IP,
# which is plenty for a startup-time probe.
#
# Defaults to the upstream repo so packaged builds get update checks
# without any extra config. Forks and private builds can point the
# check at their own releases by setting TIDEWAY_UPDATE_REPO. Set it
# to an empty string to disable auto update entirely.
_UPDATE_REPO = os.environ.get("TIDEWAY_UPDATE_REPO", "J-M-PUNK/tideway")

# Cache the latest-release lookup so mashing F5 in the frontend doesn't
# burn the GitHub rate limit. 1 hour TTL — update checks don't need to
# be realtime.
_update_cache: dict = {}
_update_cache_lock = threading.Lock()
_UPDATE_CACHE_TTL_SEC = 3600.0


def _parse_semver(v: str) -> tuple[int, ...]:
    """Parse 'v1.2.3' / '1.2.3' / '1.2.3-beta' → (1, 2, 3). Tags that
    don't parse get (0,) so they always compare as older than a real
    version — intentional; lets us ignore dev / pre-release tags."""
    s = v.strip().lstrip("vV")
    # Strip any pre-release / build-metadata suffix for the comparison.
    for sep in ("-", "+"):
        idx = s.find(sep)
        if idx >= 0:
            s = s[:idx]
    parts: list[int] = []
    for chunk in s.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            return (0,)
    return tuple(parts) if parts else (0,)


@app.get("/api/version")
def app_version() -> dict:
    return {"version": APP_VERSION}


# Settings fields to redact when included in the activity report.
# Anything here gets replaced with a "<redacted>" sentinel before
# the report is written. Anchors against the credentials guarantee
# in the user-facing description of the activity-report feature:
# "settings (with credentials stripped)".
_DIAGNOSTICS_REDACT_KEYS = ("spotify_client_id",)


def _build_activity_report() -> dict:
    """Assemble the full diagnostic snapshot used by the Save Activity
    Report button. Everything here is best-effort — if any single
    section fails, it's recorded as an error string and the rest of
    the report is still produced. The whole point is to be useful
    even when the app is in a degraded state (e.g. user can't sign
    in and is reporting a bug)."""
    report: dict = {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "app": {
            "version": APP_VERSION,
            "frozen": bool(getattr(sys, "frozen", False)),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "auth": {
            # Just the boolean, not the token. Knowing whether the
            # user is signed in is part of "what state was the app in
            # when this happened"; the token is irrelevant to a bug
            # report and a credential.
            "logged_in": _is_logged_in(),
        },
    }

    # Settings (redacted). Build directly from the live `settings`
    # global — that way the report reflects what the running process
    # is actually using, not whatever's currently on disk.
    try:
        settings_dict = asdict(settings)
        for key in _DIAGNOSTICS_REDACT_KEYS:
            if key in settings_dict and settings_dict[key]:
                settings_dict[key] = "<redacted>"
        report["settings"] = settings_dict
    except Exception as exc:
        report["settings"] = {"error": f"{type(exc).__name__}: {exc}"}

    # Player snapshot — only if the player is already constructed.
    # Calling _native_player() here would lazily construct it just
    # to dump diagnostics, which would leave a side effect on a
    # process that previously never touched audio. Read the
    # singleton directly instead.
    try:
        if _pcm_player_singleton is not None:
            report["player"] = _snapshot_dict(_pcm_player_singleton.snapshot())
        else:
            report["player"] = {"state": "not_initialized"}
    except Exception as exc:
        report["player"] = {"error": f"{type(exc).__name__}: {exc}"}

    # Audio devices. Three pieces:
    #   - what the user picked in settings (the id),
    #   - what the player resolved that to (best-effort device name),
    #   - the full sounddevice enumeration (host APIs, channel counts,
    #     default sample rates) — this is where most "wrong device
    #     selected" bug reports actually get answered.
    audio: dict = {
        "configured_device_id": getattr(settings, "audio_output_device", "") or None,
    }
    try:
        if _pcm_player_singleton is not None:
            audio["player_devices"] = _pcm_player_singleton.list_output_devices()
        else:
            audio["player_devices"] = None
    except Exception as exc:
        audio["player_devices"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        import sounddevice as sd  # type: ignore

        # query_devices() returns a list of dicts plus host APIs
        # available via query_hostapis(). Capture both — the host
        # API id stored on each device only makes sense alongside
        # the host APIs list.
        devices = sd.query_devices()
        # `query_devices()` may return either a list of dicts or, in
        # some sounddevice versions, a DeviceList that's iterable but
        # not a plain list. Coerce to list[dict] for JSON.
        audio["sounddevice_devices"] = [dict(d) for d in devices]
        audio["sounddevice_hostapis"] = [
            dict(h) for h in sd.query_hostapis()
        ]
        defaults = sd.default.device
        audio["sounddevice_default_input_idx"] = (
            defaults[0] if isinstance(defaults, (list, tuple)) else None
        )
        audio["sounddevice_default_output_idx"] = (
            defaults[1] if isinstance(defaults, (list, tuple)) else None
        )
    except Exception as exc:
        audio["sounddevice_error"] = f"{type(exc).__name__}: {exc}"
    report["audio"] = audio

    return report


@app.post("/api/diagnostics/save-activity-report")
def save_activity_report() -> dict:
    """Write a diagnostic snapshot to ~/Downloads/tideway-activity-
    <timestamp>.json. Intentionally unauthenticated so users who
    can't sign in can still produce one when they file a bug.

    The path is OS-aware: ~/Downloads on macOS / Linux, the user's
    Downloads folder on Windows resolved through the shell's known-
    folder if available, otherwise the home directory as a fallback.
    """
    report = _build_activity_report()
    # Filename-safe ISO-ish timestamp with the colons swapped out so
    # Windows accepts it (NTFS won't allow `:`). Local time so users
    # filing reports recognize the time they hit the button.
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    filename = f"tideway-activity-{ts}.json"

    # Resolve the Downloads folder. Path.home()/"Downloads" works on
    # all three platforms when the OS uses the standard locale and
    # the user hasn't moved the folder. If it doesn't exist (locale
    # difference, custom folder structure, server-style install),
    # fall through to $HOME so the report still lands somewhere
    # discoverable.
    downloads = Path.home() / "Downloads"
    target_dir = downloads if downloads.is_dir() else Path.home()
    target_path = target_dir / filename
    try:
        with open(target_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str, sort_keys=True)
    except OSError as exc:
        # Out of disk, permission denied, weird path. Surface as a
        # 500 so the frontend can render an actionable error toast.
        raise HTTPException(
            status_code=500,
            detail=f"Couldn't write activity report: {exc}",
        )

    return {
        "path": str(target_path),
        "size_bytes": target_path.stat().st_size,
        "report_schema": report["schema"],
    }


def _match_release_asset(release_data: dict) -> Optional[str]:
    """Return the download URL of the current platform's installer in
    a GitHub /releases/latest response, or None if the release ships
    no matching asset.

    Naming convention (matches scripts/build_dmg.sh and the Inno Setup
    script):
      - macOS:           Tideway-<version>.dmg
      - Windows x64:     Tideway-setup-<version>.exe
      - Windows ARM64:   Tideway-setup-<version>-arm64.exe

    On Windows we pick the asset matching the host CPU rather than the
    process arch. platform.machine() reflects the underlying CPU even
    when we're running as an emulated x64 process on an ARM64 host
    (Prism exposes PROCESSOR_ARCHITEW6432=ARM64), so an ARM64 user who
    accidentally installed the x64 build will still be offered the
    correct ARM64 installer on the next update.

    Linux falls through to None because we don't package a Linux
    installer today.
    """
    want_arm64 = False
    if sys.platform == "darwin":
        suffix = ".dmg"
    elif sys.platform.startswith("win"):
        suffix = ".exe"
        want_arm64 = platform.machine().lower() in ("arm64", "aarch64")
    else:
        return None

    assets = release_data.get("assets") or []
    candidates: list[tuple[bool, str]] = []
    for a in assets:
        name = (a.get("name") or "").lower()
        if not (name.endswith(suffix) and name.startswith("tideway")):
            continue
        url = a.get("browser_download_url")
        if not url:
            continue
        is_arm64 = name.endswith("-arm64" + suffix)
        candidates.append((is_arm64, url))

    # Strict pass: only an asset with the matching arch suffix.
    for is_arm64, url in candidates:
        if is_arm64 == want_arm64:
            return url
    # Fallback: any matching extension. Lets older releases that
    # predate the ARM64 build still expose their single x64 asset to
    # ARM64 hosts (the install will fail at runtime, but that is the
    # pre-fix status quo and not a regression).
    if candidates:
        return candidates[0][1]
    return None


def _fetch_latest_release(timeout: float = 8.0) -> dict:
    """GET the latest GitHub release for the configured update repo,
    using `requests` so the call goes through certifi's CA bundle
    instead of urllib's system-resolved store.

    The bundled-Python urllib path was hitting cert verification
    failures on real installs (the symptom: /api/update-check
    returning `latest: null` with no logs). requests bundles its own
    CA file, so it works regardless of whether the OS-level cert path
    is plumbed through to the embedded interpreter.
    """
    import requests as _requests

    resp = _requests.get(
        f"https://api.github.com/repos/{_UPDATE_REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


@app.get("/api/update-check")
def update_check() -> dict:
    """Compare the running app's version against the latest GitHub
    Release. Returns {available, latest, url, notes, error} for the
    UI banner. Cached so repeated frontend probes don't spam GitHub's
    API.

    `available` is gated on (newer-tag AND installer-for-this-platform-
    in-the-release). The platform check matters when a point release
    ships an installer for some OSes but not others — e.g. a Windows-
    only fix release. macOS / Linux users on the older version would
    otherwise see a banner that points at a release with no asset they
    can install.

    `error` is non-null when the GitHub fetch itself failed. We were
    silently swallowing those exceptions, which made cert / network
    failures invisible to the user — the banner just never appeared.
    """
    now = time.monotonic()
    with _update_cache_lock:
        cached = _update_cache.get("latest")
        if cached and now - cached[0] < _UPDATE_CACHE_TTL_SEC:
            return cached[1]

    payload: dict = {
        "available": False,
        "current": APP_VERSION,
        "latest": None,
        "url": None,
        "notes": None,
        "error": None,
    }
    asset_url: Optional[str] = None
    # Auto update is off unless the fork sets TIDEWAY_UPDATE_REPO to
    # its own org/repo. Return the idle payload instead of hitting a
    # 404 on an empty repo path.
    if not _UPDATE_REPO:
        with _update_cache_lock:
            _update_cache["latest"] = (now, payload)
            _update_cache["asset_url"] = (now, asset_url)
        return payload
    try:
        data = _fetch_latest_release(timeout=4.0)
        latest_tag = (data.get("tag_name") or "").strip()
        latest_url = data.get("html_url") or None
        latest_notes = data.get("body") or None
        if latest_tag:
            payload["latest"] = latest_tag
            payload["url"] = latest_url
            payload["notes"] = latest_notes
            if _parse_semver(latest_tag) > _parse_semver(APP_VERSION):
                asset_url = _match_release_asset(data)
                if asset_url is not None:
                    payload["available"] = True
    except Exception as exc:
        # Offline / rate-limited / cert verify failure / repo private.
        # Surface the reason on the response so support can see what's
        # actually wrong and log it server-side. Old behavior was to
        # silently report no update, which made the cert-verify
        # failure on bundled Python invisible.
        msg = f"{type(exc).__name__}: {exc}"
        payload["error"] = msg
        logger.warning("update_check failed: %s", msg)

    with _update_cache_lock:
        _update_cache["latest"] = (now, payload)
        _update_cache["asset_url"] = (now, asset_url)
    return payload


def _update_asset_url() -> Optional[str]:
    """Return the download URL for this platform's installer in the
    latest GitHub release, or None if there isn't one.

    Reuses the cache populated by /api/update-check when warm — the
    "Install now" click otherwise pays a second GitHub round trip
    against the same data update_check just fetched.
    """
    if not _UPDATE_REPO:
        return None
    now = time.monotonic()
    with _update_cache_lock:
        cached = _update_cache.get("asset_url")
        if cached and now - cached[0] < _UPDATE_CACHE_TTL_SEC:
            return cached[1]
    try:
        data = _fetch_latest_release(timeout=8.0)
    except Exception as exc:
        logger.warning("_update_asset_url fetch failed: %s", exc)
        return None
    asset_url = _match_release_asset(data)
    with _update_cache_lock:
        _update_cache["asset_url"] = (now, asset_url)
    return asset_url


@app.post("/api/update/install")
def update_install() -> dict:
    """Download the latest release's installer for the current OS and
    open it so the user can run through the install prompt. Doesn't
    quit the app — the frontend does that after this returns so the
    old bundle is out of the way when the user drags / runs the new
    one.

    Returns the filesystem path we staged the download to so the UI
    can tell the user where to look if something goes sideways.
    """
    _require_local_access()
    url = _update_asset_url()
    if url is None:
        raise HTTPException(
            status_code=404,
            detail="No installer asset for this platform in the latest release.",
        )
    # Stage into ~/Downloads so the user sees it in their usual place
    # + can re-run it if they cancel the first attempt. Falls back to
    # a temp dir if Downloads doesn't exist / isn't writable.
    downloads = Path.home() / "Downloads"
    try:
        downloads.mkdir(parents=True, exist_ok=True)
        target_dir = downloads
    except OSError:
        target_dir = Path(tempfile.mkdtemp(prefix="tdl-update-"))
    filename = url.rsplit("/", 1)[-1] or "Tideway-update"
    target = target_dir / filename
    try:
        # Use requests so the download goes through certifi's CA
        # bundle — same reason as _fetch_latest_release. urllib's
        # cert path doesn't always resolve in the bundled Python.
        import requests as _requests

        with _requests.get(url, stream=True, timeout=60) as resp, open(
            target, "wb"
        ) as f:
            resp.raise_for_status()
            # 1 MB chunks keep memory flat on 100 MB+ installers.
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Couldn't download installer: {exc}",
        )
    # Open the installer in whatever way the OS expects. Detached so
    # the subprocess doesn't linger as a zombie when the app quits
    # next.
    try:
        if sys.platform == "darwin":
            subprocess.Popen(
                ["open", str(target)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        elif sys.platform.startswith("win"):
            # os.startfile is the Windows idiom — it hands the file
            # to the shell the same way double-clicking would.
            os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(
                ["xdg-open", str(target)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Downloaded but couldn't open: {exc}",
        )
    return {"ok": True, "downloaded_to": str(target)}


# ---------------------------------------------------------------------------
# Spotify import
# ---------------------------------------------------------------------------


def _spotify_redirect_uri() -> str:
    # Has to exactly match whatever the user registered in their
    # Spotify Developer dashboard. We use our single-instance port so
    # the auth code lands straight back into this process.
    return f"http://127.0.0.1:{int(os.environ.get('TIDAL_DL_PORT', '47823'))}/api/import/spotify/callback"


class _SpotifyConnectRequest(BaseModel):
    client_id: str


@app.get("/api/import/spotify/status")
def spotify_status() -> dict:
    _require_local_access()
    auth = spotify_import.load_session()
    connected = auth is not None
    username = None
    if auth is not None:
        try:
            me = spotify_import.current_user(auth)
            username = me.get("display_name") or me.get("id")
        except Exception:
            # Token might be invalid — report not-connected so the UI
            # surfaces the re-auth path.
            connected = False
    return {
        "connected": connected,
        "username": username,
        "client_id_set": bool(settings.spotify_client_id),
        "redirect_uri": _spotify_redirect_uri(),
    }


@app.post("/api/import/spotify/connect")
def spotify_connect(req: _SpotifyConnectRequest) -> dict:
    """Save the client_id + return the Spotify authorization URL.
    Frontend opens it in an external browser; the callback route
    below picks up the code and finalizes the session."""
    _require_local_access()
    client_id = (req.client_id or "").strip()
    if not client_id:
        raise HTTPException(status_code=400, detail="client_id is required")
    settings.spotify_client_id = client_id
    save_settings(settings)
    try:
        auth_url, _state = spotify_import.build_auth_url(
            client_id, _spotify_redirect_uri()
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"auth_url": auth_url}


@app.get("/api/import/spotify/callback")
def spotify_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    """Landing endpoint Spotify redirects the browser to after the
    user authorizes. Exchanges the code for a token, then returns a
    small HTML page telling the user to return to the app."""
    from fastapi.responses import HTMLResponse

    if error:
        return HTMLResponse(
            f"<h3>Spotify authorization failed: {error}</h3>"
            "<p>You can close this tab and try again in the app.</p>",
            status_code=400,
        )
    if not code or not state:
        return HTMLResponse(
            "<h3>Missing code / state in callback</h3>"
            "<p>Try connecting again from the app.</p>",
            status_code=400,
        )
    auth = spotify_import.exchange_code(code, state, _spotify_redirect_uri())
    if auth is None:
        return HTMLResponse(
            "<h3>Spotify token exchange failed</h3>"
            "<p>Close this tab and try connecting again.</p>",
            status_code=502,
        )
    return HTMLResponse(
        "<h3>Connected to Spotify 🎉</h3>"
        "<p>You can close this tab and return to the app.</p>",
    )


@app.post("/api/import/spotify/disconnect")
def spotify_disconnect() -> dict:
    _require_local_access()
    spotify_import.clear_session()
    return {"ok": True}


@app.get("/api/import/spotify/playlists")
def spotify_playlists() -> list[dict]:
    _require_local_access()
    auth = spotify_import.load_session()
    if auth is None:
        raise HTTPException(status_code=401, detail="Not connected to Spotify")
    try:
        return spotify_import.list_playlists(auth)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


class _SpotifyMatchRequest(BaseModel):
    playlist_id: str


@app.post("/api/import/spotify/match")
def spotify_match(req: _SpotifyMatchRequest) -> dict:
    """Fetch a Spotify playlist's tracks + resolve each to a Tidal
    track. Returns a preview payload so the frontend can let the user
    eyeball the matches before creating the playlist. Matching fans
    out across a bounded worker pool so a 100-track playlist lands in
    a few seconds instead of half a minute."""
    _require_auth()
    auth = spotify_import.load_session()
    if auth is None:
        raise HTTPException(status_code=401, detail="Not connected to Spotify")
    try:
        tracks = spotify_import.list_playlist_tracks(auth, req.playlist_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    rows = spotify_import.match_tracks(tidal.session, tracks)
    matched = sum(1 for r in rows if r["match"] is not None)
    return {
        "rows": rows,
        "total": len(rows),
        "matched": matched,
    }


class _CreatePlaylistRequest(BaseModel):
    name: str
    description: Optional[str] = ""
    track_ids: list[str]


class _TextImportRequest(BaseModel):
    text: str


@app.post("/api/import/spotify/liked-tracks/match")
def spotify_match_liked_tracks() -> dict:
    """Pull the user's Liked Songs + match each against Tidal. Same
    shape as the playlist matcher; frontend feeds rows into the
    bulk-favorite flow instead of creating a playlist."""
    _require_auth()
    auth = spotify_import.load_session()
    if auth is None:
        raise HTTPException(status_code=401, detail="Not connected to Spotify")
    try:
        tracks = spotify_import.list_liked_tracks(auth)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    rows = spotify_import.match_tracks(tidal.session, tracks)
    matched = sum(1 for r in rows if r["match"] is not None)
    return {"rows": rows, "total": len(rows), "matched": matched}


@app.post("/api/import/spotify/saved-albums/match")
def spotify_match_saved_albums() -> dict:
    _require_auth()
    auth = spotify_import.load_session()
    if auth is None:
        raise HTTPException(status_code=401, detail="Not connected to Spotify")
    try:
        albums = spotify_import.list_saved_albums(auth)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    rows = spotify_import.match_albums(tidal.session, albums)
    matched = sum(1 for r in rows if r["match"] is not None)
    return {"rows": rows, "total": len(rows), "matched": matched}


@app.post("/api/import/spotify/followed-artists/match")
def spotify_match_followed_artists() -> dict:
    """Needs the user-follow-read scope; sessions that predate this
    feature will 403 from Spotify. Surface a clear re-auth prompt
    via the HTTP detail so the UI can suggest disconnecting +
    reconnecting."""
    _require_auth()
    auth = spotify_import.load_session()
    if auth is None:
        raise HTTPException(status_code=401, detail="Not connected to Spotify")
    try:
        artists = spotify_import.list_followed_artists(auth)
    except Exception as exc:
        msg = str(exc)
        if "403" in msg or "Insufficient" in msg:
            raise HTTPException(
                status_code=403,
                detail="Your Spotify session doesn't have permission to read followed artists. Disconnect and reconnect to re-grant.",
            )
        raise HTTPException(status_code=502, detail=msg)
    rows = spotify_import.match_artists(tidal.session, artists)
    matched = sum(1 for r in rows if r["match"] is not None)
    return {"rows": rows, "total": len(rows), "matched": matched}


class _BulkFavoriteImportRequest(BaseModel):
    kind: str  # "track" | "album" | "artist"
    ids: list[str]


@app.post("/api/import/favorite")
def import_favorite(req: _BulkFavoriteImportRequest) -> dict:
    """Bulk-favorite a list of Tidal ids. Wraps the existing
    /api/favorites/bulk handler — import review screens call this
    after the user confirms their selection. Sync (not fire-and-
    forget like the legacy bulk endpoint) so the UI can show the
    final count."""
    _require_auth()
    if req.kind not in FAVORITE_KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown kind: {req.kind}")
    added = 0
    failed = 0
    for obj_id in req.ids:
        try:
            tidal.favorite(req.kind, obj_id, add=True)
            added += 1
        except Exception:
            failed += 1
    return {"kind": req.kind, "added": added, "failed": failed}


class _DeezerImportRequest(BaseModel):
    source: str  # playlist id OR full Deezer URL


@app.post("/api/import/deezer/match")
def deezer_match(req: _DeezerImportRequest) -> dict:
    """Fetch a public Deezer playlist by id / URL + match its tracks
    against Tidal. No OAuth — Deezer's public API serves any playlist
    that's marked public, which covers 95%+ of what users want to
    import without the friction of a registered dev app."""
    _require_auth()
    pid = deezer_import.parse_playlist_id(req.source)
    if not pid:
        raise HTTPException(
            status_code=400,
            detail="Couldn't find a Deezer playlist id in the input",
        )
    try:
        playlist = deezer_import.fetch_playlist(pid)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    rows = deezer_import.match_each(tidal.session, playlist["tracks"])
    matched = sum(1 for r in rows if r["match"] is not None)
    return {
        "rows": rows,
        "total": len(rows),
        "matched": matched,
        "playlist": {
            "name": playlist["name"],
            "description": playlist["description"],
        },
    }


@app.post("/api/import/text/parse")
def text_import_parse(req: _TextImportRequest) -> dict:
    """Parse an M3U / M3U8 / plain-text playlist blob + match each
    parsed row against Tidal. Returns the same {rows, total, matched}
    shape the Spotify matcher uses so the frontend's MatchReview UI
    can render both sources identically."""
    _require_auth()
    parsed = playlist_import.parse(req.text or "")
    rows = playlist_import.match_each(tidal.session, parsed)
    matched = sum(1 for r in rows if r["match"] is not None)
    return {"rows": rows, "total": len(rows), "matched": matched}


@app.post("/api/import/create")
@app.post("/api/import/spotify/create")
def import_create(req: _CreatePlaylistRequest) -> dict:
    """Create a Tidal playlist from a set of Tidal track ids — the
    ones the frontend kept after reviewing matches. Generic across
    every import source (Spotify OAuth, M3U, Deezer once it lands)
    since by this point we're just looking at Tidal track ids.

    Two routes point at this handler: /api/import/create is the new
    generic path, /api/import/spotify/create is the legacy alias.
    Keep both for now so older frontends that ship pointing at the
    original path don't 404."""
    _require_auth()
    name = (req.name or "").strip() or "Imported playlist"
    try:
        created = tidal.create_playlist(name, req.description or "")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Couldn't create playlist: {exc}")
    pid = getattr(created, "id", None) or getattr(created, "uuid", None)
    if not pid:
        raise HTTPException(status_code=502, detail="Created playlist has no id")

    # Tidal's playlist.add() takes a list of ints; batch so we don't
    # overshoot whatever their request-size ceiling is (undocumented
    # but 100 has been reliable across every client I've seen).
    added = 0
    failed = 0
    BATCH = 100
    for i in range(0, len(req.track_ids), BATCH):
        chunk = req.track_ids[i : i + BATCH]
        try:
            int_ids = [int(x) for x in chunk]
            created.add(int_ids)
            added += len(chunk)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "spotify import: add-batch failed (%s): %s", len(chunk), exc
            )
            failed += len(chunk)
    return {
        "playlist_id": str(pid),
        "added": added,
        "failed": failed,
        "name": name,
    }


@app.get("/api/health")
def health() -> dict:
    """Liveness probe AND single-instance detection marker.

    The desktop launcher probes this endpoint before binding its own
    port; an existing healthy response (with `app` == _HEALTH_MARKER)
    means another copy is already running and the second launch should
    exit instead of crashing on EADDRINUSE.

    Also reports whether the curl-cffi impersonated transport loaded.
    When False, the app is on the plain-requests fallback, which is
    more likely to be flagged by anti-abuse heuristics and is the
    transport that surfaces the cryptic
    `ConnectionError(PermissionError(13))` chain when a user's AV
    blocks the socket. Surface it here so support can ask the user
    to hit /api/health and read back one boolean.
    """
    try:
        from app.http import IMPERSONATED as _impersonated
    except Exception:
        _impersonated = False
    return {"ok": True, "app": _HEALTH_MARKER, "impersonated": _impersonated}


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


@app.post("/api/_internal/quit", include_in_schema=False)
def quit_app(request: Request) -> dict:
    """Force a real app shutdown from the UI.

    The close-to-tray handler swallows the red-X button, so the user
    needs a dedicated "Quit" path that bypasses it. Restricted to
    loopback for the same reason as /focus — only legitimate caller is
    the local UI.
    """
    client = request.client
    host = client.host if client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403)
    if _quit_callback is None:
        return {"ok": False, "reason": "no launcher"}
    try:
        _quit_callback()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


class _WindowThemeRequest(BaseModel):
    theme: str


@app.post("/api/_internal/window-theme", include_in_schema=False)
def set_window_theme(request: Request, payload: _WindowThemeRequest) -> dict:
    """Push the React UI's active theme down to the OS so the title
    bar tints to match the app body. Loopback-only — only legitimate
    caller is our own UI shell, and the side effect (recoloring the
    OS-drawn window chrome) shouldn't be reachable from anywhere
    else. Unauthenticated by design: theme switches need to work
    even before the user signs in (login screen has the same chrome
    as the rest of the app).
    """
    client = request.client
    host = client.host if client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403)
    if payload.theme not in ("dark", "light"):
        raise HTTPException(status_code=400, detail="theme must be dark or light")
    try:
        from app import window_chrome
        window_chrome.set_theme(payload.theme)
        return {"ok": True, "theme": window_chrome.get_theme()}
    except Exception as exc:
        # Window chrome is decorative — never let a tint failure break
        # the user's theme switch in the React UI.
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


@app.post("/api/_internal/mini_player", include_in_schema=False)
def open_mini_player(request: Request) -> dict:
    """Spawn a second, always-on-top pywebview window with the compact
    player UI. Returns {ok: false} in plain-browser dev mode where
    there's no launcher to create windows — the UI should hide the
    menu entry in that case.
    """
    client = request.client
    host = client.host if client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403)
    if _mini_player_callback is None:
        return {"ok": False, "reason": "no launcher"}
    try:
        _mini_player_callback()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


class _NotifyRequest(BaseModel):
    title: str
    body: str
    subtitle: Optional[str] = None


class _AutostartRequest(BaseModel):
    enabled: bool


class _VideoDownloadRequest(BaseModel):
    quality: Optional[str] = None  # "HIGH" | "MEDIUM" | "LOW"


@app.post("/api/video/{video_id}/download")
def video_download_start(video_id: int, req: _VideoDownloadRequest) -> dict:
    """Kick off an HLS → MP4 remux of a Tidal music video.

    Separate from the track-downloader queue because video downloads
    are rare and bypass all the DASH / manifest / retry plumbing the
    audio path needs. We reuse the same output_dir but put files in a
    `Videos/` subdir so they don't intermix with album folders.
    """
    _require_auth()
    from app import video_downloader

    quality = (req.quality or "").upper() or None
    if quality and quality not in _VALID_VIDEO_QUALITIES:
        raise HTTPException(status_code=400, detail=f"Invalid quality: {quality}")
    # Resolve manifest URL the same way /api/video/{id}/stream does
    # (kept inline so a single failure point has one place to
    # diagnose rather than two).
    try:
        if quality:
            resp = tidal.session.request.request(
                "GET",
                f"videos/{video_id}/urlpostpaywall",
                params={
                    "urlusagemode": "STREAM",
                    "videoquality": quality,
                    "assetpresentation": "FULL",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            urls = payload.get("urls") if isinstance(payload, dict) else None
            manifest_url = urls[0] if isinstance(urls, list) and urls else None
        else:
            video = tidal.session.video(video_id)
            manifest_url = video.get_url()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if not manifest_url:
        raise HTTPException(status_code=404, detail="No playback URL available")

    # Fetch metadata for filename + payload. Cheap — one HTTP call via
    # tidalapi, cached by the server session.
    try:
        video = tidal.session.video(video_id)
        title = getattr(video, "name", None) or f"Video {video_id}"
        artist = ""
        artists = getattr(video, "artists", None)
        if artists:
            artist = ", ".join(
                a.name for a in artists if getattr(a, "name", None)
            )
        duration = getattr(video, "duration", None)
    except Exception:
        title = f"Video {video_id}"
        artist = ""
        duration = None

    output_dir = Path(settings.videos_dir)
    job = video_downloader.start(
        video_id=video_id,
        manifest_url=manifest_url,
        title=title,
        artist=artist,
        output_dir=output_dir,
        duration_s=float(duration) if duration else None,
    )
    return video_downloader.status(video_id) or {
        "video_id": video_id,
        "state": job.state,
    }


@app.get("/api/video/{video_id}/download")
def video_download_status(video_id: int) -> dict:
    _require_local_access()
    from app import video_downloader

    s = video_downloader.status(video_id)
    if s is None:
        return {"video_id": video_id, "state": "idle"}
    return s


@app.get("/api/video/downloads")
def video_downloads_list() -> list[dict]:
    _require_local_access()
    from app import video_downloader

    return video_downloader.list_all()


@app.get("/api/autostart")
def autostart_status() -> dict:
    """Report whether the app is registered to launch at login.

    `available` is False in dev mode (no frozen exe path); the UI
    grays out the toggle in that case.
    """
    _require_local_access()
    from app import autostart
    return autostart.status()


@app.put("/api/autostart")
def autostart_set(req: _AutostartRequest) -> dict:
    _require_local_access()
    from app import autostart
    try:
        return autostart.set_enabled(req.enabled)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/notify", include_in_schema=False)
def fire_notification(req: _NotifyRequest, request: Request) -> dict:
    """Fire an OS-level notification. Loopback-only.

    The frontend owns the "should I notify?" decision because it has
    the context the backend doesn't — track title/artist, whether the
    window is focused, which user preference is set. The server is
    just a thin shim that exposes the platform-specific notification
    shell so this can run from inside a sandbox where the browser
    Notification API isn't available (pywebview's WKWebView doesn't
    surface it as system-level).
    """
    client = request.client
    host = client.host if client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403)
    from app.notify import notify as _notify
    _notify(req.title, req.body, req.subtitle)
    return {"ok": True}


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


@app.post("/api/auth/login/inapp/start")
def auth_login_inapp_start() -> dict:
    """Ask the desktop shell to open an in-app pywebview window pointed
    at Tidal's PKCE login URL. The shell watches for navigation to
    `tidal://login/auth?...`, captures that URL, and posts it back
    through /api/auth/pkce/complete — all without the user ever
    seeing the "Oops" page or having to paste anything.

    Only available when the packaged app is running. In `./run.sh`
    dev mode there's no pywebview shell to call back into, so we
    return `supported: false` and the frontend falls back to the
    classic open-browser-and-paste flow.
    """
    if _inapp_login_callback is None:
        return {"supported": False}
    _inapp_login_state["phase"] = "active"
    try:
        _inapp_login_callback(tidal.pkce_login_url())
    except Exception as exc:
        _inapp_login_state["phase"] = "idle"
        raise HTTPException(status_code=500, detail=str(exc))
    return {"supported": True}


@app.get("/api/auth/login/inapp/state")
def auth_login_inapp_state() -> dict:
    """Surface the in-app login window's state so the frontend can
    react when the shell aborts early. phases:
      - idle: no attempt in progress or shell not available
      - active: window is open, user is signing in
      - aborted_sso: shell closed the window because it detected
        a navigation into an SSO provider WKWebView can't render
        (Windows / Linux fallback path only)
      - closed: user or shell closed the window for another reason
      - unauthorized: macOS Safari-polling path only. User denied
        the Automation permission prompt, so we can't watch for the
        redirect and the frontend falls back to the paste flow."""
    return {"phase": _inapp_login_state.get("phase", "idle")}


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
    ok, reason = tidal.complete_pkce_login(req.redirect_url.strip())
    if not ok:
        base = (
            "PKCE login failed. Double-check that you pasted the URL "
            "from the Oops page immediately after logging in."
        )
        raise HTTPException(
            status_code=401,
            detail=f"{base} ({reason})" if reason else base,
        )
    _invalidate_auth_cache()
    _invalidate_preview_cache()
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
    _invalidate_lastfm_cache()
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
    _invalidate_lastfm_cache()
    return {"connected": True, "username": username}


@app.post("/api/lastfm/disconnect")
def lastfm_disconnect() -> dict:
    _require_auth()
    lastfm.disconnect()
    _invalidate_lastfm_cache()
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


# Stats-page Last.fm fetches get coalesced behind a short TTL. Each
# StatsPage mount fires user-info + top-artists + top-tracks +
# top-albums + loved-tracks + three charts, and Last.fm's rate budget
# is tight enough that a user who revisits the page every minute can
# easily saturate the concurrency semaphore. Stats move slowly; five
# minutes is invisible and cuts the request volume by ~90% on
# typical browsing.
_lastfm_cache: dict[str, tuple[float, Any]] = {}
_lastfm_cache_lock = threading.Lock()
_LASTFM_CACHE_TTL_SEC = 300.0


def _lastfm_cached(
    key: str, fetch, ttl_sec: float = _LASTFM_CACHE_TTL_SEC,
    persistent: bool = False,
):
    """Scope the cache to (username, endpoint, args). Username is part
    of the key so reconnecting to a different account doesn't serve
    the previous user's data, and disconnect clears the whole map.

    `ttl_sec` defaults to the shared 5-minute TTL but callers can
    override when the underlying data changes slowly enough that a
    longer cache is worthwhile (e.g. the resolved chart, whose cold
    load takes ~18 s).

    `persistent=True` adds a SQLite-backed second layer at
    `user_data_dir()/lastfm_disk_cache.db`. The in-memory hit stays
    the hot path (microseconds); on a miss we check disk before
    paying the upstream cost; on a successful upstream fetch we
    populate both. The motivating case is the resolved chart — its
    1-hour TTL was already long, but the in-memory dict died on
    every app restart so anyone who quit Tideway between visits paid
    the full 18 seconds again. With persistence, the cold load
    happens once per real cache miss instead of once per process
    lifetime.
    """
    from app import lastfm_disk_cache

    username = lastfm.status().get("username") or ""
    full_key = f"{username}|{key}"
    now = time.monotonic()
    # 1. Memory hit — hot path.
    with _lastfm_cache_lock:
        cached = _lastfm_cache.get(full_key)
        if cached and (now - cached[0]) < ttl_sec:
            return cached[1]
    # 2. Disk hit (only if the caller opted in). Promote to memory
    #    so subsequent lookups in this process skip the disk read.
    if persistent:
        disk_value = lastfm_disk_cache.get(full_key, ttl_sec)
        if disk_value is not None:
            with _lastfm_cache_lock:
                _lastfm_cache[full_key] = (now, disk_value)
            return disk_value
    # 3. Real fetch.
    data = fetch()
    with _lastfm_cache_lock:
        _lastfm_cache[full_key] = (now, data)
    if persistent:
        lastfm_disk_cache.set(full_key, data)
    return data


def _invalidate_lastfm_cache() -> None:
    """Drop both cache layers. Called when the user disconnects from
    Last.fm (or reconnects under a different account); keeping the
    previous account's results around would be a privacy bug."""
    from app import lastfm_disk_cache

    with _lastfm_cache_lock:
        _lastfm_cache.clear()
    lastfm_disk_cache.clear()


@app.get("/api/lastfm/user-info")
def lastfm_user_info() -> dict:
    """Header profile data for the Stats page — playcount, registered
    date, avatar. Empty dict if Last.fm isn't connected."""
    _require_auth()
    return _lastfm_cached("user-info", lastfm.get_user_info)


@app.get("/api/lastfm/top-artists")
def lastfm_top_artists(period: str = "overall", limit: int = 50) -> list[dict]:
    _require_auth()
    p = _validate_period(period)
    return _lastfm_cached(
        f"top-artists:{p}:{limit}",
        lambda: lastfm.get_top_artists(period=p, limit=limit),
    )


@app.get("/api/lastfm/top-tracks")
def lastfm_top_tracks(period: str = "overall", limit: int = 50) -> list[dict]:
    _require_auth()
    p = _validate_period(period)
    return _lastfm_cached(
        f"top-tracks:{p}:{limit}",
        lambda: lastfm.get_top_tracks(period=p, limit=limit),
    )


@app.get("/api/lastfm/top-albums")
def lastfm_top_albums(period: str = "overall", limit: int = 50) -> list[dict]:
    _require_auth()
    p = _validate_period(period)
    return _lastfm_cached(
        f"top-albums:{p}:{limit}",
        lambda: lastfm.get_top_albums(period=p, limit=limit),
    )


@app.get("/api/lastfm/loved-tracks")
def lastfm_loved_tracks(limit: int = 50) -> list[dict]:
    _require_auth()
    return _lastfm_cached(
        f"loved-tracks:{limit}",
        lambda: lastfm.get_loved_tracks(limit=limit),
    )


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


class _LastFmTrackPlaycountBatchItem(BaseModel):
    artist: str
    track: str


class _LastFmTrackPlaycountBatchRequest(BaseModel):
    items: list[_LastFmTrackPlaycountBatchItem]


@app.post("/api/lastfm/track-playcounts")
def lastfm_track_playcounts(req: _LastFmTrackPlaycountBatchRequest) -> dict:
    """Batched variant of /api/lastfm/track-playcount.

    The frontend's useLastfmTrackPlaycount hook coalesces any
    same-tick requests from a rendering track list into one POST to
    this endpoint, so a 50-row album hits Last.fm through a single
    HTTP call from the UI's perspective even though each row still
    maps to its own Last.fm API request on the backend. Rate limit
    pressure on Last.fm stays the same; what this avoids is the
    50-parallel-fetch storm the browser would otherwise open and
    the request-queue churn that comes with it.

    Response shape mirrors the per-row endpoint: a dict keyed by
    "artist|track" (lowercased) so the frontend can look entries up
    without having to rebuild the request key.
    """
    _require_auth()
    results: dict[str, dict] = {}
    # Dedupe by key before hitting Last.fm; callers can submit the
    # same (artist, track) multiple times when a track appears on
    # several playlists rendered simultaneously.
    seen: set[tuple[str, str]] = set()
    for item in req.items:
        if not item.artist or not item.track:
            continue
        key_pair = (item.artist.lower(), item.track.lower())
        if key_pair in seen:
            continue
        seen.add(key_pair)
        try:
            val = lastfm.get_track_playcount(item.artist, item.track)
        except Exception:
            val = {}
        results[f"{key_pair[0]}|{key_pair[1]}"] = val
    return {"results": results}


# ---------------------------------------------------------------------------
# Spotify public-data enrichment
#
# Complements Last.fm rather than replacing it. Last.fm remains the
# source for personal listening history (user scrobbles, per-user
# playcounts, stats page, history page). Spotify adds GLOBAL
# popularity signals Last.fm can't match — billion-scale track
# play counts and artist monthly-listener counts pulled directly
# from Spotify's own Web Player GraphQL (via spotapi).
#
# All access is ISRC-mediated: Tidal track → ISRC → Spotify track.
# See app/spotify_public.py for the caching + fallback story.
# ---------------------------------------------------------------------------


@app.get("/api/spotify/track-playcount")
def spotify_track_playcount(isrc: str) -> dict:
    """Global Spotify play count for the given ISRC. `{playcount: null}`
    when Spotify doesn't recognize the recording or when the public
    API is unreachable — callers should degrade silently rather than
    surface errors."""
    _require_auth()
    if not isrc:
        raise HTTPException(status_code=400, detail="isrc is required")
    try:
        from app import spotify_public
        return {"playcount": spotify_public.playcount_by_isrc(isrc)}
    except Exception as exc:
        logger.warning("spotify playcount fetch failed: %s", exc)
        return {"playcount": None}


class _TrackPlaycountItem(BaseModel):
    isrc: str
    title: Optional[str] = None
    artist: Optional[str] = None


class _TrackPlaycountsRequest(BaseModel):
    tracks: list[_TrackPlaycountItem]
    refresh: Optional[bool] = False


def _run_playcount_batch(
    codes: list[str],
    metadata: Optional[dict[str, tuple[str, str]]] = None,
    refresh: bool = False,
) -> dict[str, Optional[int]]:
    """Resolve a list of ISRCs to Spotify playcounts through a bounded
    thread pool. `metadata` supplies the optional title + artist per
    ISRC so the fuzzy-fallback path can activate when Spotify's ISRC
    search misses. `refresh=True` drops stale nulls/zeros first so
    Popular-style pages self-heal entries cached as 0 during a
    release-week lull. Shared between the POST and GET variants so
    their pool size, error envelope, and caching semantics stay in
    lockstep.
    """
    if not codes:
        return {}
    from app import spotify_public

    if refresh:
        try:
            spotify_public.purge_null_playcounts(codes)
        except Exception as exc:
            logger.warning("playcount null-cache flush failed: %s", exc)

    def _one(code: str) -> tuple[str, Optional[int]]:
        try:
            if metadata:
                title, artist = metadata.get(code, ("", ""))
                if title and artist:
                    return code, spotify_public.playcount_with_fallback(
                        code, title, artist
                    )
            return code, spotify_public.playcount_by_isrc(code)
        except Exception:
            return code, None

    try:
        with ThreadPoolExecutor(max_workers=min(5, len(codes))) as pool:
            return dict(pool.map(_one, codes))
    except Exception as exc:
        logger.warning("spotify track-playcounts batch failed: %s", exc)
        return {c: None for c in codes}


@app.post("/api/spotify/track-playcounts")
def spotify_track_playcounts_batch(body: _TrackPlaycountsRequest) -> dict:
    """Batched Spotify playcount lookup with fuzzy title+artist
    fallback when Spotify's ISRC search misses (covers
    feature-version ISRCs that haven't been indexed yet)."""
    _require_auth()
    codes: list[str] = []
    metadata: dict[str, tuple[str, str]] = {}
    for t in body.tracks or []:
        code = t.isrc.strip().upper()
        if not code:
            continue
        codes.append(code)
        metadata[code] = (t.title or "", t.artist or "")
    return {
        "playcounts": _run_playcount_batch(
            codes, metadata=metadata, refresh=bool(body.refresh)
        )
    }


@app.get("/api/spotify/track-playcounts")
def spotify_track_playcounts(isrcs: str, refresh: bool = False) -> dict:
    """Simpler GET variant without title/artist context — same
    pooling and caching as the POST form, no fuzzy fallback."""
    _require_auth()
    if not isrcs:
        raise HTTPException(status_code=400, detail="isrcs is required")
    codes = [c.strip().upper() for c in isrcs.split(",") if c.strip()]
    return {"playcounts": _run_playcount_batch(codes, refresh=refresh)}


@app.get("/api/spotify/album-total-plays")
def spotify_album_total_plays(isrcs: str) -> dict:
    """Sum Spotify's per-track play counts across an album.

    `isrcs` is a comma-separated list (e.g. `?isrcs=USUM7170...,USUM7170...`).
    Returns `{total_plays, resolved, total}` so the frontend can
    decide whether the number is complete or partial.

    First call is slow (~0.5s per un-cached track); subsequent calls
    hit the SQLite cache. Frontend should fire this once per album
    page and share the result.
    """
    _require_auth()
    if not isrcs:
        raise HTTPException(status_code=400, detail="isrcs is required")
    codes = [c for c in isrcs.split(",") if c.strip()]
    if not codes:
        return {"total_plays": 0, "resolved": 0, "total": 0}
    try:
        from app import spotify_public
        return spotify_public.album_total_plays(codes)
    except Exception as exc:
        logger.warning("spotify album-total-plays fetch failed: %s", exc)
        return {"total_plays": 0, "resolved": 0, "total": len(codes)}


@app.get("/api/spotify/artist-stats")
def spotify_artist_stats(
    tidal_artist_id: str,
    sample_isrc: str = "",
    sample_isrcs: str = "",
    tidal_artist_name: str = "",
) -> dict:
    """Spotify artist overview — monthly listeners, followers, world
    rank, top cities. `tidal_artist_id` keys the cache.

    The newer client passes `tidal_artist_name` plus a comma-separated
    `sample_isrcs` so the resolver can prefer the ISRC whose primary
    Spotify artist actually matches the Tidal artist's name. The
    older single-`sample_isrc` form is still accepted for back-compat
    and silently falls back to the legacy "first candidate from one
    ISRC" path, which gets the wrong artist when the chosen track is
    a feature credit.

    Returns an empty-ish dict (`{monthly_listeners: null, ...}`) when
    Spotify can't resolve the artist so the frontend can render the
    section as "not available" rather than throwing.
    """
    _require_auth()
    isrcs: list[str] = [
        c.strip().upper()
        for c in (sample_isrcs or "").split(",")
        if c.strip()
    ]
    if not isrcs and sample_isrc:
        isrcs = [sample_isrc.strip().upper()]
    if not tidal_artist_id or not isrcs:
        raise HTTPException(
            status_code=400,
            detail=(
                "tidal_artist_id is required, plus at least one of "
                "sample_isrc or sample_isrcs"
            ),
        )
    try:
        from app import spotify_public

        if tidal_artist_name:
            stats = spotify_public.artist_stats_v2(
                tidal_artist_id, tidal_artist_name, isrcs
            )
        else:
            # Legacy single-ISRC, no-name path — preserved for older
            # cached clients still on /api/spotify/artist-stats?sample_isrc=.
            # Known to mis-resolve when the sample track is a feature.
            stats = spotify_public.artist_stats(tidal_artist_id, isrcs[0])
    except Exception as exc:
        logger.warning("spotify artist-stats fetch failed: %s", exc)
        return {
            "monthly_listeners": None,
            "followers": None,
            "world_rank": None,
            "top_cities": [],
        }
    if stats is None:
        return {
            "monthly_listeners": None,
            "followers": None,
            "world_rank": None,
            "top_cities": [],
        }
    return stats.to_dict()


@app.get("/api/debug/playcount-trace")
def debug_playcount_trace(q: str = "", isrc: str = "") -> dict:
    """Diagnose a missing Spotify playcount for a specific track.

    Pass either `?q=<title artist>` (searches Tidal, picks the first
    track, reports its ISRC) or `?isrc=...` directly. The endpoint
    then walks the ISRC-to-playcount resolution WITHOUT touching the
    cache and also reports what's currently cached in the SQLite DB
    so you can tell cached-null from transient-failure.
    """
    _require_auth()
    report: dict = {"query": q, "requested_isrc": isrc}

    resolved_isrc: str = ""
    if isrc:
        resolved_isrc = isrc.strip().upper()
    elif q:
        try:
            results = tidal.search(q, limit=3)
            tracks = results.get("tracks", []) or []
            report["tidal_hits"] = [
                {
                    "id": getattr(t, "id", None),
                    "name": getattr(t, "name", ""),
                    "artists": [
                        getattr(a, "name", "")
                        for a in (getattr(t, "artists", None) or [])
                    ],
                    "isrc": (getattr(t, "isrc", "") or "").strip().upper() or None,
                }
                for t in tracks
            ]
            if tracks:
                resolved_isrc = (
                    (getattr(tracks[0], "isrc", "") or "").strip().upper()
                )
        except Exception as exc:
            report["tidal_search_error"] = f"{exc!r}"
    if not resolved_isrc:
        report["verdict"] = "no_isrc"
        return report

    report["isrc"] = resolved_isrc

    # Inspect the SQLite cache directly so we can distinguish "cached
    # null keeping the track dark" from "uncached miss". sqlite3.connect
    # creates an empty DB if the file doesn't exist; that's harmless
    # here and avoids a TOCTOU window.
    try:
        import sqlite3 as _sqlite3
        from app.paths import user_data_dir as _udir
        db_path = _udir() / "spotify_public_cache.db"
        conn = _sqlite3.connect(str(db_path), timeout=5.0)
        try:
            try:
                row = conn.execute(
                    "SELECT playcount, fetched_at FROM track_playcount WHERE isrc=?",
                    (resolved_isrc,),
                ).fetchone()
            except _sqlite3.OperationalError:
                # Table doesn't exist yet — the cache DB was just
                # created by our connect() call. Treat as "no entry".
                row = None
            if row is not None:
                report["cache"] = {
                    "playcount": row[0],
                    "fetched_at": row[1],
                    "age_seconds": (
                        int(time.time()) - int(row[1]) if row[1] else None
                    ),
                }
            else:
                report["cache"] = None
            try:
                row2 = conn.execute(
                    "SELECT spotify_track_id, fetched_at FROM isrc_to_spotify_track "
                    "WHERE isrc=?",
                    (resolved_isrc,),
                ).fetchone()
            except _sqlite3.OperationalError:
                row2 = None
            if row2 is not None:
                report["isrc_cache"] = {
                    "spotify_track_id": row2[0],
                    "fetched_at": row2[1],
                }
            else:
                report["isrc_cache"] = None
        finally:
            conn.close()
    except Exception as exc:
        report["cache_error"] = f"{exc!r}"

    # Live probe: search Spotify for this ISRC and walk each candidate.
    from app import spotify_public

    try:
        song, _ = spotify_public._ensure_client()
        res = song.query_songs(f"isrc:{resolved_isrc}", limit=5)
    except Exception as exc:
        report["spotify_search_error"] = f"{exc!r}"
        return report

    items = (
        (res.get("data") or {})
        .get("searchV2", {})
        .get("tracksV2", {})
        .get("items")
        or []
    )
    candidates: list[dict] = []
    for entry in items:
        item = (entry.get("item") or {}).get("data") or {}
        uri = item.get("uri") or ""
        if not uri.startswith("spotify:track:"):
            continue
        track_id = uri.split(":")[-1]
        info: dict = {
            "spotify_track_id": track_id,
            "name": item.get("name") or "",
            "playcount": None,
            "error": None,
        }
        try:
            payload = spotify_public._song_info(track_id)
            pc_raw = (payload.get("data") or {}).get("trackUnion", {}).get("playcount")
            try:
                info["playcount"] = int(pc_raw) if pc_raw is not None else None
            except (TypeError, ValueError):
                info["playcount"] = None
            info["playcount_raw"] = pc_raw
        except Exception as exc:
            info["error"] = f"{exc!r}"
        candidates.append(info)
    report["spotify_candidates"] = candidates

    if not candidates:
        report["verdict"] = "spotify_no_hits"
    elif all(c["playcount"] is None for c in candidates):
        report["verdict"] = "spotify_no_playcount_field"
    else:
        report["verdict"] = "ok"
    return report


@app.post("/api/debug/clear-artist-cache/{tidal_artist_id}")
def debug_clear_artist_cache(tidal_artist_id: str) -> dict:
    """Force-reset the cached Tidal→Spotify artist mapping for one
    artist. Use after the artist's stats line is showing as blank
    because of a stale null cache from before the null-TTL fix."""
    _require_auth()
    import sqlite3
    from app.paths import user_data_dir

    db_path = user_data_dir() / "spotify_public_cache.db"
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        try:
            cur = conn.execute(
                "DELETE FROM tidal_to_spotify_artist WHERE tidal_artist_id=?",
                (str(tidal_artist_id),),
            )
            deleted = cur.rowcount
            conn.commit()
        except sqlite3.OperationalError:
            # DB or table didn't exist — nothing cached to clear.
            return {"ok": False, "reason": "no cache db yet"}
    finally:
        conn.close()
    return {"ok": True, "rows_deleted": deleted}


@app.get("/api/tidal/backoff")
def tidal_backoff() -> dict:
    """Current Tidal-backoff state. The request gate in
    app/tidal_client.py trips this after HTTP 429 or an
    `abuse_detected` 403; every Tidal call raises TidalBackoffError
    while it's active, so the UI needs to know to stop firing
    non-essential fetches and surface a banner explaining why
    things look frozen."""
    return tidal_backoff_state()


@app.get("/api/debug/artist-resolve/{tidal_artist_id}")
def debug_artist_resolve(tidal_artist_id: str) -> dict:
    """Diagnose why an artist page's monthly-listeners or personal
    play-count line is blank.

    Walks the full resolution pipeline for the given Tidal artist id
    without hitting the Spotify cache, plus the Last.fm artist-play
    lookup, and returns a structured JSON report showing where each
    half of the hero stat line succeeded or fell over. Reachable at
    `http://127.0.0.1:47823/api/debug/artist-resolve/<id>`.
    """
    _require_auth()
    from app import spotify_public

    report: dict = {"tidal_artist_id": str(tidal_artist_id)}

    try:
        artist = tidal.session.artist(int(tidal_artist_id))
        artist_name = getattr(artist, "name", "") or ""
    except Exception as exc:
        return {
            **report,
            "error": f"Tidal session.artist failed: {exc!r}",
        }
    report["tidal_artist_name"] = artist_name

    # Path A: top tracks via the tidalapi method directly. Capture the
    # exception text instead of swallowing — that's the data we need
    # to know whether Tidal returned [] or threw.
    top_tracks: list = []
    try:
        top_tracks = list(artist.get_top_tracks(limit=10))
        report["top_tracks_source"] = "artist.get_top_tracks"
    except Exception as exc:
        report["top_tracks_error"] = f"artist.get_top_tracks: {exc!r}"

    # Path B fallback: walk the first few albums' tracks. Massive
    # artists sometimes have a flaky top_tracks endpoint but their
    # albums-list resolves fine.
    if not top_tracks:
        try:
            albums = list(tidal.get_artist_albums(artist))[:2]
            for alb in albums:
                try:
                    top_tracks.extend(list(alb.tracks())[:5])
                except Exception as exc:
                    report.setdefault("album_track_errors", []).append(
                        f"{getattr(alb, 'name', '')}: {exc!r}"
                    )
                if len(top_tracks) >= 10:
                    break
            if top_tracks:
                report["top_tracks_source"] = "fallback:album_tracks"
        except Exception as exc:
            report["album_walk_error"] = f"{exc!r}"

    sample_isrcs: list[str] = []
    top_tracks_preview: list[dict] = []
    for t in top_tracks[:10]:
        isrc = (getattr(t, "isrc", "") or "").strip().upper()
        track_artists = [
            getattr(a, "name", "") for a in (getattr(t, "artists", None) or [])
        ]
        top_tracks_preview.append(
            {
                "name": getattr(t, "name", ""),
                "artists": track_artists,
                "isrc": isrc or None,
            }
        )
        if isrc and isrc not in sample_isrcs:
            sample_isrcs.append(isrc)
    report["top_tracks"] = top_tracks_preview
    report["isrc_count"] = len(sample_isrcs)

    report["spotify"] = spotify_public.debug_resolve_artist(
        str(tidal_artist_id), artist_name, sample_isrcs
    )

    # Last.fm side — we query by artist name, so report exactly what
    # we sent and what came back.
    lastfm_info: dict = {"queried_name": artist_name, "enabled": False}
    try:
        status = lastfm.status()
        lastfm_info["enabled"] = bool(status.get("has_credentials"))
        lastfm_info["username"] = status.get("username")
    except Exception as exc:
        lastfm_info["status_error"] = f"{exc!r}"
    if lastfm_info["enabled"] and artist_name:
        try:
            pc = lastfm.get_artist_playcount(artist_name)
            lastfm_info["playcount_payload"] = pc
            lastfm_info["userplaycount"] = (
                pc.get("userplaycount") if isinstance(pc, dict) else None
            )
        except Exception as exc:
            lastfm_info["playcount_error"] = f"{exc!r}"
    report["lastfm"] = lastfm_info

    return report


@app.get("/api/lastfm/chart/top-artists")
def lastfm_chart_top_artists(limit: int = 50) -> list[dict]:
    _require_auth()
    return _lastfm_cached(
        f"chart-top-artists:{limit}",
        lambda: lastfm.get_chart_top_artists(limit=limit),
    )


@app.get("/api/lastfm/chart/top-tracks")
def lastfm_chart_top_tracks(limit: int = 50) -> list[dict]:
    _require_auth()
    return _lastfm_cached(
        f"chart-top-tracks:{limit}",
        lambda: lastfm.get_chart_top_tracks(limit=limit),
    )


@app.get("/api/lastfm/chart/top-tracks-resolved")
def lastfm_chart_top_tracks_resolved(limit: int = 50) -> list[dict]:
    """Last.fm top tracks already resolved to Tidal Track objects.

    The Popular page's Tracks tab used to fire N separate search
    requests from the browser to resolve each Last.fm entry —
    cold-load was 5-10 s for 50 rows. We now do the whole fan-out
    server-side with a bounded thread pool and cache the result, so
    the client is back to one round-trip.
    """
    _require_auth()
    limit = max(1, min(limit, 100))

    def _resolve_all() -> list[dict]:
        entries = lastfm.get_chart_top_tracks(limit=limit)

        pref = (settings.explicit_content_preference or "explicit").lower()

        def _one(entry: dict) -> Optional[dict]:
            tidal_jitter_sleep()
            title = (entry.get("name") or "").strip()
            artist = (entry.get("artist") or "").strip()
            if not title or not artist:
                return None
            try:
                results = tidal.search(f"{artist} {title}", limit=5)
            except Exception:
                return None
            tracks = filter_explicit_dupes(
                results.get("tracks", []), pref, kind="track"
            )
            if not tracks:
                return None
            # Exact title + artist first; fall back to Tidal's top hit.
            wt = title.lower()
            wa = artist.lower()
            exact = next(
                (
                    t for t in tracks
                    if getattr(t, "name", "").lower() == wt
                    and any(
                        getattr(a, "name", "").lower() == wa
                        for a in (getattr(t, "artists", None) or [])
                    )
                ),
                None,
            )
            return track_to_dict(exact or tracks[0])

        # 3 workers. The whole-chart resolve was one of the heavier
        # bursts of Tidal traffic per user session — 50 searches in
        # ~7 s is exactly the kind of pattern that trips abuse
        # detection over time. Slower wall-clock (~18 s cold load)
        # but the result is cached so subsequent visits are instant.
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(_one, entries))

        return [r for r in results if r is not None]

    # 1-hour TTL with disk persistence. The Last.fm global chart
    # turns over slowly, and the cold-load cost (~18 s for 50 rows
    # against Tidal at 3 workers) is expensive enough that we don't
    # want to pay it again on every app restart. The in-memory
    # layer alone died with the process; the SQLite-backed layer
    # rides through restarts so the user only pays the resolve
    # once per hour even across launches.
    return _lastfm_cached(
        f"chart-top-tracks-resolved:{limit}",
        _resolve_all,
        ttl_sec=3600.0,
        persistent=True,
    )


@app.get("/api/lastfm/chart/top-tags")
def lastfm_chart_top_tags(limit: int = 50) -> list[dict]:
    _require_auth()
    return _lastfm_cached(
        f"chart-top-tags:{limit}",
        lambda: lastfm.get_chart_top_tags(limit=limit),
    )


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


@app.get("/api/play-report/log")
def play_report_log() -> dict:
    """Return the rolling buffer of recent play-report attempts.

    Used by the Settings "Diagnose play reporting" panel so users can
    see whether events are reaching Tidal without grepping stderr.
    Each entry has ts_ms, phase (sent/skipped), track_id, http_status,
    listened_s, and an optional note (error body or skip reason).
    """
    _require_local_access()
    return {"entries": play_report_recent_log()}


class _PlayReportDiagnoseRequest(BaseModel):
    """Optional track_id to synthesize a play for. Defaults to a known
    Tidal catalog track (Daft Punk — Get Lucky, track_id 77748546) so
    the diagnose button works even when the user hasn't played anything
    yet in this session."""
    track_id: Optional[int] = None


@app.post("/api/play-report/diagnose")
def play_report_diagnose(req: _PlayReportDiagnoseRequest) -> dict:
    """Fire a synthetic playback_session event NOW and wait briefly
    for the reporter to process it. Returns the resulting log entry
    so the UI can show status / note inline.

    Uses a 30-second fake listen (well above the 30s / 50% threshold
    Tidal applies before a play counts for Recently Played) and marks
    it as "user_trigger" source so it stands out from real plays.
    """
    _require_auth()
    track_id = str(req.track_id or 77748546)
    now_ms = int(time.time() * 1000)
    # sourceType must be one of Tidal's enum values (ALBUM, ARTIST,
    # MIX, PLAYLIST, TRACK, etc.) — "user_trigger" was not valid and
    # likely caused Tidal's aggregation pipeline to silently drop the
    # event from Recently Played even though HTTP returned 200. "TRACK"
    # with sourceId = the track itself is what real single-track taps
    # report, so it's the right fallback for a synthetic diagnose too.
    synthetic = PlaySession(
        session_id=str(uuid.uuid4()),
        track_id=track_id,
        quality="LOSSLESS",
        source_type="TRACK",
        source_id=track_id,
        start_ts_ms=now_ms - 30_000,
        end_ts_ms=now_ms,
        start_position_s=0.0,
        end_position_s=30.0,
    )
    before = len(play_report_recent_log())
    play_reporter.record(synthetic)
    # Poll the log for up to 5s for the new entry to land. Background
    # reporter thread typically processes within a few hundred ms.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        entries = play_report_recent_log()
        if len(entries) > before:
            return {"ok": True, "entry": entries[-1]}
        time.sleep(0.1)
    return {"ok": False, "reason": "reporter didn't process within 5s"}


@app.post("/api/auth/logout")
def auth_logout() -> dict:
    # Stop playback before the session goes away. Without this, an
    # already-buffered track keeps playing after logout — disorienting,
    # and the next track-end auto-advance fails (no session to resolve
    # the next stream URL).
    if _pcm_player_singleton is not None:
        try:
            _pcm_player_singleton.stop()
        except Exception:
            pass
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
# Native audio player (PyAV + sounddevice)
#
# Decodes DASH / local audio with PyAV and drives a sounddevice
# OutputStream at the track's native sample rate. Gapless
# transitions via the preload → inline-swap path in PCMPlayer. The
# frontend is a remote control: it POSTs commands and reads state
# via GET /api/player/state (one-shot) or subscribes to
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


class _PlayerEqRequest(BaseModel):
    # Empty list disables EQ entirely.
    bands: list[float]
    preamp: Optional[float] = None


class _PlayerEqPresetRequest(BaseModel):
    preset: int


class _PlayerEqEnabledRequest(BaseModel):
    enabled: bool


class _PlayerOutputDeviceRequest(BaseModel):
    # Empty string routes to the system default.
    device_id: str


_player_bootstrapped = False
_pcm_player_singleton: Optional[PCMPlayer] = None


def _native_player() -> PCMPlayer:
    """Return the PCMPlayer singleton. Lazily constructed on first
    call; subsequent calls reuse it for the lifetime of the process.
    """
    global _player_bootstrapped, _pcm_player_singleton

    if _pcm_player_singleton is None:
        try:
            import av  # noqa: F401
            import sounddevice  # noqa: F401
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Audio engine unavailable: {exc}",
            )
        _pcm_player_singleton = PCMPlayer(
            lambda: tidal.session,
            local_lookup=lambda tid: str(local_index.get(str(tid)))
            if local_index.get(str(tid))
            else None,
            quality_clamp=tidal.clamp_quality_to_subscription,
        )
        # Mirror state changes into macOS Now Playing so media keys
        # can find us. update_state() no-ops on non-macOS / when the
        # MediaPlayer framework isn't available, so unconditional
        # subscription is safe.
        _pcm_player_singleton.subscribe(macos_now_playing_bridge.update_state)

    # One-shot: re-apply persisted EQ + output device so users who
    # set a USB-DAC preference or an EQ preset keep it across restart.
    if not _player_bootstrapped:
        _player_bootstrapped = True
        try:
            if settings.eq_enabled and settings.eq_bands:
                _pcm_player_singleton.apply_equalizer(
                    settings.eq_bands, preamp=settings.eq_preamp
                )
            if settings.audio_output_device:
                _pcm_player_singleton.set_output_device(
                    settings.audio_output_device
                )
            if getattr(settings, "exclusive_mode", False):
                _pcm_player_singleton.set_exclusive_mode(True)
            if getattr(settings, "force_volume", False):
                _pcm_player_singleton.set_force_volume(True)
        except Exception as exc:
            print(f"[player] bootstrap failed: {exc}", flush=True)
    return _pcm_player_singleton


# ---------------------------------------------------------------------
# Tidal Connect dispatch
# ---------------------------------------------------------------------
#
# When a Tidal Connect session is active, audio plays on the remote
# device, not through PCMPlayer's local sounddevice output. Player
# endpoints (play_track, pause, play, seek) divert to the
# TidalConnectManager's transport methods. The state these endpoints
# return is synthesized from the manager's polled state (track id,
# position, duration) so the frontend's now-playing UI shows
# something coherent — title plays on the device, scrubber roughly
# tracks position via the 1s polling cadence.
#
# This is the integration layer for Tidal Connect's experimental
# release. End-to-end audio handoff is a bet on hypothesis A from
# docs/cast-and-connect-scope.md (signed Tidal stream URLs accepted
# as DIDL-Lite <res> content). Verified-without-hardware up to the
# SOAP layer; real-device feedback is what unblocks a non-experimental
# release.


def _tc_active() -> bool:
    """True if a Tidal Connect session is currently open. Cheap probe
    for the divert checks in the player endpoints."""
    try:
        from app.audio.tidal_connect import get_manager
        return get_manager().status().get("control_plane_ready", False) is True
    except Exception:
        return False


def _tc_snapshot(track_id: Optional[str] = None) -> dict:
    """Synthesize a player-snapshot-shaped dict from Tidal Connect
    state. Same fields the local PCMPlayer's snapshot produces, so
    the frontend's now-playing UI reads them identically without
    branching on which engine is active."""
    from app.audio.tidal_connect import get_manager

    mgr = get_manager()
    status = mgr.status()
    with mgr._session_lock:  # noqa: SLF001 — internal access for state read
        session = mgr._session
    if session is None:
        return {
            "state": "idle",
            "track_id": None,
            "position_ms": 0,
            "duration_ms": 0,
            "volume": 100,
            "muted": False,
            "error": None,
            "seq": 0,
            "stream_info": None,
            "force_volume": False,
        }
    return {
        "state": "playing" if session.current_track_id else "idle",
        "track_id": track_id,
        "position_ms": session.position_s * 1000,
        "duration_ms": session.duration_s * 1000,
        "volume": session.volume_percent,
        "muted": session.muted,
        "error": None,
        "seq": int(status.get("device_count", 0)),  # bumped on poll
        "stream_info": None,
        "force_volume": False,
    }


def _snapshot_dict(snap) -> dict:
    """Serialize a PlayerSnapshot into a JSON-friendly dict."""
    stream_info = None
    if snap.stream_info is not None:
        si = snap.stream_info
        stream_info = {
            "source": si.source,
            "codec": si.codec,
            "bit_depth": si.bit_depth,
            "sample_rate_hz": si.sample_rate_hz,
            "audio_quality": si.audio_quality,
            "audio_mode": si.audio_mode,
        }
    return {
        "state": snap.state,
        "track_id": snap.track_id,
        "position_ms": snap.position_ms,
        "duration_ms": snap.duration_ms,
        "volume": snap.volume,
        "muted": snap.muted,
        "error": snap.error,
        "seq": snap.seq,
        "stream_info": stream_info,
        "force_volume": getattr(snap, "force_volume", False),
    }


@app.get("/api/player/available")
def player_available() -> dict:
    """Feature-probe endpoint. True iff PyAV + sounddevice are
    importable — i.e., the audio engine can run."""
    pcm_available = False
    try:
        import av  # noqa: F401
        import sounddevice  # noqa: F401
        pcm_available = True
    except Exception:
        pass
    return {"available": pcm_available}


@app.get("/api/player/state")
def player_state() -> dict:
    # Local-access gate (not _require_auth) so offline users can
    # play their downloaded tracks. The load() path inside the
    # player checks local_index first and only falls through to
    # Tidal when a track isn't on disk.
    _require_local_access()
    return _snapshot_dict(_native_player().snapshot())


@app.get("/api/now-playing/state")
def now_playing_state_get() -> dict:
    """Backend backstop for the persisted now-playing snapshot.

    Pywebview's WKWebView on macOS doesn't always preserve
    localStorage between launches the way a regular browser tab
    does, so the frontend's "restore on quit" path can come up
    empty even when the user expects their track back. The
    frontend POSTs the same JSON it writes to localStorage to
    `/api/now-playing/state` (see below) on every persist tick;
    the server keeps the latest copy in `user_data_dir`. On
    startup the frontend reads it back via this GET and prefers
    it when localStorage is missing.

    Returns `{"state": null}` when nothing has been persisted yet.
    """
    _require_local_access()
    return {"state": now_playing_state.read_state()}


@app.put("/api/now-playing/state")
async def now_playing_state_put(request: Request) -> dict:
    """Push the frontend's persisted snapshot to disk. Server doesn't
    interpret the contents — it just round-trips the JSON. The
    frontend is the only consumer; durability across launches is
    the only contract we care about here.

    Accepts an empty body / null payload to mean "clear" — used
    when the user explicitly stops playback so a relaunch doesn't
    restore something they just dismissed.
    """
    _require_local_access()
    try:
        body = await request.json()
    except Exception:
        body = None
    if body is None or body == {} or not isinstance(body, dict):
        now_playing_state.clear_state()
        return {"ok": True, "cleared": True}
    now_playing_state.write_state(body)
    return {"ok": True}


class _NowPlayingMetadata(BaseModel):
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    artwork_url: str = ""


@app.post("/api/now-playing")
def now_playing_update(payload: _NowPlayingMetadata) -> dict:
    """Push the current track's display metadata into macOS Now
    Playing. Frontend hits this on track change so Control Center,
    the menu-bar widget, and the lock screen show the song title /
    artist / album / duration alongside the play state.

    No-ops on non-macOS or when the MediaPlayer framework isn't
    available; the bridge handles the platform check internally.
    Returns `{"ok": true}` either way so the frontend doesn't have
    to branch on platform.
    """
    _require_local_access()
    macos_now_playing_bridge.update_metadata(
        title=payload.title,
        artist=payload.artist,
        album=payload.album,
        duration_ms=payload.duration_ms,
        artwork_url=payload.artwork_url,
    )
    return {"ok": True}


@app.post("/api/player/load")
def player_load(req: _PlayerLoadRequest) -> dict:
    _require_local_access()
    snap = _native_player().load(req.track_id, quality=req.quality)
    return _snapshot_dict(snap)


@app.post("/api/player/play_track")
def player_play_track(req: _PlayerLoadRequest) -> dict:
    """Atomic load + play. Used by the auto-advance path so we
    don't pay two HTTP round-trips + two sequential awaits at
    track-end. Shorter code path = smaller perceptible gap.

    When a Tidal Connect session is active, this diverts to
    `tidal_connect_manager.load_track(track_id)` — the device fetches
    the audio from Tidal directly, PCMPlayer stays idle. Returns a
    synthesized snapshot in the same shape the local engine produces
    so the frontend reads it identically."""
    _require_local_access()
    if _tc_active():
        from app.audio.tidal_connect import get_manager
        try:
            get_manager().load_track(int(req.track_id))
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Tidal Connect: {exc}"
            ) from exc
        return _tc_snapshot(track_id=str(req.track_id))
    snap = _native_player().play_track(req.track_id, quality=req.quality)
    return _snapshot_dict(snap)


@app.post("/api/player/preload")
def player_preload(req: _PlayerLoadRequest) -> dict:
    """Pre-resolve the next track's manifest so auto-advance can
    skip the network fetch. Frontend fires this ~15s before the
    current track ends. Synchronous on the manifest fetch but
    called well in advance, so it doesn't race track-end.
    """
    _require_local_access()
    return _native_player().preload(req.track_id, quality=req.quality)


class _PlayerPrefetchRequest(BaseModel):
    track_ids: list[str]
    quality: Optional[str] = None
    # Whether to also pre-download the init + first media segment
    # for each track. On by default since the whole point of this
    # endpoint is to make the next click instant. Set false for
    # large background sweeps where network / memory cost matters
    # more than the snappiness gain.
    warm_bytes: bool = True


@app.get("/api/player/cache-stats")
def player_cache_stats() -> dict:
    """Inspect the stream-manifest cache. Useful while testing the
    prefetch path — compare hits/misses across cold/warm clicks, and
    confirm album-mount prefetch is actually landing in the cache."""
    _require_local_access()
    player = _native_player()
    stats = getattr(player, "cache_stats", None)
    if stats is None:
        return {"hits": 0, "misses": 0, "size": 0, "entries": []}
    return stats()


@app.post("/api/player/prefetch")
def player_prefetch(req: _PlayerPrefetchRequest) -> dict:
    """Warm the stream-manifest cache for a list of tracks so the
    next play-click skips the track→stream→manifest round-trips to
    Tidal. Called by the frontend on hover (single id) and on
    album / playlist mount (batched).

    Runs the resolves in parallel with a small worker pool, but
    caps at ~10 to respect tidalapi's implicit rate budget. Errors
    are swallowed per-track — prefetch is fire-and-forget.

    No-ops when offline mode is on — the user has explicitly opted
    out of network activity for browsing, and prefetch is pure
    speculative network."""
    _require_local_access()
    if settings.offline_mode:
        return {"prefetched": 0, "total": len(req.track_ids), "skipped": "offline"}
    ids = [tid for tid in req.track_ids if tid]
    if not ids:
        return {"prefetched": 0, "total": 0}
    player = _native_player()
    prefetch = getattr(player, "prefetch", None)
    if prefetch is None:
        return {"prefetched": 0, "total": len(ids)}
    with ThreadPoolExecutor(
        max_workers=min(3, len(ids)), thread_name_prefix="prefetch"
    ) as pool:
        results = list(
            pool.map(
                lambda tid: prefetch(tid, req.quality, warm_bytes=req.warm_bytes),
                ids,
            )
        )
    return {"prefetched": sum(1 for r in results if r), "total": len(ids)}


@app.post("/api/player/preload/clear")
def player_preload_clear() -> dict:
    """Drop the preload cache. Used by the frontend on quality
    changes so a cached-for-old-quality MPD doesn't get consumed
    by a subsequent load().
    """
    _require_local_access()
    _native_player()._drop_preload()
    return {"ok": True}


@app.post("/api/player/play")
def player_play() -> dict:
    _require_local_access()
    if _tc_active():
        from app.audio.tidal_connect import get_manager
        try:
            get_manager().play()
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Tidal Connect: {exc}"
            ) from exc
        return _tc_snapshot()
    return _snapshot_dict(_native_player().play())


@app.post("/api/player/pause")
def player_pause() -> dict:
    _require_local_access()
    if _tc_active():
        from app.audio.tidal_connect import get_manager
        try:
            get_manager().pause()
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Tidal Connect: {exc}"
            ) from exc
        return _tc_snapshot()
    return _snapshot_dict(_native_player().pause())


@app.post("/api/player/resume")
def player_resume() -> dict:
    _require_local_access()
    if _tc_active():
        from app.audio.tidal_connect import get_manager
        try:
            get_manager().play()
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Tidal Connect: {exc}"
            ) from exc
        return _tc_snapshot()
    return _snapshot_dict(_native_player().resume())


@app.post("/api/player/stop")
def player_stop() -> dict:
    _require_local_access()
    if _tc_active():
        # Stop on Tidal Connect just clears the queue + pauses the
        # device. Disconnecting is a separate user action through
        # the picker — stopping a track shouldn't tear down the
        # whole session.
        from app.audio.tidal_connect import get_manager
        try:
            get_manager().pause()
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Tidal Connect: {exc}"
            ) from exc
        return _tc_snapshot()
    return _snapshot_dict(_native_player().stop())


@app.post("/api/player/seek")
def player_seek(req: _PlayerSeekRequest) -> dict:
    _require_local_access()
    if _tc_active():
        from app.audio.tidal_connect import get_manager
        # Resolve the fractional position into seconds using the
        # last polled duration. SeekSecond is integer-only on the
        # device side, so float-second precision is lost — fine for
        # a UI scrubber.
        mgr = get_manager()
        with mgr._session_lock:  # noqa: SLF001
            session = mgr._session
        duration_s = session.duration_s if session else 0
        position_s = int(max(0.0, min(req.fraction, 1.0)) * duration_s)
        try:
            mgr.seek(position_s)
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Tidal Connect: {exc}"
            ) from exc
        return _tc_snapshot()
    return _snapshot_dict(_native_player().seek(req.fraction))


@app.post("/api/player/volume")
def player_volume(req: _PlayerVolumeRequest) -> dict:
    _require_local_access()
    return _snapshot_dict(_native_player().set_volume(req.volume))


@app.post("/api/player/muted")
def player_muted(req: _PlayerMutedRequest) -> dict:
    _require_local_access()
    return _snapshot_dict(_native_player().set_muted(req.muted))


@app.get("/api/player/eq")
def player_eq_state() -> dict:
    """Current EQ: persisted bands + preamp + enabled flag + the static
    list of presets + band frequencies so the frontend can render
    sliders.

    The active engine reports its own band layout + presets. The PCM
    engine's stub matches VLC's 10-band shape so the slider UI keeps
    rendering; presets will be empty until Phase 5 ships.
    """
    _require_local_access()
    return {
        "enabled": settings.eq_enabled,
        "bands": list(settings.eq_bands),
        "preamp": settings.eq_preamp,
        "band_count": PCMPlayer.eq_bands_count(),
        "frequencies": PCMPlayer.eq_band_frequencies(),
        "presets": PCMPlayer.eq_presets(),
    }


@app.post("/api/player/eq")
def player_eq_set(req: _PlayerEqRequest) -> dict:
    """Persist new band amplitudes. Only pushes them to the audio
    engine when the EQ is enabled — if disabled, the sliders can
    still move (user previewing a curve) but playback stays flat
    until they toggle on.

    Save-then-apply order: if the engine ever throws, persisted
    state still matches what the UI shows. The reverse ordering
    could leave a crash-time mismatch between audible filter and
    the stored setting on next launch.
    """
    _require_local_access()
    player = _native_player()
    settings.eq_bands = list(req.bands)
    settings.eq_preamp = req.preamp
    save_settings(settings)
    if settings.eq_enabled:
        player.apply_equalizer(req.bands, preamp=req.preamp)
    return {
        "ok": True,
        "enabled": settings.eq_enabled,
        "bands": settings.eq_bands,
        "preamp": settings.eq_preamp,
    }


@app.post("/api/player/eq/preset")
def player_eq_preset(req: _PlayerEqPresetRequest) -> dict:
    """Apply a named preset. Returns the resolved bands so the
    frontend's sliders can snap to the preset curve. Persists the
    bands so a relaunch keeps the same sound. Only pushes to the
    engine when the EQ is enabled.

    `apply_equalizer_preset` has the side effect of pushing the
    curve into the engine immediately. Sequence here:
      1. Resolve + apply preset (engine now has the curve applied).
      2. Mirror the bands into `settings` and persist.
      3. If EQ is disabled, call apply_equalizer([]) to null out
         the filter so playback is flat even though we saved the
         preset bands.
    Persist BEFORE the conditional override so a crash between the
    save and the null-out can't leave an audible filter with
    disabled settings on next launch.
    """
    _require_local_access()
    player = _native_player()
    bands = player.apply_equalizer_preset(req.preset)
    settings.eq_bands = bands
    settings.eq_preamp = None
    save_settings(settings)
    if not settings.eq_enabled:
        player.apply_equalizer([])
    return {"ok": True, "enabled": settings.eq_enabled, "bands": bands}


@app.post("/api/player/eq/enabled")
def player_eq_enabled(req: _PlayerEqEnabledRequest) -> dict:
    """Master EQ on/off switch. Turning off bypasses the filter
    entirely; turning back on re-applies the stored bands so the
    user's curve survives the off → on → off cycle."""
    _require_local_access()
    player = _native_player()
    settings.eq_enabled = bool(req.enabled)
    if settings.eq_enabled and settings.eq_bands:
        player.apply_equalizer(settings.eq_bands, preamp=settings.eq_preamp)
    else:
        player.apply_equalizer([])
    save_settings(settings)
    return {"ok": True, "enabled": settings.eq_enabled}


@app.get("/api/player/output-devices")
def player_output_devices() -> dict:
    _require_local_access()
    devices = _native_player().list_output_devices()
    return {
        "devices": devices,
        "current": settings.audio_output_device,
    }


@app.post("/api/player/output-device")
def player_set_output_device(req: _PlayerOutputDeviceRequest) -> dict:
    _require_local_access()
    _native_player().set_output_device(req.device_id)
    settings.audio_output_device = req.device_id
    save_settings(settings)
    return {"ok": True, "device_id": settings.audio_output_device}


# ---------------------------------------------------------------------------
# AirPlay integration
#
# AirPlay is an optional second output. When connected, the PCM the
# player decodes is tee'd into a FLAC encoder and pushed to a paired
# receiver via pyatv. Discovery, pair, connect, and disconnect each
# have their own endpoint below so the frontend can walk the user
# through the flow. None of this is load-bearing on regular local
# playback; when AirPlay is off, the tap is a no-op.
# ---------------------------------------------------------------------------


class _AirPlayDeviceRequest(BaseModel):
    device_id: str


class _AirPlayPinRequest(BaseModel):
    pin: str


def _airplay_manager():
    """Lazy import so a broken pyatv install doesn't crash the app."""
    from app.audio.airplay import AirPlayManager  # noqa: WPS433

    return AirPlayManager.instance()


@app.get("/api/cast/devices")
def cast_devices() -> dict:
    """Snapshot of Chromecast devices currently visible on the LAN.

    Discovery runs continuously in the background (started by the
    lifespan hook above), so this endpoint just reads the current
    cache and translates it for the frontend. It does not block on
    a fresh mDNS scan — devices come and go from the cache as
    pychromecast's CastBrowser callbacks fire.
    """
    _require_local_access()
    from app.audio.cast import cast_manager  # noqa: WPS433 — lazy

    devices = cast_manager.list_devices()
    return {
        "status": cast_manager.status(),
        "devices": [
            {
                "id": d.id,
                "friendly_name": d.friendly_name,
                "model_name": d.model_name,
                "manufacturer": d.manufacturer,
                "cast_type": d.cast_type,
            }
            for d in devices
        ],
    }


class _CastConnectRequest(BaseModel):
    device_id: str


@app.post("/api/cast/connect")
def cast_connect(req: _CastConnectRequest) -> dict:
    """Open a Cast session against the given device. Tears down any
    existing session first. Blocks for the duration of the Cast
    handshake — typically under a second on the LAN, capped at 10s
    by the manager. Returns the connected device summary on
    success; 404 if the device id isn't currently in discovery,
    502 for handshake failure.
    """
    _require_local_access()
    from app.audio.cast import cast_manager  # noqa: WPS433

    try:
        device = cast_manager.connect(req.device_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "ok": True,
        "device": {
            "id": device.id,
            "friendly_name": device.friendly_name,
            "model_name": device.model_name,
            "cast_type": device.cast_type,
        },
    }


@app.post("/api/cast/disconnect")
def cast_disconnect() -> dict:
    """Tear down the active Cast session, returning audio to the
    local output. No-op if nothing is connected; idempotent."""
    _require_local_access()
    from app.audio.cast import cast_manager  # noqa: WPS433

    cast_manager.disconnect()
    return {"ok": True}


@app.get("/api/tidal-connect/devices")
def tidal_connect_devices() -> dict:
    """Snapshot of Tidal Connect-capable devices on the LAN.

    SSDP-scans for OpenHome MediaRenderer devices each call. We
    don't keep a continuous browser running like Cast does because
    SSDP is more network-noisy than mDNS, and Tidal Connect
    devices are a smaller set the user typically already knows
    about — on-demand refresh is enough.
    """
    _require_local_access()
    from app.audio.tidal_connect import get_manager  # noqa: WPS433

    mgr = get_manager()
    if not mgr.is_available():
        return {
            "status": {"available": False, "device_count": 0},
            "devices": [],
        }
    devices = mgr.refresh(timeout=5.0)
    return {
        "status": mgr.status(),
        "devices": [
            {
                "id": d.id,
                "friendly_name": d.friendly_name,
                "manufacturer": d.manufacturer,
                "model": d.model,
                "is_openhome": d.is_openhome,
                "has_credentials_service": d.has_credentials_service,
            }
            for d in devices
        ],
    }


class _TidalConnectConnectRequest(BaseModel):
    device_id: str


@app.post("/api/tidal-connect/connect")
def tidal_connect_connect(req: _TidalConnectConnectRequest) -> dict:
    """Open a Tidal Connect session against the given device.

    Fetches the OpenHome description, builds the service controllers,
    and clears the device's queue so we start from a known state.
    Audio handoff (loading a Tidal track on the device) is a
    separate `load_track` flow — connect just opens the control
    channel.

    Status codes:
      200  session opened
      404  device id isn't in the discovery cache
      502  descriptor fetch failed, or the device doesn't expose
           the OpenHome services we need
    """
    _require_local_access()
    from app.audio.tidal_connect import get_manager  # noqa: WPS433

    mgr = get_manager()
    try:
        device = mgr.connect(req.device_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "ok": True,
        "device": {
            "id": device.id,
            "friendly_name": device.friendly_name,
            "manufacturer": device.manufacturer,
            "model": device.model,
        },
    }


@app.post("/api/tidal-connect/disconnect")
def tidal_connect_disconnect() -> dict:
    """Tear down the active Tidal Connect session. Idempotent."""
    _require_local_access()
    from app.audio.tidal_connect import get_manager  # noqa: WPS433

    get_manager().disconnect()
    return {"ok": True}


@app.get("/api/upnp/devices")
def upnp_devices() -> dict:
    """SSDP-scan the LAN for UPnP MediaRenderers. Used by the
    forthcoming Settings UPnP section to populate the device
    picker. Day 1 of UPnP work — no connect / play yet, just
    confirming that at least one device on the user's network
    responds to discovery."""
    _require_local_access()
    from app.audio.upnp import get_manager  # noqa: WPS433 — lazy

    mgr = get_manager()
    if not mgr.is_available():
        return {
            "available": False,
            "reason": "async-upnp-client not installed",
            "devices": [],
        }
    devices = mgr.discover(timeout=5.0)
    return {
        "available": True,
        "devices": [
            {
                "id": d.id,
                "name": d.name,
                "manufacturer": d.manufacturer,
                "model": d.model,
                "location": d.location,
                "service_types": list(d.service_types),
            }
            for d in devices
        ],
    }


@app.get("/api/airplay/devices")
def airplay_devices() -> dict:
    _require_local_access()
    from app.audio.airplay import AirPlayManager

    if not AirPlayManager.is_available():
        return {
            "available": False,
            "reason": AirPlayManager.import_error(),
            "devices": [],
            "connected_id": None,
        }
    mgr = _airplay_manager()
    devices = mgr.discover()
    return {
        "available": True,
        "devices": [
            {
                "id": d.id,
                "name": d.name,
                "address": d.address,
                "has_raop": d.has_raop,
                "paired": d.paired,
            }
            for d in devices
        ],
        "connected_id": mgr.current_device_id(),
    }


@app.post("/api/airplay/pair/start")
def airplay_pair_start(req: _AirPlayDeviceRequest) -> dict:
    _require_local_access()
    try:
        _airplay_manager().begin_pairing(req.device_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True}


@app.post("/api/airplay/pair/pin")
def airplay_pair_pin(req: _AirPlayPinRequest) -> dict:
    _require_local_access()
    try:
        _airplay_manager().submit_pin(req.pin)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.post("/api/airplay/pair/cancel")
def airplay_pair_cancel() -> dict:
    _require_local_access()
    _airplay_manager().cancel_pairing()
    return {"ok": True}


@app.post("/api/airplay/connect")
def airplay_connect(req: _AirPlayDeviceRequest) -> dict:
    _require_local_access()
    # The AirPlay encoder needs to match the player's current output
    # format. We read it off the active player; if nothing is loaded
    # yet, fall back to 44.1 kHz stereo int16 (CD-quality), the
    # lowest-common-denominator that every receiver accepts. The
    # player will reconnect-at-correct-format on the next load() if
    # the actual stream is hi-res.
    player = _native_player()
    sample_rate = getattr(player, "_stream_sample_rate", None) or 44100
    channels = getattr(player, "_stream_channels", None) or 2
    dtype = getattr(player, "_stream_sd_dtype", None) or "int16"
    try:
        _airplay_manager().connect(
            req.device_id,
            sample_rate=sample_rate,
            channels=channels,
            dtype=dtype,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "connected_id": req.device_id}


@app.post("/api/airplay/disconnect")
def airplay_disconnect() -> dict:
    _require_local_access()
    _airplay_manager().disconnect()
    return {"ok": True}


# The actual audio stream endpoint pyatv reaches is served by a
# dedicated HTTP listener bound to 0.0.0.0 on an ephemeral port,
# owned by AirPlayManager. See app/audio/airplay.py and
# _StreamHTTPServer for why FastAPI doesn't host the stream
# directly.


# ---------------------------------------------------------------------------
# Global media-key event bus
#
# Global hotkeys (play-pause / next / previous) fire on a pynput thread
# in the backend. We publish each to this bus; the frontend subscribes
# via SSE and runs the corresponding action through its player hook —
# that way queue/shuffle/repeat decisions stay in the frontend instead
# of being re-implemented server-side.
# ---------------------------------------------------------------------------


class _HotkeyBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[asyncio.Queue] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._loop = loop

    def publish(self, action: str) -> None:
        """Safe to call from any thread (including pynput's listener
        thread). Schedules the payload put on the FastAPI event loop."""
        with self._lock:
            loop = self._loop
            subs = list(self._subscribers)
        if loop is None:
            return
        for q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, action)
            except Exception:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=32)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass


_hotkey_bus = _HotkeyBus()


def _emit_hotkey(action: str) -> dict:
    _hotkey_bus.publish(action)
    return {"ok": True, "action": action}


@app.post("/api/hotkey/play_pause")
def hotkey_play_pause() -> dict:
    _require_local_access()
    return _emit_hotkey("play_pause")


@app.post("/api/hotkey/next")
def hotkey_next() -> dict:
    _require_local_access()
    return _emit_hotkey("next")


@app.post("/api/hotkey/previous")
def hotkey_previous() -> dict:
    _require_local_access()
    return _emit_hotkey("previous")


@app.get("/api/hotkey/events")
async def hotkey_events(request: Request):
    """SSE stream of hotkey events. The frontend's usePlayer hook
    subscribes and maps each action onto its own toggle/next/prev
    so queue state + advance logic stay in one place."""
    _require_local_access()
    _hotkey_bus.bind_loop(asyncio.get_running_loop())
    q = _hotkey_bus.subscribe()

    async def _gen():
        try:
            # Initial ping so the frontend knows the subscription is up.
            yield "data: {\"action\": \"_ready\"}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    action = await asyncio.wait_for(q.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    # Keepalive comment — prevents proxies from closing
                    # a silent connection.
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps({'action': action})}\n\n"
        finally:
            _hotkey_bus.unsubscribe(q)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/player/events")
async def player_events(request: Request):
    """SSE stream of player snapshots.

    State-change notifications are pushed immediately via the
    player's subscribe() listener; between those we poll at 4Hz so
    the frontend gets smooth position updates during playback.
    When paused/idle we drop to a 1Hz heartbeat to keep the
    connection alive without wasting cycles.
    """
    _require_local_access()
    player = _native_player()
    queue: asyncio.Queue[Optional[dict]] = asyncio.Queue(maxsize=32)
    loop = asyncio.get_running_loop()

    def _on_snapshot(snap) -> None:
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
    pref = (settings.explicit_content_preference or "explicit").lower()
    tracks = filter_explicit_dupes(results.get("tracks", []), pref, kind="track")
    albums = filter_explicit_dupes(results.get("albums", []), pref, kind="album")
    artists = _rerank_artists(q, list(results.get("artists", [])))
    return {
        "tracks": [track_to_dict(t) for t in tracks],
        "albums": [album_to_dict(a) for a in albums],
        "artists": [artist_to_dict(a) for a in artists],
        "playlists": [playlist_to_dict(p) for p in results.get("playlists", [])],
    }


def _rerank_artists(query: str, artists: list) -> list:
    """Match Tidal's own search feel: surface the exact-name match
    first, then splice in its relational "Fans also like" neighbours,
    then keep the remaining fuzzy matches in whatever order Tidal
    returned.

    Runs best-effort. If the similarity lookup fails (network blip,
    Tidal rate-limit, unknown-artist edge case), we skip the injection
    and return the original list intact."""
    if not artists:
        return artists

    norm_query = _norm_title(query)
    if not norm_query:
        return artists

    # Find the first result whose normalized name equals the query.
    exact_idx: Optional[int] = None
    for i, a in enumerate(artists):
        if _norm_title(getattr(a, "name", "")) == norm_query:
            exact_idx = i
            break
    if exact_idx is None:
        # No confident top hit — don't fan out, just hand Tidal's
        # ordering through. Avoids bad injections for misspellings.
        return artists

    top_hit = artists[exact_idx]
    rest = [a for j, a in enumerate(artists) if j != exact_idx]

    # Fetch related / "fans also like" for the top hit. Capped so we
    # don't drown out the raw fuzzy matches below.
    similar: list = []
    try:
        raw = top_hit.get_similar() or []
        seen = {str(getattr(top_hit, "id", ""))}
        for a in raw:
            aid = str(getattr(a, "id", "") or "")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            similar.append(a)
            if len(similar) >= 6:
                break
    except Exception:
        similar = []

    # De-dupe similars against the rest of the raw search results so
    # the same artist doesn't render twice when Tidal already surfaced
    # them as a fuzzy match.
    rest_ids = {str(getattr(a, "id", "") or "") for a in rest}
    similar = [a for a in similar if str(getattr(a, "id", "") or "") not in rest_ids]

    return [top_hit, *similar, *rest]


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


_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}


def _scan_local_videos(root: Path) -> list[dict]:
    """Enumerate video files under `root`. Metadata comes from the
    filename pattern `<Artist> - <Title>.mp4` that video_downloader
    writes; no MP4 tag reading since the remux doesn't author tags.
    """
    import os as _os

    if not root.is_dir():
        return []
    out: list[dict] = []
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
                        if ext not in _VIDEO_EXTENSIONS:
                            continue
                        st = entry.stat()
                        stem = _os.path.splitext(entry.name)[0]
                        # "<Artist> - <Title>" is how video_downloader
                        # names files. Split on the first " - " so
                        # track titles containing dashes still work.
                        if " - " in stem:
                            artist, title = stem.split(" - ", 1)
                        else:
                            artist, title = "", stem
                        path = Path(entry.path)
                        try:
                            rel = str(path.relative_to(root))
                        except ValueError:
                            rel = entry.name
                        out.append({
                            "path": str(path),
                            "relative_path": rel,
                            "title": title.strip(),
                            "artist": artist.strip(),
                            "size_bytes": st.st_size,
                            "ext": ext,
                            "mtime": st.st_mtime,
                        })
                    except OSError:
                        continue
        except OSError:
            continue
    out.sort(key=lambda v: (v["artist"].lower(), v["title"].lower()))
    return out


@app.get("/api/library/local")
def library_local() -> dict:
    """List the user's downloaded audio + video files. The frontend's
    Local Library page groups audio by artist/album and renders videos
    in a dedicated section so the user can browse what's actually on
    disk (as opposed to what they've favorited in Tidal).

    Audio tags come from mutagen, cached by (path, mtime, size) so a
    second load is effectively free. Videos come from the
    _scan_local_videos helper which parses the "<Artist> - <Title>"
    filename the downloader writes.
    """
    _require_local_access()
    import os as _os

    root = Path(settings.output_dir).expanduser()
    videos_root = Path(settings.videos_dir).expanduser()
    videos = _scan_local_videos(videos_root)
    files: list[dict] = []
    if not root.is_dir():
        return {
            "output_dir": str(root),
            "videos_dir": str(videos_root),
            "files": [],
            "videos": videos,
        }
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
                            "album_artist": tags.get("album_artist"),
                            "track_num": tags.get("track_num") or 0,
                            "tidal_id": tags.get("tidal_id"),
                            "duration": tags.get("duration") or 0,
                            "size_bytes": st.st_size,
                            "ext": ext,
                            # mtime lets the frontend offer a
                            # "Recent" sort (newest → oldest) without
                            # needing a second round-trip. Seconds
                            # since epoch; JSON-clean.
                            "mtime": st.st_mtime,
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
    return {
        "output_dir": str(root),
        "videos_dir": str(videos_root),
        "files": files,
        "videos": videos,
    }


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


# Strips the parenthetical / bracketed variant tags Tidal hangs off
# album names so that "Hurry Up Tomorrow", "Hurry Up Tomorrow
# (Deluxe)", "Hurry Up Tomorrow [Explicit]", and "Hurry Up Tomorrow
# - Deluxe Edition" all reduce to the same key. Used by the More By
# row to keep the same album from filling six slots when Tidal
# carries it under multiple variant IDs (explicit / clean / region /
# deluxe / standard / re-release). Conservative on purpose: we only
# strip text inside () or [] and a trailing " - <something> Edition"
# tail, so an album whose actual title contains a parenthetical
# (rare but it happens — "Some Album (2024 Remaster)" is genuinely
# distinct from "Some Album") still reduces to a different key from
# the un-remastered version once we carry the variant tag through.
_VARIANT_SUFFIX_RE = re.compile(
    r"\s*[\(\[][^)\]]*[\)\]]\s*$"  # trailing parenthetical / bracketed tag
    r"|\s*-\s*[A-Za-z0-9 ]*?(edition|version|remaster|mix)\s*$",
    re.IGNORECASE,
)


def _normalize_album_title(title: str) -> str:
    """Reduce an album title to a comparison key. Lowercased, with
    trailing variant tags stripped, whitespace collapsed. Returns
    empty string if the title is empty / falsy."""
    if not title:
        return ""
    s = title.strip()
    # Strip variant suffixes one at a time in case Tidal stacks them
    # (e.g. "Album (Deluxe) [Explicit]"). Bounded loop so a malformed
    # title can't cause infinite work.
    for _ in range(4):
        new = _VARIANT_SUFFIX_RE.sub("", s).strip()
        if new == s:
            break
        s = new
    return " ".join(s.lower().split())


@app.get("/api/album/{album_id}")
def album_detail(album_id: int) -> dict:
    _require_auth()
    try:
        album = tidal.session.album(album_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    primary = _first(lambda: album.artist) or (
        album.artists[0] if getattr(album, "artists", None) else None
    )

    # Everything below is a separate Tidal round-trip; running them
    # in parallel turns the album page into a one-slow-call load
    # instead of six-sequential-calls. Each helper is wrapped so a
    # failure just yields the empty default without blowing up the
    # whole response — similar / review / more-by-artist /
    # related-artists all 404 on non-editorial content, and we'd
    # rather render a page with holes than return a 500.
    def _safe(fn, default):
        try:
            return fn()
        except Exception:
            return default

    def _more_by() -> list[dict]:
        if primary is None:
            return []
        full = _safe(lambda: list(tidal.get_artist_albums(primary)) or [], [])
        eps = _safe(lambda: list(primary.get_ep_singles(limit=20)) or [], [])
        out: list[dict] = []
        current_id = str(album.id)
        # Tidal's catalog regularly carries the same release under
        # multiple album IDs — explicit / clean variants, region-locked
        # editions, deluxe / standard pairs, label re-issues. An ID-
        # only dedupe lets all of those slip through and the user sees
        # what looks like the same album twice. Dedupe by a normalized
        # title key as well, so a title that already appeared (or that
        # matches the album we're currently viewing) gets dropped.
        # `_normalize_album_title` lowercases, strips parenthetical
        # variant tags, and collapses whitespace.
        current_title_key = _normalize_album_title(getattr(album, "name", "") or "")
        seen_ids: set[str] = set()
        seen_titles: set[str] = {current_title_key} if current_title_key else set()
        for a in full + eps:
            aid = str(getattr(a, "id", "") or "")
            if not aid or aid == current_id or aid in seen_ids:
                continue
            title_key = _normalize_album_title(getattr(a, "name", "") or "")
            if title_key and title_key in seen_titles:
                continue
            seen_ids.add(aid)
            if title_key:
                seen_titles.add(title_key)
            out.append(album_to_dict(a))
            if len(out) >= 12:
                break
        return out

    def _related_artists() -> list[dict]:
        if primary is None:
            return []
        return _safe(
            lambda: [artist_to_dict(x) for x in primary.get_similar()][:12],
            [],
        )

    # 2 workers on album page — slower than 5 but the difference is
    # barely perceptible (the slowest single call is the bottleneck)
    # and we avoid firing five concurrent Tidal requests per album
    # click.
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_tracks = pool.submit(
            _safe,
            lambda: [track_to_dict(t) for t in tidal.get_album_tracks(album)],
            [],
        )
        f_similar = pool.submit(
            _safe,
            lambda: [album_to_dict(a) for a in album.similar()][:12],
            [],
        )
        f_review = pool.submit(_safe, lambda: album.review() or None, None)
        f_more_by = pool.submit(_more_by)
        f_related = pool.submit(_related_artists)

    return {
        **album_to_dict(album),
        "tracks": f_tracks.result(),
        "similar": f_similar.result(),
        "review": f_review.result(),
        "more_by_artist": f_more_by.result(),
        "related_artists": f_related.result(),
    }


_artist_detail_cache: dict[str, tuple[float, dict]] = {}
_artist_detail_cache_lock = threading.Lock()
_ARTIST_DETAIL_CACHE_TTL_SEC = 300.0


@app.get("/api/artist/{artist_id}")
def artist_detail(artist_id: int) -> dict:
    _require_auth()
    cache_key = str(artist_id)
    now = time.monotonic()
    with _artist_detail_cache_lock:
        cached = _artist_detail_cache.get(cache_key)
    if cached and (now - cached[0]) < _ARTIST_DETAIL_CACHE_TTL_SEC:
        return cached[1]
    try:
        artist = tidal.session.artist(artist_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    # Ten Tidal calls feed the artist page: bio, similar artists,
    # top tracks, albums, EPs/singles, "other" (compilations only,
    # per tidalapi's COMPILATIONS filter), the Tidal-curated artist
    # page (for the real "Appears on" entries), the Artist Radio
    # mix id, videos, and credits. Each hits tidal.com over HTTPS
    # with a typical round-trip of 150-400ms.
    def _safe(fn, default):
        try:
            return fn()
        except Exception:
            return default

    # 5 workers — the page load is dominated by perceived wait when
    # the user clicks "Artist" from an album, and the previous 3
    # workers stretched it to ~1.5s. Five concurrent fetches still
    # don't look like a scrape to Tidal's abuse layer (a real client
    # opening an artist page does similar volume), and the wall-clock
    # drops to roughly two waves of the slowest single call.
    with ThreadPoolExecutor(max_workers=5) as pool:
        # Bio used to be jittered to spread the burst, but that was
        # premature: bio is a small text fetch, not a stream-manifest
        # request, and the 50-200ms sleep added directly to the
        # critical path with no observable behavior benefit.
        f_bio = pool.submit(_safe, artist.get_bio, None)
        f_similar = pool.submit(
            _safe,
            lambda: [artist_to_dict(a) for a in artist.get_similar()][:12],
            [],
        )
        f_top_tracks = pool.submit(
            _safe,
            lambda: [
                track_to_dict(t) for t in tidal.get_artist_top_tracks(artist)
            ],
            [],
        )
        f_albums_raw = pool.submit(
            _safe, lambda: list(tidal.get_artist_albums(artist)) or [], []
        )
        f_eps = pool.submit(
            _safe, lambda: list(artist.get_ep_singles(limit=40)) or [], []
        )
        f_compilations = pool.submit(
            _safe, lambda: list(artist.get_other(limit=40)) or [], []
        )
        f_page = pool.submit(_safe, artist.page, None)
        f_mix_id = pool.submit(
            _safe, lambda: str(artist.get_radio_mix().id), None
        )
        # Credits and videos used to be separate endpoints the
        # frontend fetched in parallel after the main artist
        # payload arrived. Folding them into the same response
        # saves two HTTP round-trips on every artist page load.
        f_videos = pool.submit(
            _safe,
            lambda: [video_to_dict(v) for v in artist.get_videos(limit=50) or []],
            [],
        )
        f_credits = pool.submit(_safe, lambda: _artist_credits_list(artist_id, 20), [])

    bio = f_bio.result()
    similar = f_similar.result()
    top_tracks = f_top_tracks.result()
    raw_albums = f_albums_raw.result()
    raw_eps = f_eps.result()
    raw_appears = list(f_compilations.result())
    artist_page = f_page.result()
    artist_mix_id = f_mix_id.result()
    videos = f_videos.result()
    credits = f_credits.result()

    # Merge "Appears on" rows scraped from the curated page into
    # raw_appears. tidalapi's `get_other()` uses filter=COMPILATIONS
    # which only returns multi-artist compilations and misses the
    # common "appears on" case of a featured or guest performance
    # on another artist's album. Tidal's own artist page carries a
    # dedicated "Appears on" module with those entries.
    if artist_page is not None:
        try:
            from tidalapi.album import Album as _TidalAlbum

            for cat in getattr(artist_page, "categories", []) or []:
                cat_title = (getattr(cat, "title", "") or "").strip().lower()
                if "appear" not in cat_title and "featured" not in cat_title:
                    continue
                for item in getattr(cat, "items", []) or []:
                    if isinstance(item, _TidalAlbum):
                        raw_appears.append(item)
        except Exception:
            pass

    # Dedupe across all three discography sections. Sources of dupes:
    #  1. tidalapi's `get_albums()` can page internally and surface the
    #     same record twice.
    #  2. Tidal sometimes tags the same release as both an album and
    #     an EP, so `get_albums()` and `get_ep_singles()` overlap.
    #  3. An artist's own album can bleed into `get_other()` (appears-
    #     on) when the featured-artist metadata is ambiguous.
    #  4. Tidal's catalog regularly carries the SAME logical release
    #     under multiple distinct ids — separate regional listings,
    #     re-uploads, distributor changes. ID-based dedup misses these.
    #
    # Key on (normalized_title, version, primary_artist_id, explicit)
    # so different editions (deluxe / anniversary), different
    # artists with same title, and explicit-vs-clean variants all stay
    # separate — but duplicate uploads of the same release collapse.
    # Precedence: albums win over EPs, EPs win over appears-on. The
    # first list a key appears in is where it stays.

    def _album_key(a) -> Optional[tuple]:
        name = (getattr(a, "name", "") or "").strip().lower()
        if not name:
            return None
        version = (getattr(a, "version", "") or "").strip().lower()
        try:
            artist_id = str(getattr(a.artist, "id", "") or "")
        except Exception:
            artist_id = ""
        explicit = bool(getattr(a, "explicit", False))
        return (name, version, artist_id, explicit)

    seen_keys: set[tuple] = set()
    seen_ids: set[str] = set()

    def _dedupe(items: list) -> list:
        out = []
        for a in items:
            aid = str(getattr(a, "id", "") or "")
            if aid and aid in seen_ids:
                continue
            key = _album_key(a)
            if key is not None:
                if key in seen_keys:
                    continue
                seen_keys.add(key)
            if aid:
                seen_ids.add(aid)
            out.append(a)
        return out

    albums_objs = _dedupe(raw_albums)
    ep_singles_objs = _dedupe(raw_eps)
    appears_on_objs = _dedupe(raw_appears)

    # Collapse explicit / clean editions of the same album per the
    # user's content-filter setting. Keeps the discography looking like
    # Tidal's own client where duplicates rarely sit side by side.
    pref = (settings.explicit_content_preference or "explicit").lower()
    albums_objs = filter_explicit_dupes(albums_objs, pref, kind="album")
    ep_singles_objs = filter_explicit_dupes(ep_singles_objs, pref, kind="album")
    appears_on_objs = filter_explicit_dupes(appears_on_objs, pref, kind="album")

    result = {
        **artist_to_dict(artist),
        "top_tracks": top_tracks,
        "albums": [album_to_dict(a) for a in albums_objs],
        "ep_singles": [album_to_dict(a) for a in ep_singles_objs],
        "appears_on": [album_to_dict(a) for a in appears_on_objs],
        "bio": bio,
        "similar": similar,
        "artist_mix_id": artist_mix_id,
        "videos": videos,
        "credits": credits,
        # Stable share URL for the copy/open-in-Tidal actions in the UI.
        "share_url": getattr(artist, "share_url", None)
        or f"https://tidal.com/browse/artist/{artist.id}",
    }
    with _artist_detail_cache_lock:
        _artist_detail_cache[cache_key] = (time.monotonic(), result)
    return result


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
    """Return an HLS manifest URL for a video, routed through our
    server-side proxy.

    Browsers (Chrome, Firefox, WebView2 on Windows) enforce CORS on
    hls.js's XHR fetches, and Tidal's CDN doesn't send
    Access-Control-Allow-Origin. So we hand the frontend a loopback
    URL to our /api/video-proxy endpoint — which fetches from Tidal
    server-side and streams bytes through from the same origin as
    the page. WKWebView (packaged macOS .app) decodes HLS natively
    without XHR and would work with the direct URL too, but sending
    it through the proxy costs one extra localhost hop per segment —
    negligible, and keeps the frontend code uniform.

    When `quality` is omitted we use the session default (what
    tidalapi's `video.get_url()` returns). When passed, we hit
    `/videos/{id}/urlpostpaywall` directly so the quality-picker
    can swap streams without mutating session state.
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
    return {"url": f"/api/video-proxy?u={quote(url, safe='')}"}


def _is_tidal_video_host(netloc: str) -> bool:
    """Tidal serves HLS from multiple CDN hostnames; match on a
    suffix so `im-cf.manifest.tidal.com`, `vmz-ad-cf.video.tidal.com`,
    etc. all pass without needing an exhaustive allowlist. Guards
    /api/video-proxy against being used as an open proxy for
    arbitrary URLs.
    """
    n = netloc.lower().split(":", 1)[0]
    return n.endswith(".tidal.com") or n == "tidal.com"


def _rewrite_m3u8(text: str, base_url: str) -> str:
    """Rewrite every URI in an HLS manifest to loop back through
    our /api/video-proxy endpoint.

    Handles:
      - Segment lines (non-#, resolved against the manifest URL).
      - Variant playlists (same shape; hls.js will re-enter this
        endpoint for each one).
      - URI attribute inside #EXT-X-KEY / #EXT-X-MAP / etc.

    URIs that don't resolve to a Tidal host are passed through
    untouched — the browser will fail on those with a CORS error
    that we'd see in the console.
    """
    import re

    def rewrite_uri(uri: str) -> str:
        abs_url = urljoin(base_url, uri)
        if not _is_tidal_video_host(urlparse(abs_url).netloc):
            return uri
        return f"/api/video-proxy?u={quote(abs_url, safe='')}"

    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            # Tag line: rewrite any embedded URI="..." attribute.
            if 'URI="' in s:
                line = re.sub(
                    r'URI="([^"]+)"',
                    lambda m: f'URI="{rewrite_uri(m.group(1))}"',
                    line,
                )
            out.append(line)
            continue
        out.append(rewrite_uri(s))
    return "\n".join(out)


@app.get("/api/video-proxy")
def video_proxy(u: str):
    """Server-side fetch + stream for Tidal HLS manifests + media
    segments. Called by hls.js from the browser via same-origin
    URLs rewritten into manifests by `_rewrite_m3u8`.

    Manifest responses (content-type includes `mpegurl`) are
    parsed and URL-rewritten recursively — a master playlist's
    variant URLs point back through the proxy, so when hls.js
    fetches them it stays in-origin.

    Segment responses (`.ts` / `.m4s` / `.mp4`) are streamed through
    unmodified with their original content-type.
    """
    _require_auth()
    try:
        parsed = urlparse(u)
    except ValueError:
        raise HTTPException(status_code=400, detail="Malformed URL")
    if not _is_tidal_video_host(parsed.netloc):
        raise HTTPException(
            status_code=400, detail="Proxy target must be a Tidal host"
        )
    # Decide upfront whether this is a manifest or a media segment.
    # Manifests need the full body in memory so we can parse + rewrite
    # URLs; segments are big and stream through chunk-by-chunk. The
    # signal lives in the URL (`.m3u8` extension) since the upstream
    # Content-Type isn't reliable until after we've made the request.
    likely_manifest = ".m3u8" in parsed.path.lower()
    try:
        # Manifests open WITHOUT stream=True so the full body lands in
        # `r.text` / `r.content` synchronously. curl-cffi's stream=True
        # path was returning empty bodies for some Tidal HLS manifests
        # (200 OK, correct Content-Type, len=0) — verified via the
        # video-proxy diagnostic log. The audio downloader gets away
        # with stream=True because it reads via iter_content(); the
        # text path on the same Response object is the broken one.
        r = SESSION.get(u, stream=not likely_manifest, timeout=30)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if r.status_code >= 400:
        r.close()
        raise HTTPException(
            status_code=r.status_code,
            detail=f"Upstream returned {r.status_code}",
        )

    content_type = (r.headers.get("Content-Type") or "").lower()
    is_manifest = (
        "mpegurl" in content_type
        or ".m3u8" in parsed.path.lower()
    )
    if is_manifest:
        try:
            text = r.text
            content_type_header = r.headers.get("Content-Type")
            # Use the FINAL URL after any redirects, not the URL the
            # caller asked for. Tidal sometimes serves manifests via
            # a redirect chain (signed CloudFront → CDN edge), and
            # relative URIs inside the manifest resolve against the
            # last redirect, not the first request. Falling back to
            # `u` when the response object doesn't expose `.url`
            # (older curl-cffi versions) keeps the existing behavior.
            base_for_rewrite = getattr(r, "url", None) or u
        finally:
            r.close()
        # Permanent diagnostic — same shape as `[audio] stream open`
        # in the audio engine. One print per manifest fetch (~1 per
        # variant per video; segments don't log) carrying the URL
        # prefix, upstream Content-Type, body length, and first 200
        # chars (line-escaped). Cheap to log; gives a future
        # "videos don't play" report enough signal to identify
        # whether Tidal has changed the manifest format, the proxy
        # is fetching empty bodies again, or something downstream
        # in hls.js is the cause.
        head = text[:200].replace("\n", "\\n").replace("\r", "\\r")
        print(
            f"[video-proxy] manifest u={u[:100]!r} "
            f"content_type={content_type_header!r} "
            f"len={len(text)} head={head!r}",
            file=sys.stderr,
            flush=True,
        )
        rewritten = _rewrite_m3u8(text, base_for_rewrite)
        return Response(
            rewritten, media_type="application/vnd.apple.mpegurl"
        )

    def _chunks():
        try:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            r.close()

    return StreamingResponse(
        _chunks(), media_type=content_type or "application/octet-stream"
    )


@app.get("/api/track/{track_id}")
def get_track(track_id: int) -> dict:
    """Return a single track by id.

    Used by the frontend to rehydrate the now-playing bar after a
    page reload. The SSE snapshot only carries the track id, so a
    fresh load with no prior queue state needs a way to fetch the
    full metadata the bar renders from. Uses the same track dict
    shape as search and library results so the frontend can reuse
    its existing `Track` type without mapping.
    """
    _require_auth()
    try:
        track = tidal.session.track(track_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return track_to_dict(track)


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


def _artist_credits_list(artist_id: int, limit: int) -> list[dict]:
    """Shared helper that powers both the /api/artist/{id}/credits
    endpoint and the `credits` field in the main artist response.

    Tidal's `/artists/{id}/credits` endpoint is undocumented. If it
    404s or the response is unexpected, return an empty list and let
    the frontend hide the section. Never raises.
    """
    try:
        resp = tidal.session.request.request(
            "GET", f"artists/{artist_id}/credits", params={"limit": limit, "offset": 0}
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

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


@app.get("/api/artist/{artist_id}/credits")
def artist_credits(artist_id: int, limit: int = 20) -> list[dict]:
    """List tracks where this artist is credited in any role — the
    equivalent of Tidal's artist-page "Credits" section (writer,
    producer, engineer, featured, etc.). Returns serialized Track rows
    with their role annotated so the frontend can group by role.

    The main /api/artist/{id} response already includes a `credits`
    field with this same data, so the frontend no longer hits this
    route on the normal artist page load. Kept around for any code
    path that wants a larger `limit` than the bundled default (20)
    without refetching the whole artist payload.
    """
    _require_auth()
    return _artist_credits_list(artist_id, limit)


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

    # Confine reveals to the configured output directories. Prevents
    # the endpoint from being abused to poke around the user's whole
    # filesystem. Both the audio output_dir and the video videos_dir
    # are allowed — the latter is often a different path (~/Movies
    # vs. ~/Music) so we have to include it or video reveals would
    # 403.
    allowed_roots: list[Path] = []
    for _d in (settings.output_dir, settings.videos_dir):
        try:
            allowed_roots.append(Path(_d).expanduser().resolve())
        except (FileNotFoundError, RuntimeError, OSError):
            continue
    if not any(_is_within(target, root) for root in allowed_roots):
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

    Explicit request wins (clamped to subscription). Otherwise use the
    highest tier the subscription allows — the UI forces an explicit
    pick on every download now, so this fallback only fires for
    callers that genuinely want "just give me the best" (bulk flows
    like Download-All, folder-level download actions).
    """
    if req_quality:
        return _clamp_quality_to_subscription(req_quality)
    return _clamp_quality_to_subscription("hi_res_lossless")


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
        "bitrate": "96 kbps",
        "description": "Data-saver streaming.",
    },
    {
        "value": "low_320k",
        "label": "Medium",
        "codec": "AAC",
        "bitrate": "320 kbps",
        "description": "Standard streaming.",
    },
    {
        "value": "high_lossless",
        "label": "High",
        "codec": "FLAC",
        "bitrate": "1411 kbps",
        "description": "Lossless (16-bit, 44.1 kHz).",
    },
    {
        "value": "hi_res_lossless",
        "label": "Max",
        "codec": "FLAC",
        "bitrate": "up to 9216 kbps",
        "description": "Up to 24-bit, 192 kHz.",
    },
]


@app.get("/api/subscription")
def subscription_status() -> dict:
    """The user's Tidal subscription tier in the form the UI needs to
    decide whether to enable the Download buttons.

    `tier` is one of:
      - "max"      — HiFi Plus / Max, hi-res FLAC available
      - "lossless" — HiFi, 16-bit FLAC available
      - "lossy"    — Free / ad-supported, only 96k or 320k AAC
      - "unknown"  — subscription endpoint failed; assume capable so
                     we don't lock the user out on a transient error

    `can_download` is True for "lossless" and "max" (and the
    optimistic "unknown") because Tideway is built around lossless+
    downloads. Lossy-only accounts still get a "look, but no
    download" experience with an explanatory tooltip.
    """
    _require_local_access()
    if not _is_logged_in():
        return {
            "tier": "unknown",
            "can_download": False,
            "reason": "Sign in to Tidal to check subscription status.",
        }
    max_quality = tidal.get_max_quality()
    if max_quality is None:
        return {
            "tier": "unknown",
            "can_download": True,
            "reason": None,
        }
    if max_quality == "hi_res_lossless":
        return {"tier": "max", "can_download": True, "reason": None}
    if max_quality == "high_lossless":
        return {"tier": "lossless", "can_download": True, "reason": None}
    return {
        "tier": "lossy",
        "can_download": False,
        "reason": (
            "Tideway downloads need a Tidal HiFi or HiFi Plus subscription. "
            "Your current tier streams lossy audio only."
        ),
    }


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
    videos_dir: Optional[str] = None
    filename_template: Optional[str] = None
    create_album_folders: Optional[bool] = None
    skip_existing: Optional[bool] = None
    concurrent_downloads: Optional[int] = None
    offline_mode: Optional[bool] = None
    notify_on_complete: Optional[bool] = None
    notify_on_track_change: Optional[bool] = None
    exclusive_mode: Optional[bool] = None
    force_volume: Optional[bool] = None
    continue_playing_after_queue_ends: Optional[bool] = None
    start_minimized: Optional[bool] = None
    explicit_content_preference: Optional[str] = None
    download_rate_limit_mbps: Optional[int] = None


@app.get("/api/settings")
def get_settings() -> dict:
    _require_local_access()
    return asdict(settings)


@app.put("/api/settings")
def update_settings(payload: SettingsPayload) -> dict:
    _require_local_access()
    global settings
    patch = payload.model_dump(exclude_unset=True)

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
    # videos_dir — same validation as output_dir. The directory need
    # not exist yet; if it doesn't, we create it on first download
    # rather than reject the save.
    if "videos_dir" in patch:
        raw = patch["videos_dir"]
        if not isinstance(raw, str) or not raw.strip():
            raise HTTPException(status_code=400, detail="videos_dir must be a non-empty string")
        resolved = Path(raw).expanduser()
        # Reject obviously-dangerous targets even if the path doesn't
        # yet exist — we create parents on first download.
        absolute_parent = resolved.parent.resolve()
        forbidden = {Path("/"), Path("/etc"), Path("/bin"), Path("/usr"), Path("/sbin"), Path("/var")}
        if resolved.resolve() in forbidden or absolute_parent in forbidden:
            raise HTTPException(status_code=400, detail=f"videos_dir not allowed: {resolved}")
        patch["videos_dir"] = str(resolved)
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
        prev_exclusive = bool(data.get("exclusive_mode", False))
        prev_force_volume = bool(data.get("force_volume", False))
        data.update(patch)
        new_settings = Settings(**data)
        save_settings(new_settings)
        settings = new_settings
        downloader.settings = new_settings
    downloader.gate.set_limit(new_settings.concurrent_downloads)
    if "exclusive_mode" in patch and bool(new_settings.exclusive_mode) != prev_exclusive:
        try:
            _native_player().set_exclusive_mode(new_settings.exclusive_mode)
        except Exception as exc:
            logger.warning("exclusive-mode toggle failed: %s", exc)
    if "force_volume" in patch and bool(new_settings.force_volume) != prev_force_volume:
        try:
            _native_player().set_force_volume(new_settings.force_volume)
        except Exception as exc:
            logger.warning("force-volume toggle failed: %s", exc)
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
    # Mix — tidalapi ships these under several class names
    # (Mix, MixV2, MixV2Full, …). Any class whose name starts with
    # "Mix" is a mix record from our perspective.
    name = type(item).__name__
    if name.startswith("Mix"):
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

    Tidal emits several response shapes for view-alls:
      1. Flat items with type wrappers: {"items": [{"type": "MIX", "data": {...}}, ...]}
      2. Flat items as bare objects: {"items": [{...mix fields...}, ...]}
      3. Module-nested: {"modules": [{"pagedList": {"items": [...]}}]} or
         {"rows": [{"modules": [...]}]}
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

    title = body.get("title") or ""
    raw_items = _collect_v2_items(body)
    out: list[dict] = []
    dropped_types: list[str] = []
    for entry in raw_items:
        serialized: Optional[dict] = None
        # First try tidalapi's parsers + our existing serializer. They
        # cover the common shape where Tidal includes every field the
        # parser expects.
        obj = _parse_v2_item(session, entry)
        if obj is not None:
            serialized = _serialize_page_item(obj)
        # Fallback: map the raw JSON straight to our wire shape. tidalapi's
        # parse methods insist on fields like numberOfVideos /
        # promotedArtists that Tidal drops from V2 view-all payloads,
        # so a playlist that renders fine on Home disappears here unless
        # we can bypass the strict parser.
        if serialized is None or _is_empty_shell(serialized):
            serialized = _raw_entry_to_item(entry) or serialized
        if serialized and not _is_empty_shell(serialized):
            out.append(serialized)
        else:
            dropped_types.append(
                (entry.get("type") if isinstance(entry, dict) else None) or "?"
            )
            if isinstance(entry, dict):
                try:
                    preview = json.dumps(entry)[:600]
                except Exception:
                    preview = repr(entry)[:600]
                print(
                    f"[page/resolve] could not build an item for "
                    f"type={entry.get('type')!r}; entry keys={list(entry.keys())}; "
                    f"sample={preview}",
                    file=sys.stderr,
                    flush=True,
                )

    if dropped_types:
        print(
            f"[page/resolve] view-all dropped {len(dropped_types)} item(s) for "
            f"path={path!r}; types={sorted(set(dropped_types))}",
            file=sys.stderr,
            flush=True,
        )

    if not out:
        preview = json.dumps(body)[:800] if isinstance(body, (dict, list)) else str(body)[:800]
        print(
            f"[page/resolve] view-all produced zero items for path={path!r}; "
            f"body preview: {preview}",
            file=sys.stderr,
            flush=True,
        )

    return {
        "title": title,
        "categories": [
            {"type": "HorizontalList", "title": "", "items": out},
        ],
    }


def _raw_entry_to_item(entry: Any) -> Optional[dict]:
    """Map a raw Tidal V2 item entry straight onto our PageItem shape.

    This is the fallback for when tidalapi's strict parsers reject a
    payload that is missing a field. The UI only needs id, name, a cover
    image, and enough metadata to render the card, so we can build that
    from whichever fields Tidal did include without demanding the full
    set tidalapi wants."""
    if not isinstance(entry, dict):
        return None
    item_type = (entry.get("type") or "").upper()
    data = _resolve_entry_payload(entry, item_type)

    def _cover(uuid: Optional[str]) -> Optional[str]:
        return _cover_url_from_uuid(uuid, size=640) if uuid else None

    def _artist_names(xs) -> list[dict]:
        out: list[dict] = []
        if not isinstance(xs, list):
            return out
        for a in xs:
            if not isinstance(a, dict):
                continue
            name = a.get("name") or ""
            aid = a.get("id")
            if name or aid is not None:
                out.append({"id": str(aid or ""), "name": name, "picture": a.get("picture")})
        return out

    try:
        if item_type == "PLAYLIST":
            return {
                "kind": "playlist",
                "id": str(data.get("uuid") or data.get("id") or ""),
                "name": data.get("title") or "",
                "description": data.get("description") or "",
                "num_tracks": int(data.get("numberOfTracks") or 0),
                "duration": int(data.get("duration") or 0),
                "cover": _cover(data.get("squareImage") or data.get("image")),
                "creator": (data.get("creator") or {}).get("name"),
                "creator_id": str((data.get("creator") or {}).get("id") or "") or None,
                "owned": False,
                "share_url": None,
            }
        if item_type == "ALBUM":
            return {
                "kind": "album",
                "id": str(data.get("id") or ""),
                "name": data.get("title") or "",
                "num_tracks": int(data.get("numberOfTracks") or 0),
                "year": _release_year(data.get("releaseDate") or data.get("streamStartDate")),
                "duration": int(data.get("duration") or 0),
                "cover": _cover(data.get("cover")),
                "artists": _artist_names(data.get("artists")),
                "explicit": bool(data.get("explicit")),
                "share_url": None,
                "release_date": data.get("releaseDate"),
                "copyright": data.get("copyright"),
                "media_tags": data.get("mediaMetadata", {}).get("tags") or [],
            }
        if item_type == "ARTIST":
            return {
                "kind": "artist",
                "id": str(data.get("id") or ""),
                "name": data.get("name") or "",
                "picture": _cover(data.get("picture")),
            }
        if item_type == "TRACK":
            album = data.get("album") or {}
            return {
                "kind": "track",
                "id": str(data.get("id") or ""),
                "name": data.get("title") or "",
                "duration": int(data.get("duration") or 0),
                "track_num": int(data.get("trackNumber") or 0),
                "explicit": bool(data.get("explicit")),
                "artists": _artist_names(data.get("artists")),
                "album": {
                    "id": str(album.get("id") or ""),
                    "name": album.get("title") or "",
                    "cover": _cover(album.get("cover")),
                } if album.get("id") else None,
                "share_url": None,
                "media_tags": data.get("mediaMetadata", {}).get("tags") or [],
                "isrc": data.get("isrc"),
            }
        if item_type == "MIX":
            return _raw_mix_to_item(data)
    except Exception:
        return None
    return None


_MIX_TYPE_LABELS = {
    "DAILY_MIX": "Daily Mix",
    "DISCOVERY_MIX": "Discovery Mix",
    "NEW_ARRIVALS_MIX": "New Arrivals",
    "HISTORY_ALLTIME_MIX": "My All-Time Mix",
    "HISTORY_RECENT_MIX": "Recent History",
    "ARTIST_MIX": "Artist Mix",
    "TRACK_MIX": "Track Radio",
    "ALBUM_MIX": "Album Radio",
    "GENRE_MIX": "Genre Mix",
    "DECADE_MIX": "Decade Mix",
}


def _raw_mix_to_item(data: dict) -> Optional[dict]:
    """Map a raw V2 mix payload to our wire shape.

    Tidal's V2 mix payloads come in two flavors depending on endpoint.
    Some ship the full home-feed shape with `title` / `subTitle` and an
    `images` dict keyed by SQUARE/MEDIUM/LARGE, others ship a stripped
    pages shape with `titleTextInfo.text` / `subtitleTextInfo.text` and
    a flat `mixImages` array of {size, url} objects. Handle both."""
    if not isinstance(data, dict):
        return None
    mix_id = str(data.get("id") or "").strip()
    if not mix_id:
        return None
    # Title: home-feed shipping uses `title`, pages shipping uses
    # `titleTextInfo.text`. Fall back to a human label derived from the
    # inner mix type constant so a missing text info never leaves the
    # card blank.
    title_info = data.get("titleTextInfo") if isinstance(data.get("titleTextInfo"), dict) else {}
    subtitle_info = data.get("subtitleTextInfo") if isinstance(data.get("subtitleTextInfo"), dict) else {}
    inner_type = (data.get("type") or "").upper()
    name = (
        data.get("title")
        or title_info.get("text")
        or _MIX_TYPE_LABELS.get(inner_type)
        or "Mix"
    )
    subtitle = (
        data.get("subTitle")
        or data.get("subtitle")
        or subtitle_info.get("text")
        or ""
    )
    # Cover: home-feed shape first, then the pages-shape array.
    images = data.get("images") if isinstance(data.get("images"), dict) else {}
    cover = None
    for bucket in ("SQUARE", "MEDIUM", "LARGE", "SMALL"):
        img = images.get(bucket) if isinstance(images, dict) else None
        if isinstance(img, dict) and img.get("url"):
            cover = img["url"]
            break
    if not cover:
        mix_images = data.get("mixImages")
        if isinstance(mix_images, list) and mix_images:
            # Prefer MEDIUM; fall back to whichever we have.
            best = next(
                (m for m in mix_images if isinstance(m, dict) and m.get("size") == "MEDIUM"),
                None,
            ) or next(
                (m for m in mix_images if isinstance(m, dict) and m.get("url")),
                None,
            )
            if best:
                cover = best.get("url")
    return {
        "kind": "mix",
        "id": mix_id,
        "name": name,
        "subtitle": subtitle,
        "cover": cover,
    }


def _release_year(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        return int(str(date_str)[:4])
    except Exception:
        return None


def _is_empty_shell(item: dict) -> bool:
    """True when an item dict has no id and no name — the card would
    render as a blank placeholder with a music icon. Better to drop it
    than to paint emptiness on the page."""
    if not isinstance(item, dict):
        return True
    has_id = bool(str(item.get("id") or "").strip())
    has_name = bool((item.get("name") or "").strip())
    return not (has_id and has_name)


def _resolve_entry_payload(entry: dict, item_type: str) -> dict:
    """Find the inner object inside a V2 view-all entry. Tidal puts the
    real payload in different slots depending on endpoint: some ship
    `data`, others ship `item`, others stash a type-specific key like
    `playlist`/`album`, and a few dump the fields straight onto the
    entry. Try each candidate in turn."""
    type_key = item_type.lower() if item_type else ""
    candidate_keys = ("data", "item", type_key) if type_key else ("data", "item")
    for key in candidate_keys:
        if not key:
            continue
        candidate = entry.get(key)
        if isinstance(candidate, dict) and candidate:
            return candidate
    # Fall through: treat the entry itself as the payload.
    return entry


def _collect_v2_items(body: Any) -> list:
    """Walk a Tidal V2 view-all response and collect everything that
    looks like an item. Handles a few shapes Tidal uses for different
    content types without requiring per-row special-casing."""
    if not isinstance(body, dict):
        return []
    # Shape 1 / 2: top-level items array.
    items = body.get("items")
    if isinstance(items, list) and items:
        return items
    # Shape 3: modules / rows wrapper — flatten one level.
    out: list = []
    for container_key in ("modules", "rows"):
        container = body.get(container_key)
        if not isinstance(container, list):
            continue
        for module in container:
            if not isinstance(module, dict):
                continue
            for inner_key in ("items", "pagedList"):
                inner = module.get(inner_key)
                if isinstance(inner, dict):
                    inner = inner.get("items")
                if isinstance(inner, list):
                    out.extend(inner)
    return out


def _parse_v2_item(session, entry: Any):
    """Turn a single V2 item dict into a tidalapi object, tolerating
    both the {type, data} wrapper and bare-object shapes. Returns None
    when the entry is something we don't render."""
    if not isinstance(entry, dict):
        return None
    item_type = (entry.get("type") or "").upper()
    data = entry.get("data") if isinstance(entry.get("data"), dict) else entry

    # Type wrapper present — dispatch by the declared type.
    if item_type:
        try:
            if item_type == "TRACK":
                return session.parse_track(data)
            if item_type == "ALBUM":
                return session.parse_album(data)
            if item_type == "ARTIST":
                return session.parse_artist(data)
            if item_type == "PLAYLIST":
                return session.parse_playlist(data)
            if item_type == "MIX":
                # parse_v2_mix covers the V2 home/pages mix shape
                # (mixImages / titleTextInfo); parse_mix is the V1
                # legacy parser that expects a flat title field.
                parser = getattr(session, "parse_v2_mix", None) or session.parse_mix
                return parser(data)
        except Exception:
            return None
        return None

    # No type wrapper — sniff the shape. Mixes carry a `mixType` or a
    # string id that starts with a known prefix; albums/tracks/playlists
    # carry numeric / uuid ids with distinguishing fields.
    try:
        if "mixType" in data or "mixNumber" in data:
            return session.parse_mix(data)
        if "numberOfTracks" in data and "artists" in data:
            return session.parse_album(data)
        if "numberOfTracks" in data and "creator" in data:
            return session.parse_playlist(data)
        if "album" in data and "duration" in data:
            return session.parse_track(data)
        if "picture" in data and "name" in data and "popularity" in data:
            return session.parse_artist(data)
    except Exception:
        return None
    return None


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
        serialized_pairs = [(i, _serialize_page_item(i)) for i in raw_items]
        items = [d for _, d in serialized_pairs if d]
        if len(items) < len(raw_items):
            # Log which tidalapi classes we dropped so a short row like
            # "Recently played shows 13 instead of 15" surfaces the
            # concrete types we still need to handle.
            dropped = sorted({
                "None" if raw is None else type(raw).__name__
                for raw, d in serialized_pairs if d is None
            })
            if dropped:
                print(
                    f"[page] {cat_type} {title!r} served "
                    f"{len(items)}/{len(raw_items)} items; dropped classes={dropped}",
                    file=sys.stderr,
                    flush=True,
                )
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
    except Exception as exc:  # noqa: BLE001 — need a catch-all to log body
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
    result = _serialize_page(page)
    if not result.get("categories"):
        # V1 page returned but every row was filtered out during
        # serialization — usually because tidalapi handed back a class
        # name _serialize_page_item doesn't recognise. Log a preview so
        # we can see which types went missing.
        preview = []
        for cat in getattr(page, "categories", []) or []:
            raw_items = list(getattr(cat, "items", []) or [])
            preview.append({
                "category": type(cat).__name__,
                "title": getattr(cat, "title", "") or "",
                "item_types": sorted({type(x).__name__ for x in raw_items}),
                "count": len(raw_items),
            })
        print(
            f"[page/resolve] V1 page had zero renderable categories "
            f"for path={path!r}; raw rows: {preview}",
            flush=True,
        )
    return result


# ---------------------------------------------------------------------------
# Favorites (like / follow)
# ---------------------------------------------------------------------------


FAVORITE_KINDS = {"track", "album", "artist", "playlist", "mix"}


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
        tidal_jitter_sleep()
        try:
            artist = tidal.session.artist(int(aid))
            # get_artist_releases includes albums + EPs + singles. The
            # full Tidal client shows all three on its new-releases
            # surface; using get_albums alone silently drops singles.
            return tidal.get_artist_releases(artist, limit=30)
        except Exception:
            return []

    # Cap fan-out aggressively — this path walks every followed artist
    # for fresh releases, which can be dozens of requests per refresh.
    # Three workers is slower but keeps the pattern from tripping
    # Tidal's abuse detection on users with large follow lists.
    seen_album_ids: set[str] = set()
    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=3) as pool:
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
        resp = SESSION.get(url, timeout=10, stream=True, allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            resp.close()
            raise HTTPException(status_code=502, detail="Upstream redirect refused")
        resp.raise_for_status()
        declared = int(resp.headers.get("Content-Length") or 0)
        if declared and declared > MAX_IMAGE_BYTES:
            resp.close()
            raise HTTPException(status_code=413, detail="Image too large")
        content_type = resp.headers.get("Content-Type", "image/jpeg")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # Stream the bytes straight to the client instead of buffering
    # the entire image into memory before responding. This cuts
    # first-byte latency for every cover on every page, and keeps
    # peak memory flat regardless of concurrent image requests.
    # The MAX_IMAGE_BYTES safety check moves into the generator.
    def _iter() -> Generator[bytes, None, None]:
        streamed = 0
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                streamed += len(chunk)
                if streamed > MAX_IMAGE_BYTES:
                    # We've already started writing to the socket, so
                    # we can't raise HTTPException here. Just stop
                    # emitting; the client gets a truncated payload
                    # which is harmless for an already-oversized
                    # response the client shouldn't have trusted
                    # anyway. Tidal's covers never come close to
                    # 5 MB in practice.
                    return
                yield chunk
        finally:
            try:
                resp.close()
            except Exception:
                pass

    return StreamingResponse(
        _iter(),
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
