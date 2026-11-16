"""No source file may contain an invalid backslash escape.

Python 3.12 turned "\S"-style escapes in ordinary strings from a
DeprecationWarning into a SyntaxWarning, and a future release makes
them a SyntaxError, at which point the module won't import at all.
The monthly deprecation audit caught one (#481): the Windows registry
path in app/autostart.py's docstring. This compiles every source file
with that warning promoted to an error, so the next one fails here
instead of in a user's app after a Python upgrade.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SOURCES = sorted(
    [*(_REPO / "app").rglob("*.py"), _REPO / "server.py", _REPO / "desktop.py"]
)


@pytest.mark.parametrize("path", _SOURCES, ids=lambda p: str(p.relative_to(_REPO)))
def test_source_has_no_invalid_escape_sequences(path):
    source = path.read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        warnings.simplefilter("error", DeprecationWarning)
        compile(source, str(path), "exec")
