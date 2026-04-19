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

MAX_CONCURRENT_DOWNLOADS = 3
# Minimum progress delta between SSE updates; prevents broadcasting every
# 64KB chunk (~800 events per FLAC) while keeping the bar feeling live.
PROGRESS_UPDATE_THRESHOLD = 0.01


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
        for _ in range(MAX_CONCURRENT_DOWNLOADS):
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
        pairs: list[tuple]
        try:
            if content_type == "track":
                pairs = [(obj, getattr(obj, "album", None))]
            elif content_type == "album":
                pairs = [(t, obj) for t in obj.tracks()]
            elif content_type == "playlist":
                pairs = [(t, getattr(t, "album", None)) for t in obj.tracks()]
            else:
                return
        except Exception as exc:
            # Surface the failure instead of swallowing silently — otherwise
            # the user clicks Download and nothing happens.
            self._surface_enqueue_failure(content_type, exc)
            return

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

    def retry(self, item: DownloadItem):
        """Re-queue an existing item. Used by the 'Retry failed' button."""
        if not self._track_map_has(item.item_id):
            return
        item.status = DownloadStatus.PENDING
        item.progress = 0.0
        item.error = None
        self.on_update(item)
        self._work_queue.put(item)

    def _worker_loop(self):
        while True:
            item = self._work_queue.get()
            self._download(item)

    def _download(self, item: DownloadItem):
        tmp_path: Optional[Path] = None
        try:
            track, album_obj = self._track_map_get(item.item_id)
            if track is None:
                raise RuntimeError("Track reference lost")

            # Skip-existing: if any audio file with the same stem already
            # lives at the destination, treat the item as complete.
            if getattr(self.settings, "skip_existing", True):
                existing = _find_existing(item, self.settings)
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

            stream_url = self._fetch_stream_url(track, item.quality)

            # Stream into a .part file so a crashed/aborted run can't leave
            # a truncated audio file behind that the next run would mistake
            # for a complete download.
            with SESSION.get(stream_url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                ext = _ext_from_response(resp)
                out_path = _build_path(item, self.settings, ext)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = out_path.with_suffix(out_path.suffix + ".part")

                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                last_published = 0.0
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            item.progress = downloaded / total
                        else:
                            # Heartbeat for chunked-encoding responses where
                            # Content-Length is unknown.
                            item.progress = min(0.95, item.progress + 0.01)
                        if item.progress - last_published >= PROGRESS_UPDATE_THRESHOLD:
                            last_published = item.progress
                            self.on_update(item)

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

        # Drop the cached tidalapi reference on success so long sessions
        # don't grow unbounded.
        self._track_map_pop(item.item_id)

    def _fetch_stream_url(self, track, quality: Optional[str]) -> str:
        """Fetch the track's stream URL, honoring per-item quality.

        tidalapi's Track.get_url() reads quality from session.config.quality,
        which is mutable module-shared state. We always take the quality
        lock (even when not overriding) so a concurrent Settings PUT or
        preview request can't swap quality mid-read and hand us back a URL
        for the wrong tier.
        """
        override: Optional[tidalapi.Quality] = None
        if quality:
            try:
                override = tidalapi.Quality[quality]
            except KeyError:
                override = None

        with self.quality_lock:
            original = self.tidal.session.config.quality
            try:
                if override is not None:
                    self.tidal.session.config.quality = override
                return track.get_url()
            finally:
                if override is not None:
                    self.tidal.session.config.quality = original


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

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
    name = settings.filename_template.format(
        title=item.title,
        artist=item.artist,
        album=item.album,
        track_num=str(item.track_num).zfill(2),
    )
    base = Path(settings.output_dir)
    if settings.create_album_folders and item.album:
        base = base / _sanitize_segment(item.album)
    return base / (_sanitize_segment(name) + ext)


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
