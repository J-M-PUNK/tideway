"""PCMPlayer's source hooks for the DLNA manager: the capped re-resolve
a format-limited renderer (Sonos) relies on, and the preload record
handed over at connect."""
from __future__ import annotations

from app.audio.player import PCMPlayer, StreamInfo


def _player() -> PCMPlayer:
    return PCMPlayer(lambda: None)


def test_resolve_stream_urls_returns_urls_and_signature(monkeypatch):
    p = _player()
    calls = []

    def _fake_resolve(track_id, quality, *, set_current_meta=True):
        calls.append((track_id, quality, set_current_meta))
        info = StreamInfo(
            source="stream", codec="FLAC", bit_depth=16,
            sample_rate_hz=44100, audio_quality="LOSSLESS",
        )
        return ["http://cdn/init", "http://cdn/1"], 200.0, info, {}, None

    monkeypatch.setattr(p, "_resolve_source", _fake_resolve)
    urls, sig = p.resolve_stream_urls("101", "high_lossless")
    assert urls == ["http://cdn/init", "http://cdn/1"]
    assert sig == "FLAC|16|44100|LOSSLESS"
    # Must not overwrite the now-playing metadata of the loaded track.
    assert calls == [("101", "high_lossless", False)]


def test_resolve_stream_urls_none_for_local_file(monkeypatch):
    p = _player()
    monkeypatch.setattr(
        p, "_resolve_source",
        lambda *a, **k: ("/music/a.flac", 1.0, StreamInfo(source="local"), {}, None),
    )
    assert p.resolve_stream_urls("101", "high_lossless") is None
