"""Tests for AOTY genre browsing (genre index + recent-by-genre).

These cover the scrape-shape contracts that the New-releases genre
picker depends on:

  - /genre.php lists each genre twice (named link + "View More");
    genre_index() must keep one {slug, name} per genre.
  - /genre/{slug}/ stacks several `div.section` blocks; only the
    "Recent {Genre} Albums" one is new releases. recent_releases_by_
    genre() must scope to that section, not the all-time canon.
  - A caller-supplied slug must be shape-checked before it reaches a
    URL.

_fetch is monkeypatched so nothing here touches the network.
"""
from __future__ import annotations

import pytest

import app.aoty as aoty


@pytest.fixture(autouse=True)
def _clear_cache():
    aoty._cache.clear()
    yield
    aoty._cache.clear()


_GENRE_INDEX_HTML = """
<html><body>
  <a href="/genre/7-rock/">Rock</a>
  <a href="/genre/7-rock/">View More</a>
  <a href="/genre/15-pop/">Pop</a>
  <a href="/genre/15-pop/">View More</a>
  <a href="/genre/22-r-and-b/">R&amp;B</a>
  <a href="/not-a-genre/">Nope</a>
  <a href="/genre/bad-slug/">Bad</a>
</body></html>
"""


def _block(artist: str, title: str) -> str:
    return f"""
    <div class="albumBlock">
      <div class="image"><a href="/album/1-x.php">
        <img src="https://cdn/x.jpg" alt="{artist} - {title}"/></a></div>
      <a href="/artist/1-a/"><div class="artistTitle">{artist}</div></a>
      <a href="/album/1-x.php"><div class="albumTitle">{title}</div></a>
      <div class="type">May 1 · LP</div>
    </div>
    """


_GENRE_PAGE_HTML = f"""
<html><body>
  <div class="section">
    <h2 class="subHeadline"><a href="/genre/7-rock/2026/">
      Users' Highest Rated Rock Albums of 2026</a></h2>
    {_block("Canon Band", "All Time Classic")}
  </div>
  <div class="section">
    <h2 class="subHeadline"><a href="/genre/7-rock/all/">
      Users' Highest Rated Rock Albums of All Time</a></h2>
    {_block("Canon Act", "The Canon")}
    {_block("Second Canon", "Also Canon")}
  </div>
  <div class="section">
    <h2 class="subHeadline"><a href="/genre/7-rock/recent/">
      Recent Rock Albums</a></h2>
    {_block("Fresh Act", "Brand New LP")}
    {_block("Another New", "Just Dropped")}
  </div>
</body></html>
"""


def test_genre_index_dedupes_and_keeps_named(monkeypatch):
    monkeypatch.setattr(aoty, "_fetch", lambda url: _GENRE_INDEX_HTML)
    out = aoty.genre_index()
    # One entry per genre, "View More" duplicates dropped, malformed
    # /genre/ hrefs and non-genre links ignored, sorted by name.
    assert out == [
        {"slug": "15-pop", "name": "Pop"},
        {"slug": "22-r-and-b", "name": "R&B"},
        {"slug": "7-rock", "name": "Rock"},
    ]


def test_recent_by_genre_scopes_to_recent_section(monkeypatch):
    monkeypatch.setattr(aoty, "_fetch", lambda url: _GENRE_PAGE_HTML)
    out = aoty.recent_releases_by_genre("7-rock", limit=10)
    titles = [(e["artist"], e["title"]) for e in out]
    # Only the "Recent" section — the all-time-canon section above it
    # must not bleed in (that would mislabel the canon as "new").
    assert titles == [
        ("Fresh Act", "Brand New LP"),
        ("Another New", "Just Dropped"),
    ]


def test_recent_by_genre_rejects_malformed_slug(monkeypatch):
    called = []
    monkeypatch.setattr(
        aoty, "_fetch", lambda url: called.append(url) or ""
    )
    assert aoty.recent_releases_by_genre("../../etc/passwd") == []
    assert aoty.recent_releases_by_genre("rock") == []
    # Guard must reject before any fetch is attempted.
    assert called == []


def test_recent_by_genre_empty_when_no_recent_section(monkeypatch):
    html = """<html><body><div class="section">
      <h2 class="subHeadline"><a>Users' Highest Rated</a></h2>
      </div></body></html>"""
    monkeypatch.setattr(aoty, "_fetch", lambda url: html)
    # No "Recent" section → empty, not a fallback to the whole page.
    assert aoty.recent_releases_by_genre("7-rock") == []


def _list_row(artist: str, title: str) -> str:
    return f"""
    <div class="albumListRow">
      <h2 class="albumListTitle">
        <a href="/album/1-x.php" itemprop="url">{artist} - {title}</a>
      </h2>
      <div class="albumListGenre">
        <a href="/genre/234-abstract-hip-hop/">Abstract Hip Hop</a>
      </div>
    </div>
    """


