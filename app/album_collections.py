"""Local-only album collections.

Tidal has no API for organizing favorite albums into user-defined
groups (playlist folders exist, album folders/tags do not — see #243).
This module is Tideway's own store for that: named collections of
albums the user assembles locally. Nothing here talks to Tidal; a
collection is a name plus a list of album snapshots, persisted to
`user_data_dir/album_collections.json`.

A "collection" doubles as both the folder and the tag the reporter
asked for — an album can live in any number of collections, so
tagging an album "Vinyl" and filing it under "Chill" is the same
operation twice.

Album snapshots are stored, not just IDs: the frontend already holds
the full album object when the user adds it, so we keep enough of it
(id, name, cover, artists, year, …) to render a card and open the
album without a round-trip to Tidal. That also means collections keep
working offline. The album ID is the identity for dedupe and removal.

Writes are atomic (tmp file + os.replace under a lock) so a crash
mid-write can't corrupt the file, mirroring now_playing_state.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from typing import Any, Optional

from app.paths import user_data_dir

log = logging.getLogger(__name__)

_FILE = user_data_dir() / "album_collections.json"
_lock = threading.RLock()

# Fields we keep from the album object the frontend sends. Bounded on
# purpose: enough to render a MediaCard and navigate to /album/<id>,
# without storing whatever else happens to ride along on the payload.
_ALBUM_FIELDS = (
    "id",
    "name",
    "cover",
    "artists",
    "year",
    "num_tracks",
    "duration",
    "explicit",
    "available",
    "album_type",
)


def _normalize_album(album: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Keep only the known display fields, and require an id. Returns
    None when the payload has no usable album id — the caller treats
    that as a bad request rather than storing a card that can't be
    opened or removed."""
    if not isinstance(album, dict):
        return None
    aid = album.get("id")
    if aid is None or str(aid) == "":
        return None
    out: dict[str, Any] = {"kind": "album"}
    for f in _ALBUM_FIELDS:
        if f in album:
            out[f] = album[f]
    out["id"] = str(aid)
    return out


def _read() -> dict[str, Any]:
    """Load the whole store. Missing / corrupt file reads as empty —
    a broken file shouldn't wipe the app, and there's nothing to
    recover from a half-written collections list anyway."""
    if not _FILE.exists():
        return {"collections": []}
    try:
        with open(_FILE, encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        log.warning("album collections read failed; starting empty")
        return {"collections": []}
    if not isinstance(obj, dict) or not isinstance(obj.get("collections"), list):
        return {"collections": []}
    return obj


def _write(data: dict[str, Any]) -> None:
    target = _FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".album_collections.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, target)
    except Exception:
        log.exception("album collections write failed")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _summary(c: dict[str, Any]) -> dict[str, Any]:
    """List-view shape: id, name, count, and up to four covers for a
    stacked folder thumbnail. Kept separate from the full detail shape
    so the library grid doesn't ship every album in every collection."""
    albums = c.get("albums", [])
    covers = [a.get("cover") for a in albums if a.get("cover")][:4]
    return {
        "id": c["id"],
        "name": c["name"],
        "count": len(albums),
        "covers": covers,
        "created_at": c.get("created_at", 0),
    }


def list_collections() -> list[dict[str, Any]]:
    """All collections as summaries, newest first."""
    with _lock:
        data = _read()
    out = [_summary(c) for c in data["collections"]]
    out.sort(key=lambda s: s.get("created_at", 0), reverse=True)
    return out


def get_collection(collection_id: str) -> Optional[dict[str, Any]]:
    """Full collection (id, name, albums) or None if it doesn't exist."""
    with _lock:
        data = _read()
    for c in data["collections"]:
        if c["id"] == collection_id:
            return {
                "id": c["id"],
                "name": c["name"],
                "created_at": c.get("created_at", 0),
                "albums": list(c.get("albums", [])),
            }
    return None


def create_collection(name: str) -> dict[str, Any]:
    """Create a collection and return its summary. Name is trimmed;
    the caller is expected to reject an empty name before calling."""
    cid = "col_" + uuid.uuid4().hex[:12]
    entry = {
        "id": cid,
        "name": name.strip(),
        "created_at": time.time(),
        "albums": [],
    }
    with _lock:
        data = _read()
        data["collections"].append(entry)
        _write(data)
    return _summary(entry)


def rename_collection(collection_id: str, name: str) -> bool:
    with _lock:
        data = _read()
        for c in data["collections"]:
            if c["id"] == collection_id:
                c["name"] = name.strip()
                _write(data)
                return True
    return False


def delete_collection(collection_id: str) -> bool:
    with _lock:
        data = _read()
        before = len(data["collections"])
        data["collections"] = [
            c for c in data["collections"] if c["id"] != collection_id
        ]
        if len(data["collections"]) == before:
            return False
        _write(data)
        return True


def add_album(collection_id: str, album: dict[str, Any]) -> Optional[bool]:
    """Add an album to a collection.

    Returns True on success, False when the album is already in the
    collection (idempotent no-op), and None when the collection doesn't
    exist or the album payload has no usable id — the endpoint maps
    those to 404 / 400 respectively.
    """
    snap = _normalize_album(album)
    if snap is None:
        return None
    with _lock:
        data = _read()
        for c in data["collections"]:
            if c["id"] == collection_id:
                albums = c.setdefault("albums", [])
                if any(a.get("id") == snap["id"] for a in albums):
                    return False
                albums.append(snap)
                _write(data)
                return True
    return None


def remove_album(collection_id: str, album_id: str) -> bool:
    with _lock:
        data = _read()
        for c in data["collections"]:
            if c["id"] == collection_id:
                albums = c.get("albums", [])
                kept = [a for a in albums if str(a.get("id")) != str(album_id)]
                if len(kept) == len(albums):
                    return False
                c["albums"] = kept
                _write(data)
                return True
    return False
