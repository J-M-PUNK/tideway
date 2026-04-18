import queue
import re
import threading
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.http import SESSION

MAX_CONCURRENT_DOWNLOADS = 3


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


class Downloader:
    def __init__(
        self,
        tidal_client,
        settings,
        on_add: Callable[[DownloadItem], None],
        on_update: Callable[[DownloadItem], None],
    ):
        self.tidal = tidal_client
        self.settings = settings
        self.on_add = on_add
        self.on_update = on_update
        self._track_map: Dict[str, Any] = {}
        self._work_queue: queue.Queue = queue.Queue()
        for _ in range(MAX_CONCURRENT_DOWNLOADS):
            threading.Thread(target=self._worker_loop, daemon=True).start()

    def submit(self, url: str):
        threading.Thread(target=self._expand_and_enqueue, args=(url,), daemon=True).start()

    def submit_object(self, obj, content_type: str):
        """Enqueue a tidalapi object directly — skips the URL fetch step."""
        threading.Thread(target=self._enqueue_object, args=(obj, content_type), daemon=True).start()

    def _enqueue_object(self, obj, content_type: str):
        if content_type == "track":
            pairs = [(obj, getattr(obj, "album", None))]
        elif content_type == "album":
            try:
                pairs = [(t, obj) for t in obj.tracks()]
            except Exception as exc:
                return
        elif content_type == "playlist":
            try:
                pairs = [(t, getattr(t, "album", None)) for t in obj.tracks()]
            except Exception:
                return
        else:
            return

        for track, album_obj in pairs:
            item = DownloadItem(item_id=str(uuid.uuid4()), url="")
            item.title = track.name
            item.artist = _artist_names(track)
            item.album = _album_name(album_obj or getattr(track, "album", None))
            item.track_num = getattr(track, "track_num", 0)
            self._track_map[item.item_id] = (track, album_obj)
            self.on_add(item)
            self._work_queue.put(item)

    # ------------------------------------------------------------------

    def _expand_and_enqueue(self, url: str):
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
            self._track_map[placeholder.item_id] = (track, album_obj)
            self.on_update(placeholder)
            self._work_queue.put(placeholder)
        else:
            # Hide the placeholder row and create one item per track
            placeholder.status = DownloadStatus.COMPLETE
            placeholder.title = f"Expanded {len(pairs)} tracks"
            self.on_update(placeholder)

            for track, album_obj in pairs:
                item = DownloadItem(item_id=str(uuid.uuid4()), url=url)
                item.title = track.name
                item.artist = _artist_names(track)
                item.album = _album_name(album_obj or getattr(track, "album", None))
                item.track_num = getattr(track, "track_num", 0)
                self._track_map[item.item_id] = (track, album_obj)
                self.on_add(item)
                self._work_queue.put(item)

    def retry(self, item: DownloadItem):
        """Re-queue an existing item. Used by the 'Retry failed' button."""
        if item.item_id not in self._track_map:
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
        try:
            track, album_obj = self._track_map.get(item.item_id, (None, None))
            if track is None:
                raise RuntimeError("Track reference lost")

            # Skip-existing: if any audio file with the same stem already
            # lives at the destination, treat the item as complete.
            if getattr(self.settings, "skip_existing", True):
                existing = _find_existing(item, self.settings)
                if existing is not None:
                    item.progress = 1.0
                    item.status = DownloadStatus.COMPLETE
                    item.error = "Already on disk"
                    self.on_update(item)
                    return

            item.status = DownloadStatus.IN_PROGRESS
            item.progress = 0.0
            self.on_update(item)

            stream_url = track.get_url()
            resp = SESSION.get(stream_url, stream=True, timeout=60)
            resp.raise_for_status()

            ext = _ext_from_response(resp)
            out_path = _build_path(item, self.settings, ext)
            out_path.parent.mkdir(parents=True, exist_ok=True)

            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    item.progress = (downloaded / total) if total else 0.5
                    self.on_update(item)

            item.progress = 1.0
            item.status = DownloadStatus.TAGGING
            self.on_update(item)

            from app.metadata import fetch_cover_art, tag_file
            cover = fetch_cover_art(album_obj or getattr(track, "album", None))
            tag_file(out_path, track, cover)

            item.status = DownloadStatus.COMPLETE
            self.on_update(item)

        except Exception as exc:
            item.status = DownloadStatus.FAILED
            item.error = str(exc)
            self.on_update(item)


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


def _build_path(item: DownloadItem, settings, ext: str) -> Path:
    name = settings.filename_template.format(
        title=item.title,
        artist=item.artist,
        album=item.album,
        track_num=str(item.track_num).zfill(2),
    )
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    base = Path(settings.output_dir)
    if settings.create_album_folders and item.album:
        base = base / re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", item.album)
    return base / (name + ext)


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
