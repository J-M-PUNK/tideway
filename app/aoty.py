"""AlbumOfTheYear.org chart scraper.

Two surfaces:

  - `top_albums_of_year(year, limit)` reads
    `/ratings/user-highest-rated/{year}/{page}/` — AOTY's
    user-rating-ordered "best of {year}" ranking. We previously
    tried `/ratings/6-highest-rated/{year}/` thinking that was
    AOTY's canonical aggregated/critic ranking, but that path
    404s for year-scoped requests (likely an all-time-only ID).
    The user-rated list is the only public year-scoped chart
    AOTY exposes at a stable URL, so we use it. Stable enough
    that an hour-level cache is comfortable.

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

Cloudflare note (added 2026-05-20): AOTY put their entire site
behind Cloudflare's anti-bot challenge. Every request from a
stock HTTP client now returns 403 with `cf-mitigated: challenge`
and the JS interstitial. Plain `requests` can't pass it; the
homepage sections silently emptied out as the existing in-memory
cache aged out. We switched the fetch path to `curl_cffi` with
browser-fingerprinted TLS (`impersonate="chrome120"`), which gets
back HTTP 200 with the full HTML. curl_cffi is already a project
dependency — used for Tidal's anti-bot path — so no new deps.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, asdict, field
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

log = logging.getLogger(__name__)

_BASE_URL = "https://www.albumoftheyear.org"

# Browser to impersonate at the TLS / HTTP/2 / header layer.
# curl_cffi sets the full Chrome 120 fingerprint when this is
# active — including the User-Agent, every `sec-ch-ua-*` client
# hint, `Accept-Language`, and the order of headers. Passing our
# own `User-Agent` override (or any other header curl_cffi already
# sets) breaks Cloudflare's consistency check: a Chrome TLS hello
# paired with an inconsistent set of HTTP headers reads as bot
# automation. So _fetch() below deliberately sends NO custom
# headers — the impersonate profile is the whole story.
_CFFI_IMPERSONATE = "chrome120"

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
# The genre list on /genre.php changes maybe once a year; the
# per-genre recent grid turns over like the global one.
_GENRE_INDEX_TTL_SEC = 86400.0  # 24 hours
_GENRE_RELEASES_TTL_SEC = 1800.0  # 30 min

# AOTY genre slugs are "{numeric-id}-{kebab-name}" (e.g. "7-rock",
# "22-r-and-b"). Pin the shape so a caller-supplied value can't be
# bent into an arbitrary path on albumoftheyear.org.
_GENRE_SLUG_RE = re.compile(r"^\d+-[a-z0-9-]+$")


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
    # AOTY's own genre tags for the album. Present on the
    # `/ratings/*` list rows; the `/releases/this-week/` cards don't
    # carry genre in their markup, so those stay empty. `genre_slugs`
    # is parallel to `genres` (same order, same length) and holds the
    # "{id}-{kebab}" path segment so the Top-of-year picker can fetch
    # that genre's real year chart instead of filtering the global
    # top-100 down to a handful.
    genres: list[str] = field(default_factory=list)
    genre_slugs: list[str] = field(default_factory=list)

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
    # AOTY's pagination: /ratings/user-highest-rated/{year}/{page}/.
    # `6-highest-rated` was tried first based on the assumption that
    # the all-time aggregated list ID extended to year-scoped URLs
    # — it doesn't, that path 404s. `user-highest-rated` is the only
    # year-scoped chart AOTY exposes at a stable URL. Each page
    # renders ~25 rows. Walk pages until we hit `limit` or a page
    # returns no rows.
    while len(out) < limit and page <= 6:
        path = f"/ratings/user-highest-rated/{year}/{page}/"
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


def top_albums_of_year_by_genre(
    genre_slug: str, year: int, limit: int = 60
) -> list[dict]:
    """Highest-rated albums of `year` for one genre.

    AOTY scopes its year chart by genre at `/genre/{slug}/{year}/`,
    same `albumListRow` shape and same ~25-per-page pagination as the
    global chart (page 1 is the bare path, later pages take a
    trailing `{page}/`). This is the real per-genre chart — not the
    global top-100 filtered down, which left niche genres with a
    handful of entries.
    """
    limit = max(1, min(int(limit), 100))
    if not _GENRE_SLUG_RE.match(genre_slug or ""):
        log.warning("aoty: rejecting malformed genre slug %r", genre_slug)
        return []
    cache_key = f"genre-top:{genre_slug}:{year}:{limit}"
    cached = _cache_get(cache_key, _TOP_OF_YEAR_TTL_SEC)
    if cached is not None:
        return cached

    out: list[AotyAlbum] = []
    page = 1
    while len(out) < limit and page <= 6:
        suffix = "" if page == 1 else f"{page}/"
        path = f"/genre/{genre_slug}/{year}/{suffix}"
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


def genre_index() -> list[dict]:
    """The genre list from /genre.php as `[{slug, name}, ...]`.

    Each genre is linked as `/genre/{id}-{slug}/`. The page repeats
    every link with a "View More" label; we keep the first
    human-named occurrence per slug and drop the duplicates. Used to
    populate the genre picker on the New-releases drill-down.
    """
    cached = _cache_get("genre-index", _GENRE_INDEX_TTL_SEC)
    if cached is not None:
        return cached

    html = _fetch(urljoin(_BASE_URL, "/genre.php"))
    if html is None:
        _cache_set("genre-index", [])
        return []
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    for a in soup.select('a[href^="/genre/"]'):
        href = a.get("href") or ""
        m = re.match(r"^/genre/(\d+-[a-z0-9-]+)/?$", href if isinstance(href, str) else "")
        if not m:
            continue
        slug = m.group(1)
        name = a.get_text(strip=True)
        # The page emits a "View More" link to the same genre after
        # the named one — skip it and any later repeats.
        if not name or name.lower() == "view more" or slug in seen:
            continue
        seen.add(slug)
        out.append({"slug": slug, "name": name})
    out.sort(key=lambda g: g["name"].lower())
    _cache_set("genre-index", out)
    return out


def recent_releases_by_genre(genre_slug: str, limit: int = 60) -> list[dict]:
    """Recent albums for one genre, from the "Recent {Genre} Albums"
    section of `/genre/{slug}/`.

    The genre page stacks several `<div class="section">` blocks
    (Critics' Highest Rated, Users' Highest Rated, Recent, …), each
    introduced by an `<h2 class="subHeadline">`. We scope parsing to
    the section whose header reads "Recent … Albums" so this returns
    new releases rather than the genre's all-time canon. The cards
    are the same `albumBlock` shape as `/releases/this-week/`, so the
    existing card parser handles them once the section is isolated.
    """
    limit = max(1, min(int(limit), 100))
    if not _GENRE_SLUG_RE.match(genre_slug or ""):
        log.warning("aoty: rejecting malformed genre slug %r", genre_slug)
        return []
    cache_key = f"genre-recent:{genre_slug}:{limit}"
    cached = _cache_get(cache_key, _GENRE_RELEASES_TTL_SEC)
    if cached is not None:
        return cached

    html = _fetch(urljoin(_BASE_URL, f"/genre/{genre_slug}/"))
    if html is None:
        _cache_set(cache_key, [])
        return []
    soup = BeautifulSoup(html, "html.parser")
    section = None
    for h2 in soup.select("h2.subHeadline"):
        if re.search(r"recent\b.*\balbums", h2.get_text(" ", strip=True), re.I):
            section = h2.find_parent("div", class_="section")
            break
    if section is None:
        # Layout changed or the genre has no recent section — empty
        # rather than falling back to the whole page (which would mix
        # in the all-time canon and mislabel it as "new").
        _cache_set(cache_key, [])
        return []
    rows = _parse_album_block_cards(str(section))[:limit]
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
        # No `headers=` kwarg. curl_cffi's impersonate profile sets
        # the entire Chrome-120 header set (UA, every sec-ch-ua-*,
        # Accept, Accept-Language, header order) consistent with the
        # TLS hello it sends. Overriding any of those — even a
        # nominally identical User-Agent — breaks Cloudflare's
        # cross-check and earns a 403.
        r = cffi_requests.get(
            url,
            impersonate=_CFFI_IMPERSONATE,
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

        # AOTY tags each row with one or more genre links
        # (`div.albumListGenre > a[href^="/genre/"]`). Capture the
        # display name and the slug from the href in lockstep so the
        # picker can both label and fetch a genre. De-duped by name,
        # original order preserved.
        genres: list[str] = []
        genre_slugs: list[str] = []
        for g in row.select("div.albumListGenre a"):
            name = g.get_text(strip=True)
            if not name or name in genres:
                continue
            href = g.get("href") or ""
            m = re.match(
                r"^/genre/(\d+-[a-z0-9-]+)/?$",
                href if isinstance(href, str) else "",
            )
            genres.append(name)
            genre_slugs.append(m.group(1) if m else "")

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
                genres=genres,
                genre_slugs=genre_slugs,
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

        # AOTY's release cards carry up to two `.ratingRow` children:
        # one labelled "critic score" and one labelled "user score".
        # On `/releases/this-week/` both are present once a release has
        # accumulated reviews, and they sit in that order: critic
        # first, user second. We deliberately pick the user-score row
        # to match the rest of the app: `Top albums of <year>` reads
        # AOTY's user-rating chart, the album quality badges on detail
        # pages reflect Tidal listener tags, etc. Showing a critic
        # average on the New Releases row was a parser regression
        # (the fixture for this test only carried the user row, so the
        # `.rating` first-match selector worked there but pulled the
        # critic average in production).
        score: Optional[int] = None
        rating_count: Optional[int] = None
        for row in block.select(".ratingRow"):
            text_els = row.select(".ratingText")
            label = (
                text_els[0].get_text(strip=True).lower() if text_els else ""
            )
            if "user" not in label:
                continue
            rating = row.select_one(".rating")
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
            # The second `.ratingText` carries the count, e.g. "(304)"
            # or "(2,024)".
            if len(text_els) >= 2:
                rating_count = _parse_rating_count(text_els[1])
            break

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
