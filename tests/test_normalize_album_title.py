"""Tests for `_normalize_album_title` — the More By dedupe key.

The function exists to collapse Tidal's variant-tagged album titles
("Hurry Up Tomorrow (Deluxe)", "Hurry Up Tomorrow [Explicit]",
"Hurry Up Tomorrow - Deluxe Edition") down to a single comparison
key so a 6-slot More By row doesn't end up filled with 6 ID-distinct
copies of the same release. Real-world bug — happened with Hurry
Up Tomorrow on the album-detail page.

Pinning the cases here keeps a future regex tweak from accidentally
breaking the dedupe on a release that does need to stay distinct.
"""
from __future__ import annotations

import pytest

from server import _normalize_album_title


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Identity / no variant tag.
        ("Hurry Up Tomorrow", "hurry up tomorrow"),
        ("Album", "album"),
        ("", ""),
        # Trailing parenthetical variant tags.
        ("Hurry Up Tomorrow (Deluxe)", "hurry up tomorrow"),
        ("Album (Explicit)", "album"),
        ("Album (Clean)", "album"),
        # Bracketed variants (Tidal sometimes uses brackets instead).
        ("Hurry Up Tomorrow [Explicit]", "hurry up tomorrow"),
        ("Album [Bonus Track Version]", "album"),
        # Stacked variant tags.
        ("Hurry Up Tomorrow (Deluxe) [Explicit]", "hurry up tomorrow"),
        ("Album (Deluxe Edition) [Clean]", "album"),
        # Suffix-style edition / version / remaster / mix.
        ("Hurry Up Tomorrow - Deluxe Edition", "hurry up tomorrow"),
        ("Album - 2024 Remaster", "album"),
        ("Album - Anniversary Edition", "album"),
        ("Album - Radio Mix", "album"),
        # Whitespace / case normalization.
        ("  Hurry  Up  Tomorrow  ", "hurry up tomorrow"),
        ("HURRY UP TOMORROW", "hurry up tomorrow"),
    ],
)
def test_normalizes(raw: str, expected: str) -> None:
    assert _normalize_album_title(raw) == expected


def test_no_infinite_loop_on_pathological_input() -> None:
    """The normalizer's strip loop is bounded at 4 iterations so a
    malformed title with infinite nested parens can't hang. Verify
    a torture-test input returns in finite time and produces a
    plausible result."""
    raw = "Album " + "(x)" * 50
    out = _normalize_album_title(raw)
    # Don't assert exact output (the loop bound stops mid-strip),
    # but it should be a string and shouldn't be the original.
    assert isinstance(out, str)
    assert "album" in out
