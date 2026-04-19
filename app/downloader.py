import queue
import re
import threading
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import tidalapi

from app.http import SESSION

# Upper bound on worker threads — the Downloader spawns this many, but at
# most `settings.concurrent_downloads` run actual work at any moment. The
# user can slide the setting up or down at runtime without restart.
MAX_WORKER_THREADS = 10
DEFAULT_CONCURRENT_DOWNLOADS = 3
# Minimum progress delta between SSE updates; prevents broadcasting every
# 64KB chunk (~800 events per FLAC) while keeping the bar feeling live.
PROGRESS_UPDATE_THRESHOLD = 0.01


class ConcurrencyGate:
    """A resizable semaphore. Workers call `acquire()` before downloading
    and `release()` when done; only `limit` acquires can be outstanding at
    once. Calling `set_limit()` wakes any worker that now fits under the
    new cap. Under contraction, excess workers keep running until they
    finish their current item — we don't kill in-flight downloads.
    """

    def __init__(self, initial: int) -> None:
        self._limit = max(1, int(initial))
        self._active = 0
        self._cond = threading.Condition()

    def acquire(self) -> None:
        with self._cond:
            while self._active >= self._limit:
                self._cond.wait()
            self._active += 1

    def release(self) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()

    def set_limit(self, new_limit: int) -> None:
        new_limit = max(1, min(MAX_WORKER_THREADS, int(new_limit)))
        with self._cond:
            self._limit = new_limit
            # Wake potentially-blocked workers so any that now fit can run.
            self._cond.notify_all()


class DownloadStatus(Enum):
    PENDING = "Pending"
    FETCHING = "Fetching…"
    IN_PROGRESS = "Downloading"
    TAGGING = "Tagging…"
    COMPLETE = "Complete"
    FAILED = "Failed"


@dataclass
class DownloadItem:
    item_id: str
    url: str
    title: str = "Fetching info…"
    artist: str = ""
    album: str = ""
    track_num: int = 0
    status: DownloadStatus = DownloadStatus.PENDING
    progress: float = 0.0
    error: Optional[str] = None
    quality: Optional[str] = None  # overrides session quality for this item
    file_path: Optional[str] = None  # final on-disk path once complete


