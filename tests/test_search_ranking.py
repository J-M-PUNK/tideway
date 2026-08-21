"""Tests for the search reranker.

The motivating bug: searching "ear" surfaced "Earth, Wind & Fire"
(popular prefix) and never the band "ear" (exact). The contract is
a strict lexical-class hierarchy — exact > prefix > all-words >
substring > none — that nothing within a class (popularity, the
user's taste) can cross, with taste + popularity ordering ties.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from app import search_ranking as sr


def _artist(name, pop=0):
    return SimpleNamespace(name=name, popularity=pop)


def _album(title, by, pop=0):
    return SimpleNamespace(name=title, popularity=pop, by=by)


def _rank(query, items, taste=frozenset()):
    out = sr.rerank(
        query,
        items,
        get_name=lambda a: a.name,
        get_popularity=lambda a: a.popularity,
        taste=taste,
    )
    return [a.name for a in out]


def _rank_albums(query, items, taste=frozenset()):
    """Rerank the way /api/search does for albums and tracks: the
    credited artist is passed alongside the title."""
    out = sr.rerank(
        query,
        items,
        get_name=lambda a: a.name,
        get_popularity=lambda a: a.popularity,
        get_artist_names=lambda a: [a.by],
        taste=taste,
    )
    return [a.name for a in out]


# --- normalization ---------------------------------------------------------


def test_normalize_folds_punctuation_diacritics_and_leading_the():
    assert sr.normalize("Earth, Wind & Fire") == "earth wind fire"
    assert sr.normalize("EAR.") == "ear"
    assert sr.normalize("  The   Beatles ") == "beatles"
    assert sr.normalize("Beyoncé") == "beyonce"
    assert sr.normalize("") == ""


# --- the motivating case ---------------------------------------------------


def test_exact_band_beats_popular_prefix():
    items = [
        _artist("Earth, Wind & Fire", pop=92),
        _artist("Earl Sweatshirt", pop=78),
        _artist("ear", pop=3),
    ]
    ranked = _rank("ear", items)
    assert ranked[0] == "ear"  # exact wins despite lowest popularity


# --- class hierarchy is absolute ------------------------------------------


def test_class_hierarchy_dominates_popularity_and_taste():
    items = [
        _artist("earshot", pop=100),  # prefix, max popularity
        _artist("the ear collective", pop=100),  # 'ear' is a word
        _artist("gear", pop=100),  # substring only
        _artist("ear", pop=0),  # exact, zero popularity, not in taste
    ]
    # Even with everything stacked against it, exact is first; then
    # prefix, then whole-word, then substring.
    ranked = _rank("ear", items, taste=frozenset({"earshot"}))
    assert ranked == [
        "ear",
        "earshot",
        "the ear collective",
        "gear",
    ]


def test_popularity_orders_within_a_class():
    items = [
        _artist("earl grey", pop=10),
        _artist("earl sweatshirt", pop=90),
    ]
    # Both are prefix matches for "earl"; popularity breaks the tie.
    assert _rank("earl", items) == ["earl sweatshirt", "earl grey"]


def test_taste_breaks_ties_but_not_classes():
    items = [
        _artist("earth", pop=50),  # prefix, not in taste
        _artist("earl", pop=50),  # prefix, in taste
    ]
    taste = frozenset({"earl"})
    # Same class + popularity -> the artist you listen to wins.
    assert _rank("ear", items, taste=taste) == ["earl", "earth"]
    # But taste never lifts a prefix over someone else's exact.
    items2 = [_artist("earth", pop=99), _artist("ear", pop=0)]
    assert _rank("ear", items2, taste=frozenset({"earth"}))[0] == "ear"


def test_non_matches_kept_at_tail_in_original_order():
    items = [
        _artist("zzz one", pop=0),
        _artist("ear", pop=0),
        _artist("zzz two", pop=0),
    ]
    ranked = _rank("ear", items)
    assert ranked[0] == "ear"
    assert ranked[1:] == ["zzz one", "zzz two"]  # fuzzy tail preserved


def test_empty_query_returns_input_unchanged():
    items = [_artist("b"), _artist("a")]
    assert _rank("", items) == ["b", "a"]


# --- taste index: cached + background-refreshed ----------------------------


def test_get_taste_is_async_and_caches():
    sr._reset_taste_for_tests()
    calls = []

    def build():
        calls.append(1)
        return ["Radiohead", "The XX", "Bonobo"]

    # First call: empty (build runs off-thread), kicks the refresh.
    assert sr.get_taste(build) == frozenset()
    for _ in range(50):
        if sr.get_taste(build):
            break
        time.sleep(0.02)
    got = sr.get_taste(build)
    assert got == frozenset({"radiohead", "xx", "bonobo"})
    # Still warm -> no further builds within the TTL.
    sr.get_taste(build)
    assert sum(calls) == 1
    sr._reset_taste_for_tests()


def test_get_taste_survives_build_failure():
    sr._reset_taste_for_tests()

    def boom():
        raise RuntimeError("favourites page 500")

    assert sr.get_taste(boom) == frozenset()
    time.sleep(0.2)
    # Failure leaves an empty set rather than crashing the caller.
    assert sr.get_taste(boom) == frozenset()
    sr._reset_taste_for_tests()


# --- artist-shaped queries against titled entities -------------------------
#
# An artist name is an ordinary thing to type into a search box, and
# scoring a track or album on its title alone gets it backwards:
# searching "radiohead" put six unrelated albums literally titled
# "Radiohead" above OK Computer, because the title scored EXACT and
# the real record scored nothing at all. The class is now the better
# of the title's match and the credited artist's.


def test_an_album_by_the_searched_artist_beats_one_merely_named_for_them():
    items = [
        _album("Radiohead", by="Anchor + Bell", pop=2),
        _album("radiohead", by="Nightly", pop=5),
        _album("OK Computer", by="Radiohead", pop=87),
        _album("In Rainbows", by="Radiohead", pop=89),
    ]
    ranked = _rank_albums("radiohead", items)
    assert ranked[:2] == ["In Rainbows", "OK Computer"]


def test_a_title_query_still_puts_the_title_first():
    """The artist signal must not drown the case it was already
    getting right."""
    items = [
        _album("Kid A", by="Radiohead", pop=87),
        _album("In Rainbows", by="Radiohead", pop=89),
    ]
    assert _rank_albums("kid a", items)[0] == "Kid A"


def test_the_artist_signal_respects_the_class_hierarchy():
    """A credited artist matching at a weaker class can't overtake a
    stronger title match, however popular the artist."""
    items = [
        # Artist is a substring match only.
        _album("Something Else", by="Radiohead Tribute Band", pop=99),
        # Title is an exact match.
        _album("Radiohead", by="Nobody", pop=1),
    ]
    assert _rank_albums("radiohead", items)[0] == "Radiohead"


def test_popularity_still_orders_a_title_and_artist_tie():
    items = [
        _album("Drake", by="Someone Else", pop=10),
        _album("Views", by="Drake", pop=95),
    ]
    assert _rank_albums("drake", items)[0] == "Views"


def test_artists_are_unaffected_by_the_artist_signal():
    """Artist rescoring passes no credited names — the name already is
    the artist — so its behavior is unchanged."""
    items = [_artist("Earth, Wind & Fire", pop=92), _artist("ear", pop=3)]
    assert _rank("ear", items)[0] == "ear"


def test_a_missing_artist_credit_falls_back_to_the_title():
    items = [
        _album("Creep", by="", pop=50),
        _album("Something", by="", pop=90),
    ]
    assert _rank_albums("creep", items)[0] == "Creep"
