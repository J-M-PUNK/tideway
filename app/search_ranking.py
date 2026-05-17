"""Relevance + interest-aware reranking for /api/search.

Tidal ranks search by its own popularity-skewed relevance, which
buries an exact match (the band "ear") under a popular near-prefix
("Earth, Wind & Fire"). We rescore every candidate so the lexical
match class is the primary key:

    exact  >  prefix  >  all-query-words  >  substring  >  no match

The class bases are spaced far enough apart that nothing within a
class (popularity, the user's taste) can ever cross a class
boundary. So a literal "ear" always beats a popular prefix, while
among results of the same class the artists you actually listen to
and the more popular ones float up — which is what makes an
ambiguous query feel like it read your mind.

The taste signal is the set of artists you listen to (Tidal
favourites + Last.fm top artists). Building it hits the network, so
it is cached and refreshed on a background thread; the search
request itself never blocks on it and simply runs lexical-only
until the first build lands.
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from typing import Callable, Iterable, Optional

# Each class strictly dominates the next: max within-class bonus
# (popularity 50 + taste 25 = 75) is far below the gap to the next
# class, so ordering is lexical-class-first, always.
_EXACT = 100_000.0
_PREFIX = 10_000.0
_WORDS = 1_000.0
_SUBSTRING = 100.0
_NONE = 0.0

_POP_WEIGHT = 50.0   # popularity 0..1 -> 0..50, in-class tiebreak
_TASTE_BONUS = 25.0  # in-class lift for an artist you listen to

_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize(s: Optional[str]) -> str:
    """Fold a name to a match key: strip diacritics, lowercase,
    reduce punctuation to spaces, drop a leading "the". So
    "Earth, Wind & Fire" -> "earth wind fire" and "EAR." -> "ear"."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _NON_WORD.sub(" ", s.lower()).strip()
    s = _WS.sub(" ", s)
    if s.startswith("the "):
        s = s[4:]
    return s


def _match_class(query_norm: str, name_norm: str) -> float:
    if not query_norm or not name_norm:
        return _NONE
    if name_norm == query_norm:
        return _EXACT
    # Prefix: the name starts with the whole query. Single-char
    # queries can't earn a prefix class (too noisy); an exact
    # single char already returned above.
    if len(query_norm) >= 2 and name_norm.startswith(query_norm):
        return _PREFIX
    q_tokens = query_norm.split()
    if q_tokens:
        name_tokens = set(name_norm.split())
        if all(t in name_tokens for t in q_tokens):
            return _WORDS
    if query_norm in name_norm:
        return _SUBSTRING
    return _NONE


def _popularity_component(popularity) -> float:
    try:
        p = float(popularity or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if p < 0.0:
        p = 0.0
    if p > 100.0:
        p = 100.0
    return (p / 100.0) * _POP_WEIGHT


def score(
    query_norm: str,
    name: str,
    popularity=0,
    artist_names: Iterable[str] = (),
    taste: frozenset[str] = frozenset(),
) -> float:
    """Score one candidate. `name` is the entity's own name (artist
    name, track/album title); `artist_names` are the names of the
    artists credited on it (so a track by an artist you listen to
    gets the taste lift even when the title itself doesn't)."""
    name_norm = normalize(name)
    base = _match_class(query_norm, name_norm)
    total = base + _popularity_component(popularity)
    if taste:
        if name_norm in taste or any(
            normalize(a) in taste for a in artist_names
        ):
            total += _TASTE_BONUS
    return total


def rerank(
    query: str,
    items: list,
    *,
    get_name: Callable[[object], str],
    get_popularity: Callable[[object], object] = lambda _x: 0,
    get_artist_names: Callable[[object], Iterable[str]] = lambda _x: (),
    taste: frozenset[str] = frozenset(),
) -> list:
    """Stable-sort `items` by score, best first. Non-matching items
    keep their original relative order at the tail rather than being
    dropped — a fuzzy Tidal match still beats nothing for a typo."""
    if not items:
        return items
    q = normalize(query)
    if not q:
        return items
    scored = []
    for idx, it in enumerate(items):
        try:
            s = score(
                q,
                get_name(it) or "",
                get_popularity(it),
                get_artist_names(it),
                taste,
            )
        except Exception:
            s = _NONE
        scored.append((-s, idx, it))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [it for _s, _i, it in scored]


def best_class(query: str, name: str) -> float:
    """The lexical class a name would land in, for top-hit picking."""
    return _match_class(normalize(query), normalize(name))


# Class bases exported so callers can compare top-hit candidates.
CLASS_EXACT = _EXACT
CLASS_PREFIX = _PREFIX
CLASS_WORDS = _WORDS
CLASS_SUBSTRING = _SUBSTRING
CLASS_NONE = _NONE


# ---------------------------------------------------------------------------
# Taste index: cached, background-refreshed. Never blocks a search.
# ---------------------------------------------------------------------------

_TASTE_TTL_SEC = 900.0  # 15 min — favourites/top-artists drift slowly
_taste_lock = threading.Lock()
_taste_state = {
    "names": frozenset(),  # type: frozenset[str]
    "built_at": 0.0,
    "refreshing": False,
}


def get_taste(build: Callable[[], Iterable[str]]) -> frozenset:
    """Return the current taste set (possibly empty) and, if it's
    stale or never built, kick a background rebuild. `build` returns
    raw artist names; it runs off-thread so a slow favourites page
    fetch can't stall search or hammer Tidal on the request path."""
    now = time.time()
    with _taste_lock:
        names = _taste_state["names"]
        never = _taste_state["built_at"] == 0.0
        stale = (now - _taste_state["built_at"]) > _TASTE_TTL_SEC
        if (never or stale) and not _taste_state["refreshing"]:
            _taste_state["refreshing"] = True
            threading.Thread(
                target=_refresh_taste,
                args=(build,),
                name="search-taste-refresh",
                daemon=True,
            ).start()
    return names


def _refresh_taste(build: Callable[[], Iterable[str]]) -> None:
    new_names: Optional[frozenset] = None
    try:
        raw = build() or ()
        new_names = frozenset(
            n for n in (normalize(x) for x in raw) if n
        )
    except Exception:
        new_names = None  # keep the previous set; just retry next TTL
    with _taste_lock:
        if new_names is not None:
            _taste_state["names"] = new_names
        _taste_state["built_at"] = time.time()
        _taste_state["refreshing"] = False


def _reset_taste_for_tests() -> None:
    with _taste_lock:
        _taste_state["names"] = frozenset()
        _taste_state["built_at"] = 0.0
        _taste_state["refreshing"] = False