class Downloader:
    def __init__(
        self,
        tidal_client,
        settings,
        on_add: Callable[[DownloadItem], None],
        on_update: Callable[[DownloadItem], None],
        on_remove: Optional[Callable[[str], None]] = None,
        on_file_ready: Optional[Callable[[str, Path], None]] = None,
    ):
        self.tidal = tidal_client
        self.settings = settings
        self.on_add = on_add
        self.on_update = on_update
        self.on_remove = on_remove or (lambda _: None)
        # Called with (tidal_track_id, final_path) when a track finishes
        # (including skip-existing). The server uses this to keep its local
        # index up to date without having to re-scan the output_dir.
        self.on_file_ready = on_file_ready or (lambda _id, _path: None)
        # _track_map is read/written from submit threads AND worker threads;
        # Python dict ops are atomic for single keys in CPython but multi-step
        # sequences (check-then-pop) aren't. Guard explicitly.
        self._track_map: Dict[str, Any] = {}
        self._track_map_lock = threading.Lock()
        self._work_queue: queue.Queue = queue.Queue()
        # Serializes any mutation of session.config.quality. Also serializes
        # reads of track.get_url() so a concurrent Settings PUT or preview
        # request can't swap quality mid-download. Exposed so the preview
        # endpoint can coordinate.
        self.quality_lock = threading.Lock()
        # Always spawn the full worker pool; the gate throttles how many
        # of them actually pull work at once.
        initial_limit = getattr(settings, "concurrent_downloads", DEFAULT_CONCURRENT_DOWNLOADS)
        self.gate = ConcurrencyGate(initial_limit)
        for _ in range(MAX_WORKER_THREADS):
            threading.Thread(target=self._worker_loop, daemon=True).start()

    # ------------------------------------------------------------------
    # _track_map helpers — always locked
    # ------------------------------------------------------------------

    def _track_map_put(self, item_id: str, pair) -> None:
        with self._track_map_lock:
            self._track_map[item_id] = pair

    def _track_map_get(self, item_id: str):
        with self._track_map_lock:
            return self._track_map.get(item_id, (None, None))

    def _track_map_pop(self, item_id: str) -> None:
        with self._track_map_lock:
            self._track_map.pop(item_id, None)

    def _track_map_has(self, item_id: str) -> bool:
        with self._track_map_lock:
            return item_id in self._track_map

    def submit(self, url: str, quality: Optional[str] = None):
        threading.Thread(target=self._expand_and_enqueue, args=(url, quality), daemon=True).start()

    def submit_object(self, obj, content_type: str, quality: Optional[str] = None):
        """Enqueue a tidalapi object directly — skips the URL fetch step."""
        threading.Thread(
            target=self._enqueue_object, args=(obj, content_type, quality), daemon=True
        ).start()

    def _enqueue_object(self, obj, content_type: str, quality: Optional[str] = None):
        import sys as _sys

        print(
            f"[downloader] _enqueue_object kind={content_type} "
            f"id={getattr(obj, 'id', '?')} quality={quality!r}",
            file=_sys.stderr,
            flush=True,
        )
        pairs: list[tuple]
        try:
            if content_type == "track":
                pairs = [(obj, getattr(obj, "album", None))]
            elif content_type == "album":
                tracks = self._call_with_auth_retry(obj.tracks)
                pairs = [(t, obj) for t in tracks]
            elif content_type == "playlist":
                tracks = self._call_with_auth_retry(obj.tracks)
                pairs = [(t, getattr(t, "album", None)) for t in tracks]
            else:
                print(
                    f"[downloader] _enqueue_object: unsupported kind {content_type!r}",
                    file=_sys.stderr,
                    flush=True,
                )
                return
        except Exception as exc:
            print(
                f"[downloader] _enqueue_object expand FAILED kind={content_type} "
                f"exc={exc!r}",
                file=_sys.stderr,
                flush=True,
            )
            # Surface the failure instead of swallowing silently — otherwise
            # the user clicks Download and nothing happens.
            self._surface_enqueue_failure(content_type, exc)
            return

        print(
            f"[downloader] _enqueue_object enqueuing {len(pairs)} track(s)",
            file=_sys.stderr,
            flush=True,
        )
        for track, album_obj in pairs:
            item = DownloadItem(item_id=str(uuid.uuid4()), url="")
            item.title = track.name
            item.artist = _artist_names(track)
            item.album = _album_name(album_obj or getattr(track, "album", None))
            item.track_num = getattr(track, "track_num", 0)
            item.quality = quality
            self._track_map_put(item.item_id, (track, album_obj))
            self.on_add(item)
            self._work_queue.put(item)

    def _call_with_auth_retry(self, fn, *args, **kwargs):
        """Call a Tidal-hitting function, retry once on 401 after forcing
        a token refresh. Used for `album.tracks()` / `playlist.tracks()`
        in the enqueue-expand path, which are separate API calls from the
        initial session.album/playlist lookup and can 401 on their own.
        tidalapi's built-in refresh only fires when the 401 body carries
        the exact string 'The token has expired.' — Tidal often doesn't,
        so we handle it ourselves.
        """
        import sys as _sys

        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not _looks_like_auth_error(exc):
                raise
            print(
                f"[downloader] auth error on {getattr(fn, '__name__', fn)!r}: "
                f"{exc!r} — forcing refresh",
                file=_sys.stderr,
                flush=True,
            )
            refresh = getattr(self.tidal, "force_refresh", None)
            if callable(refresh) and refresh():
                return fn(*args, **kwargs)
            raise

    def _surface_enqueue_failure(self, content_type: str, exc: Exception) -> None:
        placeholder = DownloadItem(item_id=str(uuid.uuid4()), url="")
        placeholder.title = f"Couldn't expand {content_type}"
        placeholder.status = DownloadStatus.FAILED
        placeholder.error = str(exc)
        self.on_add(placeholder)

    # ------------------------------------------------------------------

    def _expand_and_enqueue(self, url: str, quality: Optional[str] = None):
        placeholder = DownloadItem(item_id=str(uuid.uuid4()), url=url)
        self.on_add(placeholder)

        try:
            content_type, obj = self.tidal.fetch_url(url)
        except Exception as exc:
            placeholder.status = DownloadStatus.FAILED
            placeholder.error = str(exc)
            self.on_update(placeholder)
            return

        if content_type == "track":
            pairs = [(obj, getattr(obj, "album", None))]
        elif content_type == "album":
            pairs = [(t, obj) for t in obj.tracks()]
        elif content_type == "playlist":
            pairs = [(t, getattr(t, "album", None)) for t in obj.tracks()]
        else:
            placeholder.status = DownloadStatus.FAILED
            placeholder.error = f"Unsupported type: {content_type}"
            self.on_update(placeholder)
            return

        if len(pairs) == 1:
            track, album_obj = pairs[0]
            placeholder.title = track.name
            placeholder.artist = _artist_names(track)
            placeholder.album = _album_name(album_obj or getattr(track, "album", None))
            placeholder.track_num = getattr(track, "track_num", 0)
            placeholder.quality = quality
            self._track_map_put(placeholder.item_id, (track, album_obj))
            self.on_update(placeholder)
            self._work_queue.put(placeholder)
        else:
            # Drop the placeholder entirely — the per-track items replace it.
            self.on_remove(placeholder.item_id)

            for track, album_obj in pairs:
                item = DownloadItem(item_id=str(uuid.uuid4()), url=url)
                item.title = track.name
                item.artist = _artist_names(track)
                item.album = _album_name(album_obj or getattr(track, "album", None))
                item.track_num = getattr(track, "track_num", 0)
                item.quality = quality
                self._track_map_put(item.item_id, (track, album_obj))
                self.on_add(item)
                self._work_queue.put(item)

    def retry(self, item: DownloadItem, quality: Optional[str] = None) -> None:
        """Re-queue an existing item. Used by the 'Retry failed' button.

        Accepts an optional `quality` so the caller can bump a failed
        hi-res download down to Lossless without re-adding it by hand.
        """
        if not self._track_map_has(item.item_id):
            return
        if quality is not None:
            item.quality = quality
        item.status = DownloadStatus.PENDING
        item.progress = 0.0
        item.error = None
        self.on_update(item)
        self._work_queue.put(item)

    def _worker_loop(self):
        while True:
            item = self._work_queue.get()
            # Block here if we're over the concurrency limit. Workers that
            # wake up already-dequeued an item, so we don't need to guard
            # against items getting lost on cancellation.
            self.gate.acquire()
            try:
                self._download(item)
            finally:
                self.gate.release()

    def _download(self, item: DownloadItem):
        import sys as _sys
        import traceback as _tb

        print(
            f"[downloader] _download START id={item.item_id[:8]} "
            f"title={item.title!r} quality={item.quality!r}",
            file=_sys.stderr,
            flush=True,
        )
        tmp_path: Optional[Path] = None
        # Snapshot settings once at the top so a concurrent Settings PUT
        # that swaps self.settings mid-download can't tear reads of
        # output_dir / filename_template / create_album_folders across
        # `_find_existing` and `_build_path`. Without this, a user flipping
        # create_album_folders between the skip-existing check and the
        # write would scan one tree but write into another.
        s = self.settings
        try:
            track, album_obj = self._track_map_get(item.item_id)
            if track is None:
                raise RuntimeError("Track reference lost")

            # Skip-existing: if any audio file with the same stem already
            # lives at the destination, treat the item as complete.
            if getattr(s, "skip_existing", True):
                existing = _find_existing(item, s)
                if existing is not None:
                    item.progress = 1.0
                    item.status = DownloadStatus.COMPLETE
                    # Note, not error — UI treats error as a failure banner.
                    item.error = None
                    item.file_path = str(existing)
                    self.on_update(item)
                    tid = getattr(track, "id", None)
                    if tid is not None:
                        self.on_file_ready(str(tid), existing)
                    self._track_map_pop(item.item_id)
                    return

            item.status = DownloadStatus.IN_PROGRESS
            item.progress = 0.0
            self.on_update(item)

            print(
                f"[downloader] _download id={item.item_id[:8]} fetching stream URL "
                f"track_id={getattr(track, 'id', '?')} quality={item.quality!r}",
                file=_sys.stderr,
                flush=True,
            )
            urls, ext_hint = self._fetch_stream_sources(track, item.quality)
            if not urls:
                raise RuntimeError("Tidal returned no stream URLs")
            print(
                f"[downloader] _download id={item.item_id[:8]} got "
                f"{len(urls)} URL(s) ext_hint={ext_hint!r}",
                file=_sys.stderr,
                flush=True,
            )

            # For the device-code path we can't know the final extension
            # until the first response's Content-Type arrives. For PKCE
            # the manifest gives us a reliable hint up front. Open the
            # first URL to resolve the extension, then write it + every
            # remaining URL sequentially into the same .part file.
            first_resp_cm = SESSION.get(urls[0], stream=True, timeout=60)
            first_resp = first_resp_cm.__enter__()
            try:
                first_resp.raise_for_status()
                ext = ext_hint or _ext_from_response(first_resp)
                out_path = _build_path(item, s, ext)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = out_path.with_suffix(out_path.suffix + ".part")

                # Progress tracking. For multi-URL DASH downloads we
                # don't know total bytes up front, so we treat each URL
                # as an equal slice of the progress bar. Within a URL
                # with known Content-Length we interpolate.
                total_urls = len(urls)
                first_len = int(first_resp.headers.get("Content-Length", 0))
                last_published = 0.0

                def _bump(url_idx: int, inner: float) -> None:
                    nonlocal last_published
                    item.progress = min(0.999, (url_idx + inner) / total_urls)
                    if item.progress - last_published >= PROGRESS_UPDATE_THRESHOLD:
                        last_published = item.progress
                        self.on_update(item)

                with open(tmp_path, "wb") as f:
                    # Write the first URL we already opened.
                    got = 0
                    for chunk in first_resp.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        f.write(chunk)
                        got += len(chunk)
                        inner = (got / first_len) if first_len else 0.5
                        _bump(0, inner)
                    first_resp_cm.__exit__(None, None, None)
                    first_resp_cm = None

                    # Then every remaining URL concatenated into the
                    # same file — for DASH hi-res these are per-segment
                    # binary chunks that form a valid FLAC once joined.
                    for i, url in enumerate(urls[1:], start=1):
                        with SESSION.get(url, stream=True, timeout=60) as resp:
                            resp.raise_for_status()
                            seg_len = int(resp.headers.get("Content-Length", 0))
                            seg_got = 0
                            for chunk in resp.iter_content(chunk_size=65536):
                                if not chunk:
                                    continue
                                f.write(chunk)
                                seg_got += len(chunk)
                                inner = (seg_got / seg_len) if seg_len else 0.5
                                _bump(i, inner)
            finally:
                if first_resp_cm is not None:
                    try:
                        first_resp_cm.__exit__(None, None, None)
                    except Exception:
                        pass

            # Atomic rename — the next skip-existing scan sees a complete file
            # or nothing at all.
            tmp_path.replace(out_path)
            tmp_path = None

            item.progress = 1.0
            item.status = DownloadStatus.TAGGING
            self.on_update(item)

            from app.metadata import fetch_cover_art, tag_file
            cover = fetch_cover_art(album_obj or getattr(track, "album", None))
            tag_file(out_path, track, cover)

            item.file_path = str(out_path)
            item.status = DownloadStatus.COMPLETE
            self.on_update(item)
            tid = getattr(track, "id", None)
            if tid is not None:
                self.on_file_ready(str(tid), out_path)

        except Exception as exc:
            print(
                f"[downloader] _download FAILED id={item.item_id[:8]} "
                f"title={item.title!r} exc={exc!r}",
                file=_sys.stderr,
                flush=True,
            )
            _tb.print_exc(file=_sys.stderr)
            item.status = DownloadStatus.FAILED
            item.error = str(exc)
            self.on_update(item)
            # Clean up a partial file so the next attempt starts fresh and
            # skip-existing can't be fooled by it.
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
            # Keep track_map entry so retry() still works for failed items.
            return

        print(
            f"[downloader] _download DONE id={item.item_id[:8]} title={item.title!r}",
            file=_sys.stderr,
            flush=True,
        )
        # Drop the cached tidalapi reference on success so long sessions
        # don't grow unbounded.
        self._track_map_pop(item.item_id)

    def _fetch_stream_sources(
        self, track, quality: Optional[str]
    ) -> tuple[list[str], Optional[str]]:
        """Fetch the list of URLs we need to download + a file-extension
        hint. Handles both session types:

        * Device-code sessions use `track.get_url()` — a single streamable
          URL. One entry in the returned list, no extension hint.
        * PKCE sessions can't call get_url (tidalapi raises URLNotAvailable
          immediately). They use `track.get_stream()` which returns a
          manifest (MPEG-DASH or BTS) whose `urls` is a list of segment
          URLs. For DASH hi-res content that list may have dozens of
          short segments which we concatenate into one FLAC file. The
          manifest also carries a reliable file_extension.

        Both paths retry once on auth error with a forced token refresh
        since tidalapi's built-in refresh triggers only on a very
        specific error message Tidal doesn't always send.
        """
        override: Optional[tidalapi.Quality] = None
        if quality:
            try:
                override = tidalapi.Quality[quality]
            except KeyError:
                override = None

        def _call() -> tuple[list[str], Optional[str]]:
            with self.quality_lock:
                original = self.tidal.session.config.quality
                try:
                    if override is not None:
                        self.tidal.session.config.quality = override
                    if getattr(self.tidal.session, "is_pkce", False):
                        # PKCE path: manifest-based stream.
                        stream = track.get_stream()
                        manifest = stream.get_stream_manifest()
                        if getattr(manifest, "is_encrypted", False):
                            # Encrypted streams would need per-segment
                            # decryption keys we don't have. Refuse
                            # loudly rather than write a corrupt file.
                            raise RuntimeError(
                                "Tidal returned an encrypted stream we can't decrypt"
                            )
                        ext_hint = getattr(manifest, "file_extension", None)
                        return (list(manifest.urls or []), ext_hint)
                    # Device-code path: single direct URL.
                    return ([track.get_url()], None)
                finally:
                    if override is not None:
                        self.tidal.session.config.quality = original

        import sys as _sys
        try:
            return _call()
        except Exception as exc:
            print(
                f"[downloader] _fetch_stream_sources FAILED track_id="
                f"{getattr(track, 'id', '?')} quality_override={quality!r} "
                f"exc={exc!r}",
                file=_sys.stderr,
                flush=True,
            )
            if _looks_like_auth_error(exc):
                refresh = getattr(self.tidal, "force_refresh", None)
                if callable(refresh) and refresh():
                    print(
                        "[downloader] _fetch_stream_sources retrying after refresh",
                        file=_sys.stderr,
                        flush=True,
                    )
                    return _call()
            raise

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _looks_like_auth_error(exc: Exception) -> bool:
    """Best-effort detection of a Tidal 401/auth error so we can trigger
    a token refresh and retry. `requests.HTTPError` carries the response
    with a status code; tidalapi sometimes re-raises as plain RuntimeError
    whose string includes "401". Either path is recognized here.
    """
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) in (401, 403):
        return True
    msg = str(exc)
    return "401" in msg or "Unauthorized" in msg


