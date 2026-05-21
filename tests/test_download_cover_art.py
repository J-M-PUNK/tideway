"""Tests for the ``_maybe_write_album_cover`` helper.

The helper drops a ``cover.jpg`` next to a freshly tagged track when
the track lives in an album / playlist subfolder of the configured
output directory. Plex, Roon, mpd, foobar2000 and every other music
manager I've checked all pick up ``cover.jpg`` automatically, so
this is the right shape for the feature request.

Invariants pinned here:

1. A track inside a subfolder gets a ``cover.jpg`` written.
2. A track at the output_dir root gets nothing (lone-track downloads
   shouldn't pollute the root).
3. An existing ``cover.jpg`` is never clobbered — covers users dropped
   in by hand stay, and the per-track rewrite cost stays zero.
4. Empty / None cover bytes are a no-op (no zero-byte placeholder
   file).
5. Paths that resolve outside the output_dir are refused (defensive;
   guards against a malicious template or symlink trick).
"""
from __future__ import annotations

import os
from pathlib import Path

from app.downloader import _maybe_write_album_cover


def test_subfolder_track_writes_cover_jpg(tmp_path: Path) -> None:
    album = tmp_path / "AlbumX"
    album.mkdir()
    track = album / "01.flac"
    track.touch()
    _maybe_write_album_cover(track, tmp_path, b"JPEG-BYTES")
    cover = album / "cover.jpg"
    assert cover.exists()
    assert cover.read_bytes() == b"JPEG-BYTES"


def test_lone_track_at_output_root_does_not_write(tmp_path: Path) -> None:
    track = tmp_path / "OnlyTrack.flac"
    track.touch()
    _maybe_write_album_cover(track, tmp_path, b"JPEG")
    assert not (tmp_path / "cover.jpg").exists()


def test_existing_cover_is_preserved(tmp_path: Path) -> None:
    album = tmp_path / "AlbumY"
    album.mkdir()
    (album / "cover.jpg").write_bytes(b"USER-DROPPED-COVER")
    track = album / "01.flac"
    track.touch()
    _maybe_write_album_cover(track, tmp_path, b"TIDAL-COVER")
    # The user's hand-placed cover takes priority and the helper
    # silently no-ops the second-track-of-the-album rewrite too.
    assert (album / "cover.jpg").read_bytes() == b"USER-DROPPED-COVER"


def test_no_cover_bytes_is_a_noop(tmp_path: Path) -> None:
    album = tmp_path / "AlbumZ"
    album.mkdir()
    track = album / "01.flac"
    track.touch()
    _maybe_write_album_cover(track, tmp_path, None)
    _maybe_write_album_cover(track, tmp_path, b"")
    assert not (album / "cover.jpg").exists()


def test_nested_subfolder_writes_cover_too(tmp_path: Path) -> None:
    # Custom template like {artist}/{album}/{title} produces two
    # levels of nesting. Cover should still land next to the track.
    nested = tmp_path / "Artist" / "Album"
    nested.mkdir(parents=True)
    track = nested / "01.flac"
    track.touch()
    _maybe_write_album_cover(track, tmp_path, b"JPEG-BYTES")
    assert (nested / "cover.jpg").exists()


def test_path_outside_output_dir_is_refused(tmp_path: Path) -> None:
    # Constructing the scenario: an album folder that's a sibling of
    # tmp_path (so not under it). The helper should refuse to write
    # cover.jpg into a directory that isn't under the configured
    # output root.
    sibling = tmp_path.parent / f"{tmp_path.name}-sibling"
    sibling.mkdir(exist_ok=True)
    try:
        track = sibling / "01.flac"
        track.touch()
        _maybe_write_album_cover(track, tmp_path, b"JPEG")
        assert not (sibling / "cover.jpg").exists()
    finally:
        # Clean up the sibling we just made; tmp_path fixture only
        # cleans up tmp_path itself.
        for child in sibling.iterdir():
            try:
                child.unlink()
            except OSError:
                pass
        try:
            sibling.rmdir()
        except OSError:
            pass


def test_helper_runs_idempotently_across_tracks(tmp_path: Path) -> None:
    # Simulate the real flow: 12 tracks finish tagging one after the
    # other. cover.jpg should be written by the first one and the rest
    # are no-ops. No exception, no file mtime churn.
    album = tmp_path / "AlbumW"
    album.mkdir()
    for i in range(1, 13):
        (album / f"{i:02d}.flac").touch()
    _maybe_write_album_cover(album / "01.flac", tmp_path, b"JPEG-V1")
    first_mtime = (album / "cover.jpg").stat().st_mtime_ns
    # Tracks 2..12 — cover.jpg already exists, helper should skip.
    for i in range(2, 13):
        _maybe_write_album_cover(album / f"{i:02d}.flac", tmp_path, b"JPEG-V2")
    # Same bytes, same mtime as the first write.
    assert (album / "cover.jpg").read_bytes() == b"JPEG-V1"
    assert (album / "cover.jpg").stat().st_mtime_ns == first_mtime
