"""Index of downloaded tracks on disk, keyed by Tidal track ID.

The downloader writes a TIDAL_TRACK_ID tag into every file we produce. This
module scans the output directory at startup and builds a map from track ID
to file path so the player can decide whether to serve a local file or fall
back to a Tidal preview stream.

Thread-safe: writes happen from the download worker threads, reads happen
from FastAPI request handlers. A single lock guards both.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from app.metadata import read_track_id

_AUDIO_EXTS = {".flac", ".m4a", ".mp3", ".mp4"}


class LocalIndex:
    def __init__(self) -> None:
        self._by_id: dict[str, Path] = {}
        self._lock = threading.Lock()
        self._scan_done = threading.Event()

    def start_scan(self, root: Path) -> None:
        """Kick off a background scan of `root` for tagged audio files."""
        threading.Thread(target=self._scan, args=(root,), daemon=True).start()

    def _scan(self, root: Path) -> None:
        try:
            if not root.exists():
                return
            # Walk with followlinks=False so a symlinked sibling folder
            # inside the music library can't trap us in a cycle.
            for dirpath, _dirs, files in os.walk(root, followlinks=False):
                for name in files:
                    path = Path(dirpath) / name
                    try:
                        if path.suffix.lower() not in _AUDIO_EXTS:
                            continue
                        tid = read_track_id(path)
                        if not tid:
                            continue
                        with self._lock:
                            existing = self._by_id.get(tid)
                            if existing is None or not existing.exists():
                                self._by_id[tid] = path
                    except Exception:
                        continue
        finally:
            self._scan_done.set()

    def add(self, track_id: str, path: Path) -> None:
        with self._lock:
            self._by_id[str(track_id)] = path

    def get(self, track_id: str) -> Optional[Path]:
        with self._lock:
            path = self._by_id.get(str(track_id))
        if path is None:
            return None
        # Files can be deleted out from under us. Drop stale entries so we
        # don't keep pointing at a ghost.
        if not path.exists():
            with self._lock:
                self._by_id.pop(str(track_id), None)
            return None
        return path

    def ids(self) -> set[str]:
        with self._lock:
            snapshot = dict(self._by_id)
        # Drop stale entries during snapshot so the set we hand out matches
        # the actual filesystem.
        stale = [tid for tid, p in snapshot.items() if not p.exists()]
        if stale:
            with self._lock:
                for tid in stale:
                    self._by_id.pop(tid, None)
            for tid in stale:
                snapshot.pop(tid, None)
        return set(snapshot.keys())