def _artist_names(track) -> str:
    try:
        return ", ".join(a.name for a in track.artists)
    except Exception:
        pass
    try:
        return track.artist.name
    except Exception:
        return ""


def _album_name(album_obj) -> str:
    try:
        return album_obj.name
    except Exception:
        return ""


def _ext_from_response(resp) -> str:
    ct = resp.headers.get("Content-Type", "").lower()
    if "flac" in ct:
        return ".flac"
    if "mp4" in ct or "m4a" in ct or "aac" in ct:
        return ".m4a"
    if "mpeg" in ct or "mp3" in ct:
        return ".mp3"
    url = resp.url.lower().split("?")[0]
    for ext in (".flac", ".m4a", ".mp3", ".mp4"):
        if url.endswith(ext):
            return ext
    return ".flac"


_WIN_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_MAX_SEGMENT = 180  # well under 255 to leave room for extensions and ancestors


def _sanitize_segment(name: str) -> str:
    """Make a single path segment safe on macOS, Linux, and Windows."""
    if not name:
        return "_"
    # Strip forbidden chars + control bytes.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    # Windows: trailing dots/spaces are stripped by the shell, which breaks
    # round-tripping. Strip them ourselves.
    name = name.rstrip(". ")
    # Windows: certain stems are reserved regardless of extension.
    stem = name.split(".", 1)[0].upper()
    if stem in _WIN_RESERVED:
        name = f"_{name}"
    # Hard length cap (bytes ≤ 255 on most filesystems).
    if len(name.encode("utf-8", errors="ignore")) > _MAX_SEGMENT:
        name = name.encode("utf-8")[:_MAX_SEGMENT].decode("utf-8", errors="ignore").rstrip()
    return name or "_"


