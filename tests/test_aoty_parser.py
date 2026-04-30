"""Tests for the AOTY HTML parsers in `app.aoty`.

The parser is the most fragile part of the AOTY integration —
AOTY is HTML-only and they don't commit to a stable layout, so a
silent regression here would just blank the Home-page section
without any visible error. These tests pin the row shape we
currently parse against minimal inline HTML fixtures, so a layout
change comes back as a clear test failure with a hint at what
moved.

If AOTY's HTML changes: re-grab a row from the live page, replace
the corresponding fixture string, and update the expected values.
"""
from __future__ import annotations

from app.aoty import (
    _parse_album_block_cards,
    _parse_album_list_rows,
    _split_artist_title,
)


# Fixture: one row from /ratings/user-highest-rated/2026/, captured
# 2026-04-30. Trimmed to the structurally-relevant elements +
# stripped of inline external links + tracking attributes.
_RATING_ROW_HTML = """
<div class="albumListRow">
  <h2 class="albumListTitle">
    <span itemprop="itemListElement" itemscope itemtype="http://schema.org/ListItem">
      <span class="albumListRank">
        <span itemprop="position">1</span>.
      </span>
      <span itemprop="item" itemscope itemtype="http://schema.org/MusicAlbum">
        <a href="/album/1460350-slayyyter-wort-girl-in-america.php" itemprop="url">Slayyyter - WOR$T GIRL IN AMERICA</a>
        <meta itemprop="name" content="Slayyyter - WOR$T GIRL IN AMERICA" />
      </span>
    </span>
  </h2>
  <div class="albumListCover mustHear user">
    <a href="/album/1460350-slayyyter-wort-girl-in-america.php">
      <div class="mustHear"><i class="fas fa-star"></i></div>
      <img src="https://cdn2.albumoftheyear.org/200x0/album/1460350-wort-girl-in-america_034044.jpg" alt="Slayyyter - WOR$T GIRL IN AMERICA"/>
    </a>
  </div>
  <div class="albumListDate">March 27, 2026</div>
  <div class="albumListGenre">
    <a href="/genre/181-electroclash/">Electroclash</a>,
    <a href="/genre/31-electropop/">Electropop</a>
  </div>
  <div class="albumListScoreContainer">
    <div class="scoreHeader">USER SCORE</div>
    <div class="scoreValueContainer" title="83.9">
      <div class="scoreValue">84</div>
    </div>
    <div class="scoreText">12,598 ratings</div>
  </div>
</div>
"""

# Fixture: one card from /releases/, captured 2026-04-30. Same
# trimming policy.
_RELEASE_BLOCK_HTML = """
<div class="albumBlock five" data-type="ep">
  <div class="image">
    <a href="/album/1728324-illit-mamihlapinatapai.php">
      <img src="https://cdn2.albumoftheyear.org/200x0/album/1728324-mamihlapinatapai_091400.jpg" alt="ILLIT - MAMIHLAPINATAPAI"/>
    </a>
  </div>
  <a href="/artist/319705-illit/"><div class="artistTitle">ILLIT</div></a>
  <a href="/album/1728324-illit-mamihlapinatapai.php"><div class="albumTitle">MAMIHLAPINATAPAI</div></a>
  <div class="type">Apr 30 · EP</div>
  <div class="ratingRowContainer">
    <div class="ratingRow">
      <div class="ratingBlock">
        <div class="rating">63</div>
        <div class="ratingBar yellow"><div class="yellow" style="width:63%;"></div></div>
      </div>
      <div class="ratingText">user score</div>
      <div class="ratingText">(304)</div>
    </div>
  </div>
</div>
"""


# --- top-of-year row parser -------------------------------------------------


def test_parse_top_row_extracts_all_fields():
    rows = _parse_album_list_rows(_RATING_ROW_HTML)
    assert len(rows) == 1
    a = rows[0]
    assert a.rank == 1
    assert a.artist == "Slayyyter"
    assert a.title == "WOR$T GIRL IN AMERICA"
    assert a.score == 84
    assert a.rating_count == 12598
    assert a.cover_url and "wort-girl-in-america" in a.cover_url
    assert a.release_date == "March 27, 2026"
    assert a.must_hear is True
    assert a.aoty_url and a.aoty_url.startswith(
        "https://www.albumoftheyear.org/album/"
    )


