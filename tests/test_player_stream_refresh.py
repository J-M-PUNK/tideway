"""PCMPlayer's side of mid-track stream recovery: the refresher it gives
each SegmentReader, and the cache entry it drops so the refresh really
asks TIDAL again."""
from __future__ import annotations

import pytest

import app.audio.player as player_mod
from app.audio.manifest_cache import ManifestCache
from app.audio.player import PCMPlayer, StreamInfo


def _player() -> PCMPlayer:
    return PCMPlayer(lambda: None)


def test_refresher_resolves_fresh_without_touching_now_playing(monkeypatch):
    p = _player()
    calls = []

    def _fake_resolve(track_id, quality, *, set_current_meta=True, fresh=False):
        calls.append((track_id, quality, set_current_meta, fresh))
        return ["http://cdn/init?new", "http://cdn/1?new"], 1.0, StreamInfo(
            source="stream"), {}, None

    monkeypatch.setattr(p, "_resolve_source", _fake_resolve)
    refresh = p._url_refresher("101", "hi_res_lossless")
    assert refresh() == ["http://cdn/init?new", "http://cdn/1?new"]
    assert calls == [("101", "hi_res_lossless", False, True)]


def test_refresher_none_without_a_track():
    assert _player()._url_refresher(None, "high_lossless") is None


def test_refresher_refuses_a_local_file(monkeypatch):
    p = _player()
    monkeypatch.setattr(
        p, "_resolve_source",
        lambda *a, **k: ("/music/a.flac", 1.0, StreamInfo(source="local"), {}, None),
    )
    with pytest.raises(RuntimeError, match="no longer resolves to a stream"):
        p._url_refresher("101", None)()


def test_manifest_cache_invalidate():
    cache = ManifestCache()
    key = ("101", "high_lossless")
    cache.store(key, ["u0", "u1"], 1.0, None, track_meta=None)
    assert cache.lookup(key) is not None
    cache.invalidate(key)
    assert cache.lookup(key) is None
    cache.invalidate(key)  # idempotent


def test_seek_reader_carries_refresher_and_offset(monkeypatch):
    """A seek opens the stream at media segment idx; the reader has to
    know that offset to map refreshed links onto its own segments."""
    p = _player()
    p._source_urls = ["init"] + [f"seg{i}" for i in range(1, 11)]
    p._current_duration_ms = 100_000
    p._current_track_id = "101"
    p._source_quality = "high_lossless"
    built = []

    class _Reader:
        def __init__(self, urls, **kw):
            built.append((list(urls), kw))

    monkeypatch.setattr(player_mod, "SegmentReader", _Reader)
    _src, start = p._build_source_at(55.0)
    urls, kw = built[0]
    assert urls == ["init"] + [f"seg{i}" for i in range(6, 11)]
    assert kw["media_offset"] == 5
    assert callable(kw["refresh_urls"])
    assert start == pytest.approx(50.0)
