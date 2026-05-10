"""Tests for the editorial-page TTL cache.

The /api/page/{name} and /api/page/resolve endpoints each block on a
synchronous Tidal API round-trip (200-800ms typical). The server-side
TTL cache means a repeat call within 60s skips the round-trip entirely.
These tests pin the contract:

  - Cache hit: a second request inside the TTL doesn't invoke the
    loader.
  - Cache miss after TTL: a request after the TTL expires re-invokes
    the loader.
  - Per-key isolation: different page names don't share an entry.
  - Loader failure not cached: a 502 should leave the cache empty so a
    recovery is observed on the next call.
  - Invalidation drops every entry: logout / login completion calls
    `_invalidate_page_cache`, which has to actually clear.

The endpoint handlers are invoked directly as plain Python functions
rather than through TestClient — that avoids needing a fully wired
FastAPI runtime (orjson, etc.) for what is essentially a unit test
of the cache wiring inside two route handlers.
"""
import pytest
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _clean_page_cache():
    """Make sure no cross-test bleed in the module-level cache."""
    import server

    server._invalidate_page_cache()
    yield
    server._invalidate_page_cache()


@pytest.fixture
def stub_auth(monkeypatch):
    """Bypass `_require_auth` so handlers run without a Tidal session."""
    import server

    monkeypatch.setattr(server, "_require_auth", lambda: None)


# ---------------------------------------------------------------------------
# Cache primitive tests — exercise the helpers directly so a regression
# in the lookup/store/invalidate logic shows up distinct from a regression
# in the endpoint wiring.
# ---------------------------------------------------------------------------


def test_lookup_returns_none_when_empty():
    import server
    assert server._lookup_page_cache("name:home") is None


def test_store_then_lookup_returns_value():
    import server
    server._store_page_cache("name:home", {"title": "Home", "categories": []})
    cached = server._lookup_page_cache("name:home")
    assert cached == {"title": "Home", "categories": []}


def test_lookup_expires_after_ttl(monkeypatch):
    import server

    # Pin time. The store_page_cache call timestamps at t=0; the
    # lookup runs at t=TTL+1 so the entry has aged past its window.
    times = iter([0.0, server._PAGE_CACHE_TTL + 1.0])
    monkeypatch.setattr(server.time, "monotonic", lambda: next(times))

    server._store_page_cache("name:home", {"hits": 1})
    assert server._lookup_page_cache("name:home") is None


def test_invalidate_drops_all_entries():
    import server
    server._store_page_cache("name:home", {"a": 1})
    server._store_page_cache("path:pages/genre_hip_hop", {"b": 2})
    server._invalidate_page_cache()
    assert server._lookup_page_cache("name:home") is None
    assert server._lookup_page_cache("path:pages/genre_hip_hop") is None


# ---------------------------------------------------------------------------
# Endpoint integration tests — call route handlers directly, count loader
# invocations to confirm the cache is intercepting on the second hit.
# ---------------------------------------------------------------------------


def test_editorial_page_caches_within_ttl(stub_auth, monkeypatch):
    import server

    calls = {"home": 0}

    def fake_home():
        calls["home"] += 1
        return _StubPage("Home", [])

    monkeypatch.setitem(server._KNOWN_PAGES, "home", fake_home)

    r1 = server.editorial_page("home")
    r2 = server.editorial_page("home")

    assert r1 == r2
    assert calls["home"] == 1, "second call should hit cache, not loader"


def test_editorial_page_recomputes_after_ttl(stub_auth, monkeypatch):
    import server

    calls = {"home": 0}

    def fake_home():
        calls["home"] += 1
        return _StubPage("Home", [])

    monkeypatch.setitem(server._KNOWN_PAGES, "home", fake_home)

    server.editorial_page("home")
    # Nudge the stored timestamp into the past so the next lookup
    # treats the entry as expired.
    with server._page_cache_lock:
        ts, value = server._page_cache["name:home"]
        server._page_cache["name:home"] = (ts - server._PAGE_CACHE_TTL - 1, value)

    server.editorial_page("home")
    assert calls["home"] == 2


def test_editorial_page_per_name_isolation(stub_auth, monkeypatch):
    import server

    calls = {"home": 0, "moods": 0}

    monkeypatch.setitem(
        server._KNOWN_PAGES,
        "home",
        lambda: _bump(calls, "home", _StubPage("Home", [])),
    )
    monkeypatch.setitem(
        server._KNOWN_PAGES,
        "moods",
        lambda: _bump(calls, "moods", _StubPage("Moods", [])),
    )

    server.editorial_page("home")
    server.editorial_page("moods")
    server.editorial_page("home")
    server.editorial_page("moods")

    assert calls == {"home": 1, "moods": 1}


def test_editorial_page_unknown_name_404_not_cached(stub_auth):
    import server

    with pytest.raises(HTTPException) as exc:
        server.editorial_page("does-not-exist")
    assert exc.value.status_code == 404


def test_resolve_page_v2_caches_within_ttl(stub_auth, monkeypatch):
    """V2 view-all paths are cached too — the resolve_page handler
    short-circuits on cache hit before invoking _fetch_v2_view_all."""
    import server

    calls = {"fetched": 0}

    def fake_v2(path: str) -> dict:
        calls["fetched"] += 1
        return {"title": "View All", "categories": [{"items": [{"id": "1"}]}]}

    monkeypatch.setattr(server, "_fetch_v2_view_all", fake_v2)

    req = server.PagePathRequest(path="home/pages/NEW_ALBUM_SUGGESTIONS/view-all")
    r1 = server.resolve_page(req)
    r2 = server.resolve_page(req)

    assert r1 == r2
    assert calls["fetched"] == 1


def test_resolve_page_distinct_paths_isolated(stub_auth, monkeypatch):
    import server

    calls = {"a": 0, "b": 0}

    def fake_v2(path: str) -> dict:
        if "a/view-all" in path:
            calls["a"] += 1
        else:
            calls["b"] += 1
        return {"title": path, "categories": []}

    monkeypatch.setattr(server, "_fetch_v2_view_all", fake_v2)

    server.resolve_page(server.PagePathRequest(path="a/view-all"))
    server.resolve_page(server.PagePathRequest(path="b/view-all"))
    server.resolve_page(server.PagePathRequest(path="a/view-all"))
    server.resolve_page(server.PagePathRequest(path="b/view-all"))

    assert calls == {"a": 1, "b": 1}


def test_editorial_page_loader_failure_not_cached(stub_auth, monkeypatch):
    import server

    state = {"raise": True, "calls": 0}

    def flaky_home():
        state["calls"] += 1
        if state["raise"]:
            raise RuntimeError("tidal hiccup")
        return _StubPage("Home", [])

    monkeypatch.setitem(server._KNOWN_PAGES, "home", flaky_home)

    with pytest.raises(HTTPException) as exc:
        server.editorial_page("home")
    assert exc.value.status_code == 502
    assert state["calls"] == 1

    # Recover and try again — must call loader, NOT serve a cached error.
    state["raise"] = False
    result = server.editorial_page("home")
    assert isinstance(result, dict)
    assert state["calls"] == 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubPage:
    """Minimal stand-in for tidalapi.Page — `_serialize_page` reads
    .title and iterates .categories, both of which we provide here."""

    def __init__(self, title: str, categories: list):
        self.title = title
        self.categories = categories


def _bump(counter: dict, key: str, value):
    counter[key] += 1
    return value
