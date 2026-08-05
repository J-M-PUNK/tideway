"""Tests for the cover-art proxy cache and its dedicated transport.

The `/api/image` proxy fronts every album/artist/playlist cover. Two
properties matter for the "covers load slow or not at all" report
(#306):

  - Cover fetches must not ride the shared audio/API `SESSION`. They
    go through a dedicated `IMAGE_SESSION` so a page full of covers
    can't starve DASH-segment / manifest / tidalapi traffic (and vice
    versa). This test pins that the endpoint calls `IMAGE_SESSION`.
  - A served cover is cached in-process, so a reload / second window /
    browser-cache eviction becomes an instant local hit instead of a
    fresh Tidal round-trip. The cache is a bounded LRU (by entry count
    and by total bytes) so it can't grow without limit.

The SSRF allowlist (https-only, no userinfo, host allowlist) is
unchanged by the cache work; the last test pins that a disallowed host
is still rejected without any upstream fetch.
"""
import pytest
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _clean_image_cache():
    import server

    with server._image_cache_lock:
        server._image_cache.clear()
        server._image_cache_bytes = 0
    yield
    with server._image_cache_lock:
        server._image_cache.clear()
        server._image_cache_bytes = 0


@pytest.fixture
def stub_auth(monkeypatch):
    import server

    monkeypatch.setattr(server, "_require_auth", lambda: None)


class _FakeResp:
    """Minimal stand-in for the streamed requests/curl-cffi response the
    proxy consumes: status, headers, raise_for_status, iter_content,
    close."""

    def __init__(self, body: bytes, content_type: str = "image/jpeg",
                 status_code: int = 200, content_length: str | None = None):
        self._body = body
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


def _counting_session(resp: _FakeResp, calls: dict):
    class _Sess:
        def get(self, url, **kwargs):
            calls["n"] = calls.get("n", 0) + 1
            return resp

    return _Sess()


_URL = "https://resources.tidal.com/images/ab/cd/ef/640x640.jpg"


# ---------------------------------------------------------------------------
# Cache primitives
# ---------------------------------------------------------------------------


def test_put_then_get_roundtrips():
    import server

    server._image_cache_put(_URL, "image/png", b"pixels")
    assert server._image_cache_get(_URL) == ("image/png", b"pixels")


def test_get_missing_returns_none():
    import server

    assert server._image_cache_get("https://images.tidal.com/nope.jpg") is None


def test_double_put_does_not_double_count_bytes():
    import server

    server._image_cache_put(_URL, "image/jpeg", b"1234")
    server._image_cache_put(_URL, "image/jpeg", b"ZZZZZZZZ")  # ignored
    with server._image_cache_lock:
        assert server._image_cache_bytes == 4
        # Original entry is kept; the second put is a no-op.
        assert server._image_cache[_URL] == ("image/jpeg", b"1234")


def test_evicts_coldest_by_entry_count(monkeypatch):
    import server

    monkeypatch.setattr(server, "_IMAGE_CACHE_MAX_ENTRIES", 2)
    server._image_cache_put("u1", "image/jpeg", b"a")
    server._image_cache_put("u2", "image/jpeg", b"b")
    server._image_cache_put("u3", "image/jpeg", b"c")  # evicts u1
    assert server._image_cache_get("u1") is None
    assert server._image_cache_get("u2") is not None
    assert server._image_cache_get("u3") is not None


def test_get_marks_recently_used(monkeypatch):
    import server

    monkeypatch.setattr(server, "_IMAGE_CACHE_MAX_ENTRIES", 2)
    server._image_cache_put("u1", "image/jpeg", b"a")
    server._image_cache_put("u2", "image/jpeg", b"b")
    server._image_cache_get("u1")  # u1 now most-recent
    server._image_cache_put("u3", "image/jpeg", b"c")  # should evict u2
    assert server._image_cache_get("u2") is None
    assert server._image_cache_get("u1") is not None


def test_evicts_by_total_bytes(monkeypatch):
    import server

    monkeypatch.setattr(server, "_IMAGE_CACHE_MAX_BYTES", 10)
    server._image_cache_put("u1", "image/jpeg", b"x" * 6)
    server._image_cache_put("u2", "image/jpeg", b"y" * 6)  # 12 > 10, evict u1
    assert server._image_cache_get("u1") is None
    assert server._image_cache_get("u2") is not None
    with server._image_cache_lock:
        assert server._image_cache_bytes == 6


# ---------------------------------------------------------------------------
# Endpoint behavior
# ---------------------------------------------------------------------------


def test_endpoint_uses_image_session_and_caches(stub_auth, monkeypatch):
    import server

    calls: dict = {}
    resp = _FakeResp(b"JPEGBYTES", content_type="image/jpeg")
    monkeypatch.setattr(server, "IMAGE_SESSION", _counting_session(resp, calls))

    r1 = server.image_proxy(_URL)
    assert r1.body == b"JPEGBYTES"
    assert r1.media_type == "image/jpeg"
    # Second request is served from cache — no second upstream fetch.
    r2 = server.image_proxy(_URL)
    assert r2.body == b"JPEGBYTES"
    assert calls["n"] == 1


def test_endpoint_does_not_touch_shared_session(stub_auth, monkeypatch):
    """Regression guard: covers must never draw from the audio/API
    SESSION. If the proxy ever reaches for it, this fails loudly."""
    import server

    calls: dict = {}
    resp = _FakeResp(b"IMG")
    monkeypatch.setattr(server, "IMAGE_SESSION", _counting_session(resp, calls))

    class _Boom:
        def get(self, *a, **k):
            raise AssertionError("image proxy used the shared SESSION")

    monkeypatch.setattr(server, "SESSION", _Boom())
    server.image_proxy(_URL)
    assert calls["n"] == 1


def test_disallowed_host_rejected_without_fetch(stub_auth, monkeypatch):
    import server

    calls: dict = {}
    resp = _FakeResp(b"IMG")
    monkeypatch.setattr(server, "IMAGE_SESSION", _counting_session(resp, calls))

    with pytest.raises(HTTPException) as exc:
        server.image_proxy("https://evil.example.com/internal")
    assert exc.value.status_code == 403
    assert calls.get("n", 0) == 0


def test_oversized_body_served_once_but_not_cached(stub_auth, monkeypatch):
    import server

    monkeypatch.setattr(server, "MAX_IMAGE_BYTES", 8)
    calls: dict = {}
    resp = _FakeResp(b"x" * 64)  # exceeds the 8-byte ceiling
    monkeypatch.setattr(server, "IMAGE_SESSION", _counting_session(resp, calls))

    server.image_proxy(_URL)
    # Not stored: a truncated/oversized body must not poison the cache.
    assert server._image_cache_get(_URL) is None
