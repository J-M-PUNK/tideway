"""AlbumOfTheYear.org chart scraper.

Two surfaces:

  - `top_albums_of_year(year, limit)` reads
    `/ratings/6-highest-rated/{year}/{page}/` — AOTY's canonical
    "Best of {year}" ranking (the headline list, aggregated /
    critic-blended). NOT `/ratings/user-highest-rated/{year}/`,
    which is a separate user-only ranking with materially
    different ordering. Stable list that turns over slowly
    enough that an hour-level cache is comfortable.

  - `recent_releases(limit)` reads `/releases/this-week/` — the
    explicitly-this-week scope of AOTY's release grid. The
    unsuffixed `/releases/` page shows a broader / different
    set of cards and is NOT what we want for the "New Releases"
    Home row. A 30-minute cache feels right since AOTY adds new
    releases throughout the day.

Both endpoints return a list of `AotyAlbum`. Resolving each entry
to a Tidal album is the consumer's job (server.py / endpoint
handler) — keeps this module pure parsing.

Caching is in-memory only with a per-key TTL. AOTY data is hour-
level fresh; a process restart re-pays ~1-2 s to refetch the
chart, which is acceptable for the use case (Home page section
visible on launch). Disk persistence isn't necessary at this
scale.

Failures are silent: HTTP errors, layout changes, or a missing
selector return an empty list rather than raising. The caller
sees zero results and renders a fallback empty state.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_BASE_URL = "https://www.albumoftheyear.org"

# A modern desktop UA — the page returns a 403 for an empty UA and
# returns the mobile layout for some bot-string defaults. Plain
# Chrome desktop matches what an actual browser sends and gives us
# the desktop HTML the rest of this module is parsing for.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_HTTP_TIMEOUT_SEC = 15.0

# In-memory cache. Per-key TTL so the two surfaces can refresh on
# different cadences without fighting over a single TTL constant.
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, list[dict]]] = {}

# Default TTLs. Top-rated lists turn over slowly (the order changes
# only as users rate over time); the releases page is a bit more
# active because new albums get added daily.
_TOP_OF_YEAR_TTL_SEC = 3600.0  # 1 hour
_RECENT_RELEASES_TTL_SEC = 1800.0  # 30 min


@dataclass
class AotyAlbum:
    """One album from an AOTY listing.

    Fields are intentionally permissive (most are Optional) — AOTY
    occasionally ships rows with missing scores during release week
    or rows with no cover for unreleased albums. The consumer
    decides how to render partial data."""

    title: str
    artist: str
    score: Optional[int] = None
    rating_count: Optional[int] = None
    cover_url: Optional[str] = None
    release_date: Optional[str] = None
    rank: Optional[int] = None
    must_hear: bool = False
    aoty_url: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def top_albums_of_year(year: int, limit: int = 50) -> list[dict]:
    """Highest-user-rated albums for the given year.

    `limit` caps the result list. AOTY paginates at ~25 per page, so
    a limit of 50 fetches two pages, 100 fetches four, and so on.
    Capped at 100 to keep the worst-case fetch budget bounded.
    """
    limit = max(1, min(int(limit), 100))
    cache_key = f"top:{year}:{limit}"
    cached = _cache_get(cache_key, _TOP_OF_YEAR_TTL_SEC)
    if cached is not None:
        return cached

    out: list[AotyAlbum] = []
    page = 1
    # AOTY's pagination: /ratings/6-highest-rated/{year}/{page}/.
    # `6-highest-rated` is AOTY's canonical aggregated ranking — the
    # headline "Best of {year}" list users actually mean when they
    # say "AOTY top albums." Distinct from `user-highest-rated`,
    # which is the separate user-only score. Each page renders ~25
    # rows. Walk pages until we hit `limit` or a page returns no rows.
    while len(out) < limit and page <= 6:
        path = f"/ratings/6-highest-rated/{year}/{page}/"
        html = _fetch(urljoin(_BASE_URL, path))
        if html is None:
            break
        rows = _parse_album_list_rows(html)
        if not rows:
            break
        out.extend(rows)
        page += 1

    out = out[:limit]
    payload = [a.to_dict() for a in out]
    _cache_set(cache_key, payload)
    return payload


def recent_releases(limit: int = 30) -> list[dict]:
    """Recently-released albums, AOTY's grid-card view at /releases/this-week/.

    Rows here use a different DOM shape than the top-of-year page —
    artist and title are in separate elements rather than combined
    in a single anchor — so they have their own parser.
    """
    limit = max(1, min(int(limit), 100))
    cache_key = f"recent:{limit}"
    cached = _cache_get(cache_key, _RECENT_RELEASES_TTL_SEC)
    if cached is not None:
        return cached

    # `/releases/this-week/` — explicitly the current week's releases.
    # The unsuffixed `/releases/` shows a different (broader) set of
    # cards and is NOT what we want for the New Releases Home row.
    html = _fetch(urljoin(_BASE_URL, "/releases/this-week/"))
    if html is None:
        _cache_set(cache_key, [])
        return []
    rows = _parse_album_block_cards(html)[:limit]
    payload = [a.to_dict() for a in rows]
    _cache_set(cache_key, payload)
    return payload


# --- internals -------------------------------------------------------------


def _cache_get(key: str, ttl_sec: float) -> Optional[list[dict]]:
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        cached_at, payload = entry
        if now - cached_at > ttl_sec:
            return None
        # Defensive copy — AOTY entries are plain dicts but the
        # consumer might mutate (sort, filter, decorate with Tidal
        # fields). Don't let that poison the cache.
        return [dict(d) for d in payload]


def _cache_set(key: str, payload: list[dict]) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), payload)


def _fetch(url: str) -> Optional[str]:
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=_HTTP_TIMEOUT_SEC,
        )
    except Exception as exc:
        log.warning("aoty fetch %s failed: %s", url, exc)
        return None
    if r.status_code != 200:
        log.warning("aoty fetch %s returned %d", url, r.status_code)
        return None
    # AOTY serves UTF-8 but doesn't always declare a charset in
    # Content-Type, which makes requests fall back to ISO-8859-1
    # for the .text attribute and mangle multi-byte characters
    # (the middle-dot in "Apr 30 · LP" comes back as the U+FFFD
    # replacement character). Force UTF-8 — the apparent_encoding
    # check would also catch this, but it's an O(n) scan and we
    # already know the right answer.
    r.encoding = "utf-8"
    return r.text


def _parse_album_list_rows(html: str) -> list[AotyAlbum]:
    """Parser for the `albumListRow` shape used on /ratings/* pages.

    Each row has the artist and title combined in the title anchor
    ("Artist - Title"). We split on the first " - " — AOTY uses
    that as their separator, and artist names containing " - "
    are vanishingly rare.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("div.albumListRow")
    out: list[AotyAlbum] = []
    for row in rows:
        title_anchor = row.select_one("h2.albumListTitle a[itemprop='url']")
        if title_anchor is None:
            continue
        combined = title_anchor.get_text(strip=True)
        artist, title = _split_artist_title(combined)
        if not artist or not title:
            continue

        rank: Optional[int] = None
        rank_el = row.select_one("span.albumListRank span[itemprop='position']")
        if rank_el is not None:
            try:
                rank = int(rank_el.get_text(strip=True))
            except ValueError:
                rank = None

        score: Optional[int] = None
        score_el = row.select_one("div.scoreValueContainer .scoreValue")
        if score_el is not None:
            try:
                score = int(score_el.get_text(strip=True))
            except ValueError:
                score = None

        rating_count = _parse_rating_count(
            row.select_one("div.albumListScoreContainer .scoreText")
        )

        cover_url: Optional[str] = None
        img = row.select_one("div.albumListCover img")
        if img is not None:
            src = img.get("src") or ""
            if isinstance(src, str) and src:
                cover_url = src

        release_date_el = row.select_one("div.albumListDate")
        release_date = (
            release_date_el.get_text(strip=True) if release_date_el else None
        )

        must_hear = row.select_one("div.albumListCover.mustHear") is not None

        aoty_url: Optional[str] = None
        href = title_anchor.get("href")
        if isinstance(href, str) and href:
            aoty_url = urljoin(_BASE_URL, href)

        out.append(
            AotyAlbum(
                title=title,
                artist=artist,
                score=score,
                rating_count=rating_count,
                cover_url=cover_url,
                release_date=release_date,
                rank=rank,
                must_hear=must_hear,
                aoty_url=aoty_url,
            )
        )
    return out


def _parse_album_block_cards(html: str) -> list[AotyAlbum]:
    """Parser for the `albumBlock` shape used on /releases/.

    Cards have the artist and title in separate elements, so no
    splitting is needed. Score and rating count are present when
    AOTY has rated the release; brand-new releases sometimes ship
    with a 0 / no rating, which we surface as None rather than 0.
    """
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.select("div.albumBlock")
    out: list[AotyAlbum] = []
    for block in blocks:
        artist_el = block.select_one(".artistTitle")
        title_el = block.select_one(".albumTitle")
        if artist_el is None or title_el is None:
            continue
        artist = artist_el.get_text(strip=True)
        title = title_el.get_text(strip=True)
        if not artist or not title:
            continue

        score: Optional[int] = None
        rating = block.select_one(".rating")
        if rating is not None:
            try:
                value = int(rating.get_text(strip=True))
                # AOTY shows "NR" or empty for no-rating yet; an
                # explicit 0 is also "no real rating yet" rather
                # than "literally 0/100", so surface that as None
                # too.
                score = value if value > 0 else None
            except ValueError:
                score = None

        rating_count = _parse_rating_count(
            block.select_one(".ratingText:nth-of-type(2)")
        ) or _parse_rating_count(block.select_one(".ratingText + .ratingText"))

        cover_url: Optional[str] = None
        img = block.select_one(".image img")
        if img is not None:
            src = img.get("src") or ""
            if isinstance(src, str) and src:
                cover_url = src

        release_label_el = block.select_one(".type")
        release_label = (
            release_label_el.get_text(strip=True) if release_label_el else None
        )

        aoty_url: Optional[str] = None
        href_el = block.select_one(".albumTitle")
        href_anchor = href_el.find_parent("a") if href_el else None
        if href_anchor is not None:
            href = href_anchor.get("href")
            if isinstance(href, str) and href:
                aoty_url = urljoin(_BASE_URL, href)

        out.append(
            AotyAlbum(
                title=title,
                artist=artist,
                score=score,
                rating_count=rating_count,
                cover_url=cover_url,
                release_date=release_label,
                rank=None,
                must_hear=False,
                aoty_url=aoty_url,
            )
        )
    return out


def _split_artist_title(combined: str) -> tuple[str, str]:
    """Split AOTY's "Artist - Title" form. Returns (artist, title)
    with both empty on malformed input."""
    if not combined:
        return "", ""
    idx = combined.find(" - ")
    if idx < 0:
        return "", combined.strip()
    return combined[:idx].strip(), combined[idx + 3 :].strip()


def _parse_rating_count(el) -> Optional[int]:
    """Extract `12,598 ratings` → 12598 (or `(304)` → 304). Returns
    None when the element is missing or the digits don't parse."""
    if el is None:
        return None
    raw = el.get_text(" ", strip=True)
    digits = re.sub(r"[^0-9]", "", raw)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None
