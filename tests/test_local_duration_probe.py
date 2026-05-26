"""Tests for the local-file duration probe + StreamInfo extraction.

User report: clicking the progress bar to scrub through a locally-
downloaded track did nothing. Root cause: ``_resolve_source`` was
passing ``duration_s=None`` to the player for local files, which left
``_current_duration_ms=0``, which made ``PCMPlayer.seek`` bail at its
``duration_ms <= 0`` guard. The fix is to read ``info.length`` (from
mutagen) inside ``_probe_local_stream_info`` and hand it back to the
resolver. These tests pin that contract so the seek path can't go
silent on local files again.
"""
from __future__ import annotations

from typing import Optional

from app.audio.player import _probe_local_stream_info


class _Info:
    """Stand-in for mutagen.flac.StreamInfo / mp3.StreamInfo / etc."""

    def __init__(
        self,
        *,
        length: Optional[float] = None,
        bits_per_sample: Optional[int] = None,
        sample_rate: Optional[int] = None,
        mime: Optional[list[str]] = None,
    ) -> None:
        self.length = length
        self.bits_per_sample = bits_per_sample
        self.sample_rate = sample_rate
        self.mime = mime or []


class _Mut:
    def __init__(self, info: Optional[_Info]):
        self.info = info
        self.tags = None  # ReplayGain path returns all None — fine.

    def get(self, _key: str):
        return None


def _patch(monkeypatch, mut: Optional[_Mut]) -> None:
    """Replace ``mutagen.File`` so the probe doesn't need a real file."""

    def fake_File(_path):  # noqa: N802 - mirrors mutagen.File name
        return mut

    import mutagen

    monkeypatch.setattr(mutagen, "File", fake_File)


def test_flac_length_returned_for_seek(monkeypatch):
    """The motivating case: a 3-minute FLAC carries info.length=180.0,
    the probe returns it, the resolver passes 180000 ms to the player,
    PCMPlayer.seek runs through its math instead of bailing."""
    _patch(
        monkeypatch,
        _Mut(_Info(length=180.0, bits_per_sample=16, sample_rate=44100, mime=["audio/flac"])),
    )
    info, duration_s = _probe_local_stream_info("/tmp/whatever.flac")
    assert info is not None
    assert info.source == "local"
    assert info.codec == "flac"
    assert info.bit_depth == 16
    assert info.sample_rate_hz == 44100
    assert duration_s == 180.0


def test_missing_length_returns_none_without_crashing(monkeypatch):
    """Some legacy / corrupt files have no info.length. The probe must
    degrade to ``duration_s=None`` (rather than raise), so the resolver
    can still play the file even though seek won't work on it."""
    _patch(
        monkeypatch,
        _Mut(_Info(length=None, bits_per_sample=16, sample_rate=44100)),
    )
    info, duration_s = _probe_local_stream_info("/tmp/odd.flac")
    assert info is not None
    assert duration_s is None


def test_garbage_length_returns_none(monkeypatch):
    """A non-numeric length (mutagen normally won't produce this, but
    cheap defence) gets coerced to None, not propagated as a string
    that the player's int(duration_s * 1000) would explode on."""
    _patch(monkeypatch, _Mut(_Info(length="banana", sample_rate=44100)))  # type: ignore[arg-type]
    info, duration_s = _probe_local_stream_info("/tmp/garbage.flac")
    assert info is not None
    assert duration_s is None


def test_unreadable_file_returns_double_none(monkeypatch):
    """mutagen.File can return None for unreadable inputs. The probe
    forwards that as (None, None) so the resolver falls back to the
    default StreamInfo and the player still tries to play."""
    _patch(monkeypatch, None)
    info, duration_s = _probe_local_stream_info("/tmp/missing.flac")
    assert info is None
    assert duration_s is None