def _build_path(item: DownloadItem, settings, ext: str) -> Path:
    # Defense-in-depth: sanitize each interpolation value BEFORE the
    # template renders it, then sanitize the whole name afterwards. That
    # way a literal path separator in either the template or any tidalapi-
    # supplied field still collapses to an underscore instead of escaping
    # the output directory.
    name = settings.filename_template.format(
        title=_sanitize_segment(item.title),
        artist=_sanitize_segment(item.artist),
        album=_sanitize_segment(item.album),
        track_num=str(item.track_num).zfill(2),
    )
    base = Path(settings.output_dir)
    if settings.create_album_folders and item.album:
        base = base / _sanitize_segment(item.album)
    final = base / (_sanitize_segment(name) + ext)
    # Hard containment check: after all the sanitization, the resolved
    # path must still live under output_dir. If it somehow doesn't, a
    # future regression introduced a vector we missed — fail loudly
    # rather than silently write outside the sandbox.
    try:
        root = Path(settings.output_dir).resolve()
        if root not in final.resolve().parents and final.resolve() != root:
            raise RuntimeError(f"Resolved path escaped output_dir: {final}")
    except RuntimeError:
        raise
    except Exception:
        # resolve() can fail on not-yet-created paths on some FSes;
        # fall through — the parent mkdir will surface real issues.
        pass
    return final


def _find_existing(item: DownloadItem, settings) -> Optional[Path]:
    """Return the path of an already-downloaded file for this item, if any."""
    candidate = _build_path(item, settings, ".flac")
    parent = candidate.parent
    stem = candidate.stem
    if not parent.exists():
        return None
    try:
        for child in parent.iterdir():
            if child.is_file() and child.stem == stem and child.suffix.lower() in (".flac", ".m4a", ".mp3", ".mp4"):
                return child
    except Exception:
        return None
    return None
