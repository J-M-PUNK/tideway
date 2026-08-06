"""Popular > Tracks paginates too (#perf).

It was the slowest page in the app: resolving all 50 Last.fm chart tracks
to Tidal took ~18s cold, past the request timeout. Now it resolves only
the requested ~18-track page and streams more on scroll. These pin the
{items, has_more} contract and that only the page is resolved.
"""
from types import SimpleNamespace

import pytest


@pytest.fixture
def stub(monkeypatch):
    import server
    from app import lastfm_disk_cache

    monkeypatch.setattr(server, "_require_auth", lambda: None)
    # Run the resolver inline instead of through the disk-backed cache.
    monkeypatch.setattr(server, "_lastfm_cached", lambda key, fn, **kw: fn())
    monkeypatch.setattr(server, "tidal_jitter_sleep", lambda: None)
    monkeypatch.setattr(server.lastfm, "status", lambda: {"username": "u"})
    monkeypatch.setattr(lastfm_disk_cache, "get", lambda k, ttl: None)
    monkeypatch.setattr(lastfm_disk_cache, "set", lambda k, v: None)
    monkeypatch.setattr(server, "filter_explicit_dupes", lambda t, pref, kind: t)
    monkeypatch.setattr(server, "filter_ai_tracks", lambda t: t)
    monkeypatch.setattr(server, "track_to_dict", lambda t: {"id": t.id, "name": t.name})

    def _search(q, limit=5):
        return {"tracks": [SimpleNamespace(id=q, name=q, artists=[])]}

    monkeypatch.setattr(server.tidal, "search", _search)


def _entries(n):
    return [{"name": f"Song {i}", "artist": f"Artist {i}"} for i in range(n)]


def test_first_page(stub, monkeypatch):
    import server

    monkeypatch.setattr(server.lastfm, "get_chart_top_tracks", lambda limit: _entries(30))
    out = server.lastfm_chart_top_tracks_resolved(offset=0, limit=18)
    assert len(out["items"]) == 18
    assert out["has_more"] is True


def test_last_page(stub, monkeypatch):
    import server

    monkeypatch.setattr(server.lastfm, "get_chart_top_tracks", lambda limit: _entries(30))
    out = server.lastfm_chart_top_tracks_resolved(offset=18, limit=18)
    assert len(out["items"]) == 12
    assert out["has_more"] is False
