"""A failed fetch must not be cached as an empty result.

AOTY's surfaces cache for a long time by design — an hour for the year
charts, a day for the genre index. That is correct for real data and
badly wrong for a fetch that never returned any: a single failure would
otherwise pin the rows empty for the whole TTL.

The case that made this matter is startup. `_prewarm_aoty` runs during
FastAPI lifespan, which under the packaged app is a few seconds before
pywebview is up to clear Cloudflare's challenge. Caching that miss meant
every AOTY row stayed empty for an hour over a few seconds of bad
timing — with the cache hiding the recovery, so even a working clearance
changed nothing until the TTL lapsed.

The distinction being pinned here is failure vs. genuine emptiness: a
fetch that succeeds and yields no rows (a quiet release week) is real
data and *should* cache.
"""
from __future__ import annotations

import pytest

from app import aoty


@pytest.fixture(autouse=True)
def _clear_cache():
    aoty._cache.clear()
    yield
    aoty._cache.clear()


def test_failed_fetch_is_not_cached_and_retries(monkeypatch):
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        # Fail the first call, succeed on the retry with one row.
        if calls["n"] == 1:
            return None
        return _ONE_ROW

    monkeypatch.setattr(aoty, "_fetch", flaky)

    assert aoty.recent_releases(limit=10) == []
    # The miss must not have been cached, so this re-fetches.
    second = aoty.recent_releases(limit=10)
    assert [a["title"] for a in second] == ["Real Album"]
    assert calls["n"] > 1


def test_successful_but_empty_fetch_is_cached(monkeypatch):
    calls = {"n": 0}

    def empty(url):
        calls["n"] += 1
        return "<html><body>no cards here</body></html>"

    monkeypatch.setattr(aoty, "_fetch", empty)

    assert aoty.recent_releases(limit=10) == []
    assert aoty.recent_releases(limit=10) == []
    # A real "AOTY has nothing this week" answer is data — cache it
    # rather than re-scraping on every Home load.
    assert calls["n"] == 1


def test_failed_genre_index_is_not_cached(monkeypatch):
    # The genre index has a 24-hour TTL, so caching a miss here is the
    # most expensive version of this bug.
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return '<a href="/genre/7-rock/">Rock</a>'

    monkeypatch.setattr(aoty, "_fetch", flaky)

    assert aoty.genre_index() == []
    assert aoty.genre_index() == [{"slug": "7-rock", "name": "Rock"}]


def test_failed_year_chart_page_is_not_cached(monkeypatch):
    # The paginated surfaces bail out of their page loop on a failed
    # fetch. Partial or empty, the result isn't a complete answer and
    # shouldn't be pinned for an hour.
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        # One row on the retry's first page, then an empty page so the
        # pagination loop terminates the way a real short chart would.
        return _ONE_LIST_ROW if calls["n"] == 2 else "<html></html>"

    monkeypatch.setattr(aoty, "_fetch", flaky)

    assert aoty.top_albums_of_year(2026, limit=5) == []
    again = aoty.top_albums_of_year(2026, limit=5)
    assert [a["title"] for a in again] == ["Charted Album"]


_ONE_ROW = """
<div class="albumBlock">
  <div class="image"><img src="/x.jpg"></div>
  <a href="/album/1-real-album.php">
    <div class="artistTitle">Real Artist</div>
    <div class="albumTitle">Real Album</div>
  </a>
</div>
"""

_ONE_LIST_ROW = """
<div class="albumListRow">
  <h2 class="albumListTitle">
    <a itemprop="url" href="/album/2-charted.php">Charted Artist - Charted Album</a>
  </h2>
</div>
"""
