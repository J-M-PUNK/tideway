"""Tests for ReplayGain tag extraction from local files.

Streaming tracks pick up the four ReplayGain values from tidalapi's
Stream object; downloaded tracks need the same values pulled out of
mutagen tags. These tests pin the shape of `_read_local_replaygain`
across the three tag formats Tideway-downloaded files might land in
(FLAC / Vorbis comments, ID3 TXXX frames, MP4 iTunes-style atoms).
"""
from __future__ import annotations

from typing import Optional

import pytest

from app.audio.player import _read_local_replaygain


class _DictTags:
    """Stand-in for FLAC/Vorbis/MP4 tag containers. Mutagen exposes a
    dict-like API across all three; the resolver only needs case-
    insensitive get(). Stores values as the lists mutagen actually
    hands back."""

    def __init__(self, **kwargs):
        # Normalize keys to lowercase to mirror what mutagen does for
        # Vorbis comments; the resolver handles both cases.
        self._data: dict[str, list[str]] = {
            k.lower(): [str(v)] for k, v in kwargs.items()
        }

    def get(self, key: str) -> Optional[list[str]]:
        return self._data.get(key.lower())

    def __getitem__(self, key: str):
        v = self.get(key)
        if v is None:
            raise KeyError(key)
        return v


class _StubMutagenFile:
    """Just enough of mutagen.File's surface for the resolver."""

    def __init__(self, tags: object):
        self.tags = tags
        self.info = object()  # caller doesn't read this in our path


class _Txxx:
    """Stand-in for an ID3 TXXX frame."""

    def __init__(self, desc: str, text: str):
        self.desc = desc
        self.text = [text]


class _Id3Tags:
    """Stand-in for an ID3 tag container — exposes `getall("TXXX")`
    which is the API mutagen.id3.ID3 uses."""

    def __init__(self, frames: list[_Txxx]):
        self._frames = frames

    def get(self, _key: str) -> None:
        # ID3 doesn't store ReplayGain under dict-style keys; the
        # resolver should fall through to the getall("TXXX") path.
        return None

    def getall(self, key: str) -> list[_Txxx]:
        if key != "TXXX":
            return []
        return list(self._frames)


def test_flac_vorbis_style_tags_parsed():
    tags = _DictTags(
        REPLAYGAIN_TRACK_GAIN="-3.45 dB",
        REPLAYGAIN_TRACK_PEAK="0.876543",
        REPLAYGAIN_ALBUM_GAIN="-4.12 dB",
        REPLAYGAIN_ALBUM_PEAK="0.998765",
    )
    rg = _read_local_replaygain(_StubMutagenFile(tags))
    assert rg.track_gain_db == pytest.approx(-3.45)
    assert rg.track_peak == pytest.approx(0.876543)
    assert rg.album_gain_db == pytest.approx(-4.12)
    assert rg.album_peak == pytest.approx(0.998765)


def test_lowercase_keys_work_too():
    """Some taggers write Vorbis comments lowercase. The resolver
    tries both cases."""
    tags = _DictTags(
        replaygain_track_gain="-2.0 dB",
        replaygain_track_peak="0.5",
    )
    rg = _read_local_replaygain(_StubMutagenFile(tags))
    assert rg.track_gain_db == pytest.approx(-2.0)
    assert rg.track_peak == pytest.approx(0.5)
    assert rg.album_gain_db is None
    assert rg.album_peak is None


def test_gain_value_without_db_suffix():
    """Some taggers omit the " dB" suffix and write the bare number."""
    tags = _DictTags(REPLAYGAIN_TRACK_GAIN="-3.45")
    rg = _read_local_replaygain(_StubMutagenFile(tags))
    assert rg.track_gain_db == pytest.approx(-3.45)


def test_id3_txxx_frames_parsed():
    """MP3 stores ReplayGain in TXXX frames keyed by description.
    The resolver scans them when dict-style lookup misses."""
    frames = [
        _Txxx("replaygain_track_gain", "-5.0 dB"),
        _Txxx("REPLAYGAIN_TRACK_PEAK", "0.99"),
        _Txxx("replaygain_album_gain", "-6.0 dB"),
        _Txxx("replaygain_album_peak", "1.02"),
        _Txxx("unrelated_tag", "ignored"),
    ]
    rg = _read_local_replaygain(_StubMutagenFile(_Id3Tags(frames)))
    assert rg.track_gain_db == pytest.approx(-5.0)
    assert rg.track_peak == pytest.approx(0.99)
    assert rg.album_gain_db == pytest.approx(-6.0)
    assert rg.album_peak == pytest.approx(1.02)


def test_missing_tags_returns_all_none():
    rg = _read_local_replaygain(_StubMutagenFile(_DictTags()))
    assert rg.track_gain_db is None
    assert rg.track_peak is None
    assert rg.album_gain_db is None
    assert rg.album_peak is None


def test_garbage_gain_value_returns_none():
    """A tag value that doesn't parse as a number should fall back to
    None rather than blow up."""
    tags = _DictTags(REPLAYGAIN_TRACK_GAIN="not a number")
    rg = _read_local_replaygain(_StubMutagenFile(tags))
    assert rg.track_gain_db is None


def test_no_tags_object():
    """Mutagen returns None for `tags` on some malformed files. The
    resolver must not crash."""
    rg = _read_local_replaygain(_StubMutagenFile(None))
    assert rg.track_gain_db is None
    assert rg.album_gain_db is None
