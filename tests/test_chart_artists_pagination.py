"""Popular > Artists paginates + resolves server-side (#perf).

The Artists tab used to render all 50 chart cards at once, and each card
fired its own Tidal search for the artist id *and* another for the artist
image on mount — ~100 concurrent searches that tripped Tidal's abuse
backoff. It now resolves the requested ~18-artist page server-side in a
bounded pool and streams more on scroll. These pin the {items, has_more}
contract, that only the page is resolved (a bounded number of searches),
and that each item carries the resolved tidal_id / tidal_picture.
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
    monkeypatch.setattr(server, "_image_url", lambda a, size: f"img:{a.id}")


def _entries(n):
    return [
        {"name": f"Artist {i}", "playcount": i, "listeners": i, "image": ""}
        for i in range(n)
    ]


def test_first_page_resolves_only_the_page(stub, monkeypatch):
    import server

    monkeypatch.setattr(
        server.lastfm, "get_chart_top_artists", lambda limit: _entries(30)
    )
    searched: list[str] = []

    def _search(q, limit=10):
        searched.append(q)
        return {"artists": [SimpleNamespace(id=f"tid-{q}", name=q)]}

    monkeypatch.setattr(server.tidal, "search", _search)

    out = server.lastfm_chart_top_artists_resolved(offset=0, limit=18)
    assert len(out["items"]) == 18
    assert out["has_more"] is True
    # Only the 18-artist page was resolved, not the whole 30-entry chart.
    assert len(searched) == 18
    first = out["items"][0]
    assert first["name"] == "Artist 0"
    assert first["tidal_id"] == "tid-Artist 0"
    assert first["tidal_picture"] == "img:tid-Artist 0"
    # Last.fm fields survive the merge.
    assert first["listeners"] == 0


def test_last_page(stub, monkeypatch):
    import server

    monkeypatch.setattr(
        server.lastfm, "get_chart_top_artists", lambda limit: _entries(30)
    )
    monkeypatch.setattr(
        server.tidal,
        "search",
        lambda q, limit=10: {"artists": [SimpleNamespace(id=q, name=q)]},
    )

    out = server.lastfm_chart_top_artists_resolved(offset=18, limit=18)
    assert len(out["items"]) == 12
    assert out["has_more"] is False


def test_unresolved_artist_yields_null_id(stub, monkeypatch):
    import server

    monkeypatch.setattr(
        server.lastfm, "get_chart_top_artists", lambda limit: _entries(1)
    )
    # Tidal returns nothing for this artist.
    monkeypatch.setattr(server.tidal, "search", lambda q, limit=10: {"artists": []})

    out = server.lastfm_chart_top_artists_resolved(offset=0, limit=18)
    assert len(out["items"]) == 1
    assert out["items"][0]["tidal_id"] is None
    assert out["items"][0]["tidal_picture"] is None
