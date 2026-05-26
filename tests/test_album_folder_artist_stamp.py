"""Tests for the album-folder-artist freeze step.

User report: with `album_folder_includes_artist` on, some tracks of
one album landed in a folder named after the track's primary artist
instead of the canonical album artist. The root cause was the seam
between two ways the same track can arrive at `_populate_item_from_track`:

  * As part of an album enqueue (`kind=album` → `album_obj` is the
    full parent Album with `.artist` set to the canonical credit).
  * As a standalone track (single-track download, OR a track restored
    from a pending snapshot after a crash). Here `album_obj` is
    `track.album`, which tidalapi populates from the track's primary
    artist rather than the album's — see `tidalapi.media.Media.parse`.

`_stamp_album_folder_artist` freezes the canonical value onto the
item at album-enqueue time, and a side-channel dict carries it across
the restore() re-fetch so a resumed item lands in the same folder as
its album-mates.
"""
from __future__ import annotations

import threading
from typing import Optional

from app.downloader import DownloadItem, Downloader


class _Stub:
    """Just enough of a Downloader to exercise the stamp helper.

    Avoids the worker-thread spin-up of the real constructor — only
    the locks + overrides dict matter for this code path.
    """

    def __init__(self) -> None:
        self._restore_overrides: dict[str, str] = {}
        self._restore_overrides_lock = threading.Lock()


# Bind the method off the real class so we test the production code,
# not a copy. `Downloader._stamp_album_folder_artist` only reads
# `self._restore_overrides` + lock — the rest of Downloader's state
# isn't touched, so the stub is enough.
_stamp = Downloader._stamp_album_folder_artist


class _Track:
    def __init__(self, *, tid: Optional[int], artist_name: str = "") -> None:
        self.id = tid
        self.artist = type("_A", (), {"name": artist_name})()


class _Album:
    def __init__(self, *, artist_name: Optional[str]) -> None:
        if artist_name is None:
            self.artist = None
        else:
            self.artist = type("_A", (), {"name": artist_name})()


def test_album_enqueue_freezes_canonical_artist_on_item():
    """Album-enqueue path: the parent album's artist name is frozen
    onto the item, regardless of what the track's own primary artist
    is. That's the value `_build_path` reaches for when the folder
    toggle is on."""
    stub = _Stub()
    item = DownloadItem(item_id="i", url="")
    track = _Track(tid=1, artist_name="Drake")  # featured contributor
    album = _Album(artist_name="DJ Khaled")  # canonical credit

    _stamp(stub, item, track, album, is_album_enqueue=True)

    assert item.album_folder_artist == "DJ Khaled"


def test_single_track_enqueue_does_not_freeze():
    """Single-track and playlist enqueues leave the field empty so
    `_build_path` falls back to the per-track logic. The canonical
    album artist isn't reliable here — tidalapi gives us the track's
    primary artist as `track.album.artist`, which is exactly the
    value we'd otherwise be "freezing", defeating the point."""
    stub = _Stub()
    item = DownloadItem(item_id="i", url="")
    track = _Track(tid=1, artist_name="Drake")
    # Simulate tidalapi's quirk where track.album.artist == track.artist.
    album = _Album(artist_name="Drake")

    _stamp(stub, item, track, album, is_album_enqueue=False)

    assert item.album_folder_artist == ""


def test_restore_override_wins_over_album_obj():
    """The motivating case: a track restored from pending state after
    a crash. submit() goes through the kind=track URL path so the
    `album_obj` it builds is `track.album` (where tidalapi already
    smuggled the track's primary artist in as the "album" artist).
    The restore override carries the CANONICAL value from the original
    album enqueue, so it must win."""
    stub = _Stub()
    stub._restore_overrides["42"] = "DJ Khaled"
    item = DownloadItem(item_id="i", url="")
    track = _Track(tid=42, artist_name="Drake")
    # tidalapi-style track.album where artist mirrors track.artist:
    album = _Album(artist_name="Drake")

    # is_album_enqueue=False mirrors the kind=track restore path.
    _stamp(stub, item, track, album, is_album_enqueue=False)

    assert item.album_folder_artist == "DJ Khaled"
    # Override consumed — a second call for a different item with the
    # same track id (shouldn't happen in practice, but the dict-pop
    # semantics matter) gets no override.
    assert "42" not in stub._restore_overrides


def test_missing_track_id_does_not_crash():
    """Defence: tidalapi has surprised us with `None`-id placeholders
    in the past (404s during retry). The stamp helper should silently
    skip the override lookup rather than throw."""
    stub = _Stub()
    item = DownloadItem(item_id="i", url="")
    track = _Track(tid=None)
    album = _Album(artist_name="DJ Khaled")

    _stamp(stub, item, track, album, is_album_enqueue=True)

    # Album-enqueue path still freezes from album_obj.
    assert item.album_folder_artist == "DJ Khaled"


def test_album_obj_without_artist_leaves_field_empty():
    """If the parent album's `.artist` is None (rare — corrupted or
    pre-released entries), we shouldn't crash. The field stays empty
    and `_build_path` falls back to `album_artist or artist`."""
    stub = _Stub()
    item = DownloadItem(item_id="i", url="")
    track = _Track(tid=1, artist_name="Drake")
    album = _Album(artist_name=None)

    _stamp(stub, item, track, album, is_album_enqueue=True)

    assert item.album_folder_artist == ""
