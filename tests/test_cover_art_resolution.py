"""Cover-art resolution + fallback (issue #204).

Downloads embedded a fixed 640px cover, well below the 1280 and
3000x3000 "origin" sizes Tidal publishes. These tests pin the new
behavior: the chosen resolution is requested, the fetch walks down to
a smaller size when an album doesn't publish the chosen one, and the
setting round-trips through the API with validation.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import metadata
from app.metadata import (
    DEFAULT_COVER_RESOLUTION,
    _cover_size_ladder,
    fetch_cover_art,
)


# ---------------------------------------------------------------------------
# Resolution ladder
# ---------------------------------------------------------------------------


def test_ladder_origin_tries_everything_largest_first():
    assert _cover_size_ladder("origin") == ["origin", 1280, 640, 320]


def test_ladder_caps_at_chosen_size():
    assert _cover_size_ladder("1280") == [1280, 640, 320]
    assert _cover_size_ladder("640") == [640, 320]


@pytest.mark.parametrize("bad", ["junk", "", None, "99999"])
def test_ladder_unknown_resolution_uses_default(bad):
    # A corrupt setting must not leave a download with no cover —
    # fall back to the default ladder, never empty.
    assert _cover_size_ladder(bad) == _cover_size_ladder(DEFAULT_COVER_RESOLUTION)
    assert _cover_size_ladder(bad)  # non-empty


# ---------------------------------------------------------------------------
# fetch_cover_art fallback
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cover_cache():
    """The per-album cover cache is module-level; clear it between
    tests so a cached result from one test can't mask another."""
    metadata._cover_cache.clear()
    yield
    metadata._cover_cache.clear()


def test_none_album_returns_none():
    assert fetch_cover_art(None, "origin") is None


def test_uses_first_available_size(monkeypatch):
    tried = []

    def fake_fetch(album_obj, size):
        tried.append(size)
        return b"IMG" if size == 1280 else None

    monkeypatch.setattr(metadata, "_fetch_cover_at", fake_fetch)
    # origin requested but only 1280 published → walks origin, then 1280.
    out = fetch_cover_art(SimpleNamespace(), "origin")
    assert out == b"IMG"
    assert tried == ["origin", 1280]


def test_returns_none_when_no_size_available(monkeypatch):
    monkeypatch.setattr(metadata, "_fetch_cover_at", lambda a, s: None)
    assert fetch_cover_art(SimpleNamespace(), "1280") is None


def test_chosen_size_requested_first(monkeypatch):
    tried = []

    def fake_fetch(album_obj, size):
        tried.append(size)
        return b"IMG"

    monkeypatch.setattr(metadata, "_fetch_cover_at", fake_fetch)
    fetch_cover_art(SimpleNamespace(), "640")
    assert tried[0] == 640  # never requests larger than the user asked


# ---------------------------------------------------------------------------
# Per-album cache
# ---------------------------------------------------------------------------


def test_album_cover_fetched_once_across_tracks(monkeypatch):
    """An album's tracks all tag the same cover — the network fetch
    must happen once, not once per track."""
    calls = {"n": 0}

    def fake_fetch(album_obj, size):
        calls["n"] += 1
        return b"COVER"

    monkeypatch.setattr(metadata, "_fetch_cover_at", fake_fetch)
    album = SimpleNamespace(id=4567)
    first = fetch_cover_art(album, "1280")
    second = fetch_cover_art(album, "1280")
    assert first == second == b"COVER"
    assert calls["n"] == 1  # cached on the second call


def test_cover_cache_keyed_by_resolution(monkeypatch):
    # A different resolution is a different cache entry — changing the
    # setting must not serve a stale size.
    seen = []

    def fake_fetch(album_obj, size):
        seen.append(size)
        return b"X"

    monkeypatch.setattr(metadata, "_fetch_cover_at", fake_fetch)
    album = SimpleNamespace(id=99)
    fetch_cover_art(album, "640")
    fetch_cover_art(album, "origin")
    # Two distinct resolutions → two fetches (640 first, then origin).
    assert 640 in seen and "origin" in seen


def test_cover_cache_caches_misses(monkeypatch):
    """A genuinely cover-less album shouldn't re-walk the ladder for
    every track — a None result is cached too."""
    calls = {"n": 0}

    def fake_fetch(album_obj, size):
        calls["n"] += 1
        return None

    monkeypatch.setattr(metadata, "_fetch_cover_at", fake_fetch)
    album = SimpleNamespace(id=1)
    assert fetch_cover_art(album, "640") is None
    assert fetch_cover_art(album, "640") is None
    # First call walked [640, 320] (2); second served the cached None.
    assert calls["n"] == 2


def test_cover_cache_evicts_oldest(monkeypatch):
    monkeypatch.setattr(metadata, "_fetch_cover_at", lambda a, s: b"C")
    for i in range(metadata._COVER_CACHE_MAX + 3):
        fetch_cover_art(SimpleNamespace(id=i), "1280")
    assert len(metadata._cover_cache) == metadata._COVER_CACHE_MAX


# ---------------------------------------------------------------------------
# _fetch_cover_at host + size guards (integration with a fake session)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, body: bytes, ok: bool = True, length=None):
        self._body = body
        self.ok = ok
        self.headers = {"Content-Length": str(length if length is not None else len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size=65536):
        yield self._body


def _album_with_url(url: str):
    return SimpleNamespace(image=lambda size: url)


def test_fetch_at_rejects_non_tidal_host(monkeypatch):
    monkeypatch.setattr(
        metadata.SESSION, "get", lambda *a, **k: _FakeResp(b"X"), raising=False
    )
    album = _album_with_url("https://evil.example.com/cover.jpg")
    assert metadata._fetch_cover_at(album, 1280) is None


def test_fetch_at_accepts_tidal_host(monkeypatch):
    monkeypatch.setattr(
        metadata.SESSION,
        "get",
        lambda *a, **k: _FakeResp(b"JPEGBYTES"),
        raising=False,
    )
    album = _album_with_url("https://resources.tidal.com/images/x/1280x1280.jpg")
    assert metadata._fetch_cover_at(album, 1280) == b"JPEGBYTES"


def test_fetch_at_rejects_oversize_declared(monkeypatch):
    huge = metadata._MAX_COVER_BYTES + 1
    monkeypatch.setattr(
        metadata.SESSION,
        "get",
        lambda *a, **k: _FakeResp(b"X", length=huge),
        raising=False,
    )
    album = _album_with_url("https://resources.tidal.com/images/x/origin.jpg")
    assert metadata._fetch_cover_at(album, "origin") is None


# ---------------------------------------------------------------------------
# Settings endpoint
# ---------------------------------------------------------------------------


def test_default_resolution_is_1280():
    from app.settings import Settings

    assert Settings().cover_art_resolution == "1280"
