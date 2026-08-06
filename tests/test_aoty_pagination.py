"""AOTY drill-down endpoints paginate — resolving only the page shown to
Tidal instead of all 100 albums up front (the slow-load perf report).

The AOTY *listing* scrape is cheap and cached; `resolve_listing` (a Tidal
search per album) is the slow part, so these pin that only the requested
slice is resolved and that `has_more` / first-page `genres` are correct.
"""
import pytest


def _listing(n):
    return [
        {
            "artist": f"A{i}",
            "title": f"T{i}",
            "genres": ["Rock"] if i % 2 else ["Pop"],
            "genre_slugs": ["7-rock"] if i % 2 else ["9-pop"],
        }
        for i in range(n)
    ]


@pytest.fixture
def stub(monkeypatch):
    import server

    monkeypatch.setattr(server, "_require_auth", lambda: None)
    # Resolver passthrough so we can count what got resolved.
    resolved = {"pages": []}
    monkeypatch.setattr(
        server.aoty_resolver,
        "resolve_listing",
        lambda page: (resolved["pages"].append(list(page)) or list(page)),
    )
    return resolved


def test_first_page_slices_and_flags_more(stub, monkeypatch):
    import server

    monkeypatch.setattr(
        server.aoty_module, "top_albums_of_year", lambda y, limit: _listing(30)
    )
    out = server.aoty_top_of_year(offset=0, limit=18)
    assert len(out["items"]) == 18
    assert out["has_more"] is True
    assert "genres" in out  # picker options on the first page
    # Only the 18-album page was resolved, not all 30.
    assert len(stub["pages"][-1]) == 18


def test_last_page_no_more_no_genres(stub, monkeypatch):
    import server

    monkeypatch.setattr(
        server.aoty_module, "top_albums_of_year", lambda y, limit: _listing(30)
    )
    out = server.aoty_top_of_year(offset=18, limit=18)
    assert len(out["items"]) == 12
    assert out["has_more"] is False
    assert "genres" not in out  # only the first page carries them


def test_genre_page_has_no_genres_field(stub, monkeypatch):
    import server

    monkeypatch.setattr(
        server.aoty_module,
        "top_albums_of_year_by_genre",
        lambda slug, y, limit: _listing(10),
    )
    out = server.aoty_top_of_year(offset=0, limit=18, genre="7-rock")
    assert out["has_more"] is False
    assert "genres" not in out


def test_recent_releases_paginates(stub, monkeypatch):
    import server

    monkeypatch.setattr(
        server.aoty_module, "recent_releases", lambda limit: _listing(25)
    )
    out = server.aoty_recent_releases(offset=0, limit=18)
    assert len(out["items"]) == 18
    assert out["has_more"] is True


def test_listing_genres_dedups_and_sorts():
    import server

    listing = [
        {"genres": ["Rock", "Pop"], "genre_slugs": ["7-rock", "9-pop"]},
        {"genres": ["Rock"], "genre_slugs": ["7-rock"]},
    ]
    genres = server._aoty_listing_genres(listing)
    assert [g["name"] for g in genres] == ["Pop", "Rock"]  # sorted, deduped
    assert {g["slug"] for g in genres} == {"7-rock", "9-pop"}
