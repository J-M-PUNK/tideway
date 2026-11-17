"""A Tidal search hit has to actually be the album AOTY listed.

Reported from the Top Albums of the Year row on Home: the first card
was Vince Staples' "Big Fish Theory" — a 2017 album, in a 2026 chart.
AOTY had listed "ii" by Denzel Curry & Kenneth Blume. Tidal's search
for that string returned nothing matching, and the resolver ended with

    resolved = album_to_dict(exact or albums[0])

so it took the top hit unconditionally. Tidal returns *something* for
almost any query, which is how a different artist, a different title
and a nine-year-old record ended up presented as this year's number
one.

A row one album shorter is better than a row with the wrong album in
it, so an implausible hit now resolves to None and callers drop it —
they already handle unresolved entries.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

from app.aoty_resolver import _is_plausible_match, _norm, _similar, _year


def _album(name: str, *artists: str):
    return NS(name=name, artists=[NS(name=a) for a in artists])


def test_the_reported_mismatch_is_rejected():
    got = _album("Big Fish Theory", "Vince Staples")

    assert not _is_plausible_match(got, "Denzel Curry & Kenneth Blume", "ii")


def test_the_right_album_is_accepted():
    got = _album("ii", "Denzel Curry")

    assert _is_plausible_match(got, "Denzel Curry & Kenneth Blume", "ii")


def test_edition_suffixes_still_match():
    """Tidal routinely carries "(Deluxe Edition)" where AOTY does not.
    Rejecting those would empty the row for the opposite reason."""
    got = _album("Marrow Deep (Deluxe Edition)", "Mastodon")

    assert _is_plausible_match(got, "Mastodon", "Marrow Deep")


def test_case_and_punctuation_differences_still_match():
    got = _album("WOR$T GIRL IN AMERICA", "Slayyyter")

    assert _is_plausible_match(got, "slayyyter", "WOR$T GIRL IN AMERICA")


def test_same_title_by_a_different_artist_is_rejected():
    """The dangerous near-miss: short generic titles collide across
    artists, which is how "ii" found something at all."""
    got = _album("Remains", "Some Other Band")

    assert not _is_plausible_match(got, "Tokyo Jihen", "Remains")


def test_an_unreadable_artist_falls_back_to_the_title():
    """Some Tidal hits carry no usable artist list. A close title is
    then the only signal there is — better than discarding a correct
    match, and the title bar is high."""
    got = NS(name="Lost Weekend", artists=[])

    assert _is_plausible_match(got, "Phoebe Bridgers", "Lost Weekend")


def test_norm_strips_a_trailing_bracketed_tag():
    assert _norm("Big Fish Theory (Deluxe Edition)") == "big fish theory"
    assert _norm("  Inferno  ") == "inferno"


def test_similar_is_a_percentage():
    assert _similar("Inferno", "Inferno") == 100.0
    assert _similar("Inferno", "Big Fish Theory") < 50.0


def test_norm_keeps_a_subtitle_that_is_part_of_the_title():
    """Only edition tags are dropped. "(The EP)" is the title of a 2026
    release, and dropping it matched the 1976 album of the same name."""
    assert _norm("Songs in the Key of Life (The EP)") == "songs in the key of life the ep"
    assert _norm("Songs In The Key Of Life - The EP") == _norm(
        "Songs in the Key of Life (The EP)"
    )
    assert _norm("Abbey Road - Remastered 2009") == "abbey road"
    assert _norm("Kid A [Expanded]") == "kid a"


def test_an_album_from_years_before_is_rejected():
    old = NS(name="Songs In The Key Of Life", artists=[NS(name="Stevie Wonder")], year=1976)
    ep = NS(
        name="Songs In The Key Of Life - The EP", artists=[NS(name="Stevie Wonder")], year=2026
    )
    want = "Songs in the Key of Life (The EP)"
    assert not _is_plausible_match(old, "Stevie Wonder", want, 2026)
    assert _is_plausible_match(ep, "Stevie Wonder", want, 2026)
    # A year of slack for pre-releases and catalog date differences.
    late = NS(name="Record", artists=[NS(name="Band")], year=2025)
    assert _is_plausible_match(late, "Band", "Record", 2026)


def test_year_reads_aoty_and_tidal_dates():
    assert _year("September 28, 2026") == 2026
    assert _year("2026-09-28 00:00:00") == 2026
    assert _year(1976) == 1976
    # AOTY's this-week cards carry no year; no check then.
    assert _year("Sep 28 · EP") is None