def test_parse_top_row_handles_missing_must_hear_flag():
    """A row without the 'mustHear' class should still parse, just
    with `must_hear=False`."""
    html = _RATING_ROW_HTML.replace("albumListCover mustHear user", "albumListCover")
    rows = _parse_album_list_rows(html)
    assert len(rows) == 1
    assert rows[0].must_hear is False


def test_parse_top_row_handles_missing_score():
    """Rows for very-recently-released albums sometimes have an
    empty / non-numeric score block. Parser should return None
    rather than raise."""
    html = _RATING_ROW_HTML.replace(
        '<div class="scoreValue">84</div>',
        '<div class="scoreValue">N/A</div>',
    )
    rows = _parse_album_list_rows(html)
    assert len(rows) == 1
    assert rows[0].score is None


def test_parse_top_row_skips_malformed_rows():
    """A row that's missing the title anchor entirely (defensive
    against future markup changes) should be silently skipped, not
    crash the whole list."""
    broken = "<div class='albumListRow'><div>nothing useful</div></div>"
    rows = _parse_album_list_rows(broken)
    assert rows == []


# --- recent-releases card parser --------------------------------------------


def test_parse_release_card_extracts_all_fields():
    rows = _parse_album_block_cards(_RELEASE_BLOCK_HTML)
    assert len(rows) == 1
    a = rows[0]
    assert a.artist == "ILLIT"
    assert a.title == "MAMIHLAPINATAPAI"
    assert a.score == 63
    assert a.rating_count == 304
    # The middle-dot in "Apr 30 · EP" is a UTF-8 encoded U+00B7.
    # If the encoding fix in `_fetch` regresses, this character
    # comes through as U+FFFD (the replacement marker) and the
    # assertion below catches it.
    assert "·" in (a.release_date or "")
    assert a.cover_url and "mamihlapinatapai" in a.cover_url
    assert a.must_hear is False  # /releases/ never sets this flag
    assert a.rank is None


def test_parse_release_card_treats_zero_score_as_none():
    """Brand-new releases sometimes ship with a 0 in the rating
    slot before AOTY has a real number. Surface that as `None` so
    the UI doesn't render a misleading 0/100."""
    html = _RELEASE_BLOCK_HTML.replace(
        '<div class="rating">63</div>', '<div class="rating">0</div>'
    )
    rows = _parse_album_block_cards(html)
    assert len(rows) == 1
    assert rows[0].score is None


def test_parse_release_card_handles_missing_cover():
    """Unreleased albums sometimes have a `noCover` block instead
    of an <img>. The parser should still emit the row with
    `cover_url=None`."""
    html = _RELEASE_BLOCK_HTML.replace(
        '<img src="https://cdn2.albumoftheyear.org/200x0/album/1728324-mamihlapinatapai_091400.jpg" '
        'alt="ILLIT - MAMIHLAPINATAPAI"/>',
        '<div class="noCover"><i class="fa-light fa-lock"></i></div>',
    )
    rows = _parse_album_block_cards(html)
    assert len(rows) == 1
    assert rows[0].cover_url is None
    assert rows[0].artist == "ILLIT"


# --- the artist/title splitter ----------------------------------------------


def test_split_artist_title_basic():
    assert _split_artist_title("Slayyyter - WOR$T GIRL IN AMERICA") == (
        "Slayyyter",
        "WOR$T GIRL IN AMERICA",
    )


def test_split_artist_title_preserves_inner_hyphens():
    """Splits on the first ' - ' so titles with their own hyphens
    survive intact."""
    assert _split_artist_title("Beck - Sea Change - Reissue") == (
        "Beck",
        "Sea Change - Reissue",
    )


def test_split_artist_title_missing_separator():
    """If AOTY ever ships a title without the ' - ' separator,
    return ('', combined) so the caller's empty-artist guard kicks
    in and the row is skipped."""
    assert _split_artist_title("just a title") == ("", "just a title")


def test_split_artist_title_empty():
    assert _split_artist_title("") == ("", "")
