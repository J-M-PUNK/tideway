"""Tests for the expanded filename-template engine.

Covers token interpolation, `/`-as-directory-separator, per-segment
sanitization, path-traversal protection, and the backward-compat
gating of `create_album_folders` against templates that already
declare their own structure.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.downloader import (
    DownloadItem,
    _build_path,
    _explicit_marker,
    _render_template,
    _split_template_path,
    _template_has_separator,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _item(**overrides) -> DownloadItem:
    base = dict(
        item_id="abc",
        url="",
        title="Song Title",
        artist="Track Artist",
        album="Album Name",
        track_num=3,
        album_artist="Album Artist",
        year=2024,
        disc_num=1,
        track_explicit=False,
        album_explicit_flag=False,
    )
    base.update(overrides)
    return DownloadItem(**base)


def _settings(template, *, output_dir, create_album_folders=True):
    return SimpleNamespace(
        filename_template=template,
        output_dir=str(output_dir),
        create_album_folders=create_album_folders,
    )


# ---------------------------------------------------------------------------
# _render_template — token interpolation
# ---------------------------------------------------------------------------


def test_render_supports_legacy_tokens():
    """Pre-existing tokens keep working — no behavior change for users
    who never touched the default template."""
    item = _item(track_num=7, title="Hello", artist="Adele", album="25")
    assert _render_template("{artist} - {title}", item) == "Adele - Hello"
    assert _render_template("{track_num} {title}", item) == "07 Hello"
    assert _render_template("{album}/{title}", item) == "25/Hello"


def test_render_supports_new_tokens():
    item = _item(
        title="Sicko Mode",
        album="Astroworld",
        album_artist="Travis Scott",
        artist="Travis Scott, Drake",
        year=2018,
        disc_num=1,
        track_num=2,
        track_explicit=True,
        album_explicit_flag=True,
    )
    rendered = _render_template(
        "{album_artist} - {year}/{album}{album_explicit}/{track_num} {title}{explicit}",
        item,
    )
    assert rendered == (
        "Travis Scott - 2018/Astroworld [E]/02 Sicko Mode [E]"
    )


def test_render_track_title_alias_matches_title():
    """`{track_title}` is offered as an alias for `{title}` because
    other downloaders use that name — keeps configs portable."""
    item = _item(title="Hello")
    assert _render_template("{track_title}", item) == "Hello"
    assert _render_template("{album_title}", item) == _render_template("{album}", item)


def test_render_year_empty_when_unknown():
    """Missing year shouldn't write the literal string 'None' into a
    folder name — render as empty, let the template author decide
    how to handle that gap."""
    item = _item(year=None)
    assert _render_template("{year}", item) == ""
    assert _render_template("{album} ({year})", item) == "Album Name ()"


def test_render_explicit_marker_only_when_flagged():
    item_clean = _item(track_explicit=False, album_explicit_flag=False)
    item_explicit = _item(track_explicit=True, album_explicit_flag=True)
    assert _render_template("{title}{explicit}", item_clean) == "Song Title"
    assert _render_template("{title}{explicit}", item_explicit) == "Song Title [E]"
    assert _render_template("{album}{album_explicit}", item_clean) == "Album Name"
    assert (
        _render_template("{album}{album_explicit}", item_explicit) == "Album Name [E]"
    )


def test_render_album_artist_falls_back_to_track_artist():
    """If we don't have a separate album_artist (single-track submit
    without an album lookup), don't blank the field — fall back to
    the track artist so the template doesn't render a void where the
    user expected a name."""
    item = _item(album_artist="", artist="Solo Artist")
    assert _render_template("{album_artist}/{title}", item) == "Solo Artist/Song Title"


def test_render_unknown_token_kept_literal():
    """A typo in the user's template must not crash the download.
    Render the unknown key as `{key}` so the user spots the bad
    filename and fixes their template — silently dropping it would
    just produce confusingly-named files."""
    item = _item()
    assert _render_template("{albmu}/{title}", item) == "{albmu}/Song Title"


def test_render_token_value_with_slash_does_not_create_directory():
    """The whole point of sanitizing token values BEFORE rendering: a
    literal `/` in tidalapi-supplied data (band name "AC/DC") must
    not split into two directories. The template's own `/` is what
    creates structure, never the data."""
    item = _item(album="AC/DC", artist="AC/DC")
    rendered = _render_template("{artist} - {album}", item)
    assert rendered == "AC_DC - AC_DC"


# ---------------------------------------------------------------------------
# _split_template_path & _template_has_separator
# ---------------------------------------------------------------------------


def test_split_drops_empty_segments_from_leading_or_doubled_slash():
    """A leading `/` would otherwise root the path off the user's
    output dir. A `//` from a typo'd template would yield a `.`
    segment. Both get filtered."""
    assert _split_template_path("/a/b/c") == ["a", "b", "c"]
    assert _split_template_path("a//b") == ["a", "b"]


def test_split_accepts_backslash_too():
    """Windows users naturally type `\\` — accept it as a separator
    so the same template works across platforms."""
    assert _split_template_path("a\\b/c") == ["a", "b", "c"]


def test_template_has_separator_ignores_slashes_inside_tokens():
    """An album literally named "AC/DC" expands to a single segment;
    that user-data slash doesn't count as a structural separator."""
    assert not _template_has_separator("{album} - {title}")
    assert _template_has_separator("{album}/{title}")
    # Token with literal `/` in its name (impossible in our docs but
    # worth pinning the behaviour): the regex strips the whole `{...}`
    # so what's inside doesn't count.
    assert not _template_has_separator("{album/with/slash}")


# ---------------------------------------------------------------------------
# _build_path — full-path assembly
# ---------------------------------------------------------------------------


