import hashlib
import threading
from io import BytesIO
from pathlib import Path
from typing import Callable, Tuple

from PIL import Image
import customtkinter as ctk

from app.http import SESSION

# In-memory cache keyed by (url, size) → CTkImage
_cache: dict = {}
_lock = threading.Lock()
_sem = threading.Semaphore(24)  # max concurrent fetches; Tidal CDN handles this fine

# Disk cache — raw JPEG bytes keyed by URL hash, survives app restarts.
_DISK_DIR = Path.home() / ".tidal_downloader_cache" / "images"
_DISK_DIR.mkdir(parents=True, exist_ok=True)


def placeholder(size: Tuple[int, int]) -> ctk.CTkImage:
    img = Image.new("RGB", size, color=(45, 45, 45))
    return ctk.CTkImage(light_image=img, dark_image=img, size=size)


def load_async(url: str, size: Tuple[int, int], callback: Callable[[ctk.CTkImage], None]):
    key = (url, size)
    with _lock:
        if key in _cache:
            callback(_cache[key])
            return
    threading.Thread(target=_fetch, args=(url, size, callback), daemon=True).start()


def _disk_path(url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return _DISK_DIR / f"{digest}.jpg"


def _load_from_disk(url: str) -> bytes | None:
    path = _disk_path(url)
    if path.exists():
        try:
            return path.read_bytes()
        except Exception:
            return None
    return None


def _save_to_disk(url: str, data: bytes):
    try:
        _disk_path(url).write_bytes(data)
    except Exception:
        pass


def _fetch(url: str, size: Tuple[int, int], callback: Callable):
    key = (url, size)
    raw = _load_from_disk(url)
    if raw is None:
        with _sem:
            try:
                resp = SESSION.get(url, timeout=10)
                resp.raise_for_status()
                raw = resp.content
                _save_to_disk(url, raw)
            except Exception:
                return

    try:
        pil = Image.open(BytesIO(raw)).convert("RGB").resize(size, Image.LANCZOS)
        img = ctk.CTkImage(light_image=pil, dark_image=pil, size=size)
        with _lock:
            _cache[key] = img
        callback(img)
    except Exception:
        pass