def test_top_by_genre_paginates_and_caps(monkeypatch):
    # Page 1 is the bare /genre/{slug}/{year}/ path, later pages take
    # a trailing {page}/. Two pages of 2 rows, then empty.
    pages = {
        "/genre/234-abstract-hip-hop/2026/": _list_row("A", "One")
        + _list_row("B", "Two"),
        "/genre/234-abstract-hip-hop/2026/2/": _list_row("C", "Three")
        + _list_row("D", "Four"),
    }
    seen = []

    def fake_fetch(url):
        path = url.split("albumoftheyear.org")[-1]
        seen.append(path)
        return pages.get(path, "")

    monkeypatch.setattr(aoty, "_fetch", fake_fetch)
    out = aoty.top_albums_of_year_by_genre(
        "234-abstract-hip-hop", 2026, limit=10
    )
    assert [(e["artist"], e["title"]) for e in out] == [
        ("A", "One"),
        ("B", "Two"),
        ("C", "Three"),
        ("D", "Four"),
    ]
    # Slug carried through so the picker can round-trip it.
    assert out[0]["genre_slugs"] == ["234-abstract-hip-hop"]
    # Walked page 1 (bare) then page 2 (suffixed), then stopped on
    # the empty third page.
    assert seen[0].endswith("/genre/234-abstract-hip-hop/2026/")
    assert "/genre/234-abstract-hip-hop/2026/2/" in seen

    out2 = aoty.top_albums_of_year_by_genre(
        "234-abstract-hip-hop", 2026, limit=3
    )
    assert len(out2) == 3  # limit caps mid-page


def test_top_by_genre_rejects_malformed_slug(monkeypatch):
    called = []
    monkeypatch.setattr(
        aoty, "_fetch", lambda url: called.append(url) or ""
    )
    assert aoty.top_albums_of_year_by_genre("../../secrets", 2026) == []
    assert called == []


# ---------------------------------------------------------------------------
# All-time genre canon (drives the "From Your Genres" row)
# ---------------------------------------------------------------------------


def _rating_row(artist: str, title: str) -> str:
    return (
        "<div class='albumListRow'>"
        "<h2 class='albumListTitle'>"
        f"<a href='/album/1-x.php' itemprop='url'>{artist} - {title}</a>"
        "</h2></div>"
    )


def _ratings_page(*pairs) -> str:
    return "<html><body>" + "".join(_rating_row(a, t) for a, t in pairs) + "</body></html>"


def test_top_by_genre_reads_the_ratings_listing_not_the_teaser(monkeypatch):
    """The genre landing page shows only five as a teaser. The canon has
    to come from the listing behind its header link, which is keyed by
    the genre's *name* rather than its numeric slug."""
    import app.aoty as aoty_mod

    seen = []

    def _fake(url):
        seen.append(url)
        return _ratings_page(("Slowdive", "Souvlaki"))

    monkeypatch.setattr(aoty_mod, "_fetch", _fake)
    out = aoty.top_albums_by_genre("26-shoegaze", limit=1)
    assert [a["title"] for a in out] == ["Souvlaki"]
    assert "/ratings/user-highest-rated/all/shoegaze/1/" in seen[0]
    assert "/genre/26-shoegaze/" not in seen[0], "read the teaser page"


def test_top_by_genre_pages_until_limit(monkeypatch):
    """Each listing page carries ~25 rows, so filling a deep request has
    to walk pages rather than stopping at the first."""
    pages = {
        1: _ratings_page(("A", "a1"), ("B", "b1")),
        2: _ratings_page(("C", "c1"), ("D", "d1")),
        3: _ratings_page(("E", "e1")),
    }
    seen = []

    def _fake(url):
        n = int(url.rstrip("/").rsplit("/", 1)[-1])
        seen.append(n)
        return pages.get(n, "")

    monkeypatch.setattr(aoty, "_fetch", _fake)
    out = aoty.top_albums_by_genre("26-shoegaze", limit=5)
    assert [a["title"] for a in out] == ["a1", "b1", "c1", "d1", "e1"]
    assert seen == [1, 2, 3]


def test_top_by_genre_stops_on_empty_page(monkeypatch):
    """A genre with fewer albums than requested must not keep fetching."""
    def _fake(url):
        n = int(url.rstrip("/").rsplit("/", 1)[-1])
        return _ratings_page(("A", "a1")) if n == 1 else "<html><body></body></html>"

    monkeypatch.setattr(aoty, "_fetch", _fake)
    out = aoty.top_albums_by_genre("26-shoegaze", limit=50)
    assert [a["title"] for a in out] == ["a1"]


def test_top_by_genre_rejects_malformed_slug(monkeypatch):
    called = []
    monkeypatch.setattr(aoty, "_fetch", lambda url: called.append(url) or "")
    assert aoty.top_albums_by_genre("../../etc/passwd") == []
    assert aoty.top_albums_by_genre("rock") == []
    assert called == [], "a malformed slug must not reach the network"


def test_top_by_genre_survives_fetch_failure(monkeypatch):
    monkeypatch.setattr(aoty, "_fetch", lambda url: None)
    assert aoty.top_albums_by_genre("7-rock") == []