def test_build_path_single_segment_with_album_folder_toggle(tmp_path):
    """Default behaviour: flat template + create_album_folders=True
    nests the file under an album folder."""
    item = _item(album="Astroworld", artist="Travis Scott", title="Sicko Mode")
    settings = _settings(
        "{artist} - {title}", output_dir=tmp_path, create_album_folders=True
    )

    out = _build_path(item, settings, ".flac")

    assert out == tmp_path / "Astroworld" / "Travis Scott - Sicko Mode.flac"


def test_build_path_template_with_separator_disables_album_folder_shortcut(tmp_path):
    """Once the user adopts a multi-segment template, `create_album_folders`
    is a no-op — otherwise we'd get duplicate album folders nested
    inside the template's own structure."""
    item = _item(album="Astroworld", title="Sicko Mode")
    settings = _settings(
        "{album_artist}/{album}/{track_num} {title}",
        output_dir=tmp_path,
        create_album_folders=True,  # would double-nest if respected
    )

    out = _build_path(item, settings, ".flac")

    # No leading "Astroworld/" — only the template's structure.
    assert out == (
        tmp_path / "Album Artist" / "Astroworld" / "03 Sicko Mode.flac"
    )


def test_build_path_per_segment_sanitization(tmp_path):
    """A token value with reserved chars must collapse to `_` inside
    its own segment — and only its own segment. The surrounding
    template structure stays intact."""
    item = _item(album="A:B*C", title='He said "hi"')
    settings = _settings(
        "{album}/{title}", output_dir=tmp_path, create_album_folders=False
    )

    out = _build_path(item, settings, ".flac")

    assert out == tmp_path / "A_B_C" / "He said _hi_.flac"


def test_build_path_blocks_traversal_via_token_value(tmp_path):
    """A title of `../escape` is sanitized at the token-value step
    (the slash becomes `_`), so the `..` can't reach the resolver."""
    item = _item(title="../escape", album="A")
    settings = _settings("{title}", output_dir=tmp_path, create_album_folders=False)

    out = _build_path(item, settings, ".flac")

    # The `/` in `../escape` was sanitized to `_` before render —
    # the path stays under output_dir.
    assert tmp_path in out.resolve().parents
    assert ".." not in out.parts


def test_build_path_blocks_absolute_template(tmp_path):
    """A maliciously absolute template (e.g. `/etc/passwd`) splits
    into segments after the leading slash is dropped. Resulting path
    stays under output_dir — we never fall through to the OS root."""
    item = _item(title="x")
    settings = _settings(
        "/etc/passwd/{title}", output_dir=tmp_path, create_album_folders=False
    )

    out = _build_path(item, settings, ".flac")

    assert tmp_path in out.resolve().parents


def test_build_path_falls_back_when_template_renders_empty(tmp_path):
    """Pathological template: every token is empty. We must still
    write *somewhere* under output_dir rather than crash with a
    zero-length filename or — worse — write to the dir itself."""
    item = _item(title="", artist="", album="", album_artist="", year=None)
    settings = _settings(
        "{year}/{album}", output_dir=tmp_path, create_album_folders=False
    )

    out = _build_path(item, settings, ".flac")

    # Some safe fallback under output_dir.
    assert tmp_path in out.parents or out.parent == tmp_path
    assert out.suffix == ".flac"


# ---------------------------------------------------------------------------
# Non-ASCII titles — issues #7, #36, #70
#
# Polish / French / typographic characters in track titles must round-
# trip through the template renderer untouched. The sanitizer only
# strips Windows-illegal chars + control bytes; everything else must
# pass through. Tidal track titles routinely contain accented Latin,
# Cyrillic, CJK, em-dashes, smart quotes, etc.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        # Polish (puhszkin's reports — #7, #36)
        "Życie",
        "Mała czarna sukienka",
        "Spróbuj się odnaleźć",
        # French / accented Latin (odyn1982's #70 — Indila / Edith Piaf)
        "Tourner dans le vide",
        "Non, je ne regrette rien",
        "Padam, padam",
        # Typographic punctuation often present in Tidal titles
        "Track — feat. Artist",
        "Don’t Stop",  # right single quote U+2019
        # Cyrillic + CJK — additional coverage for the broader fix
        "Подмосковные вечера",
        "千本桜",
    ],
)
def test_render_preserves_non_ascii_titles(title):
    """The template renderer must not strip non-ASCII characters from
    titles. Sanitization only removes Windows-illegal chars (`<>:"/\\|?*`)
    and control bytes; everything else is part of the user's filename."""
    item = _item(title=title)
    out = _render_template("{artist} - {title}", item)
    assert title in out, (
        f"Title {title!r} was mangled to {out!r} — non-ASCII chars must "
        f"survive the sanitizer."
    )


def test_build_path_handles_polish_path_segments(tmp_path):
    """End-to-end through _build_path: the rendered path under
    output_dir must contain the original Polish characters byte-for-byte
    so the downloader can write to it on a case-insensitive Windows FS."""
    item = _item(title="Życie", artist="Dawid Podsiadło", album="Małomiasteczkowy")
    settings = _settings(
        "{album_artist}/{album}/{title}",
        output_dir=tmp_path,
        create_album_folders=False,
    )
    out = _build_path(item, settings, ".flac")
    parts = out.relative_to(tmp_path).parts
    assert parts[-1].startswith("Życie"), parts
    assert "Małomiasteczkowy" in parts


# ---------------------------------------------------------------------------
# _explicit_marker — small but worth pinning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag,expected", [(True, " [E]"), (False, "")])
def test_explicit_marker(flag, expected):
    assert _explicit_marker(flag) == expected
