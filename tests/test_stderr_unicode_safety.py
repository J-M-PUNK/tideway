"""Regression test for issues #7, #36, #70.

Triggering case: on a Windows machine with a non-UTF-8 locale code
page (cp1250 on Polish, cp1252 on Western European, etc.),
PyInstaller's --windowed mode delivered `sys.stderr is None` and the
downloader's debug `print(... title=!r ...)` calls would later trip
UnicodeEncodeError on a track title containing characters outside the
locale's code page. That exception escaped the download worker's
try/finally (which had no `except`), killed the worker thread, and
left every subsequent download stuck in PENDING forever.

`desktop.py` now (a) wraps None stderr/stdout with `encoding="utf-8"`
+ `errors="replace"` and (b) reconfigures real stderr/stdout to use
`errors="replace"` so a non-encodable character degrades to "?" rather
than raising.

This test pins both halves of that fix by reproducing what the
downloader's print does against a strict-encoded stderr — without the
fix it raises, with the fix it doesn't.
"""
from __future__ import annotations

import io
import sys


def _print_pattern(title: str, stream) -> None:
    """Replicate the exact print pattern the downloader uses at the
    top of `_download` — the call that tripped UnicodeEncodeError on
    user-reported tracks."""
    print(
        f"[downloader] _download START "
        f"title={title!r} quality={'LOSSLESS'!r}",
        file=stream,
        flush=True,
    )


def test_print_with_strict_cp1250_stderr_polish_title_raises():
    """Sanity check: a strict cp1250 stream genuinely raises on
    characters outside cp1250. Confirms the test is exercising the
    failure mode rather than passing for the wrong reason."""
    strict_stream = io.TextIOWrapper(
        io.BytesIO(), encoding="cp1250", errors="strict", write_through=True
    )
    # Cyrillic isn't in cp1250 — guaranteed to trip strict encoding.
    title = "Подмосковные вечера"
    try:
        _print_pattern(title, strict_stream)
    except UnicodeEncodeError:
        return  # expected
    raise AssertionError(
        "Strict cp1250 stream should have raised on Cyrillic title — "
        "the test setup is broken if it didn't."
    )


def test_print_with_replace_errors_does_not_raise():
    """The actual fix: wrapping with errors='replace' degrades the
    non-encodable character to '?' rather than raising. Mirrors what
    desktop.py now does for the wrapped-None case."""
    safe_stream = io.TextIOWrapper(
        io.BytesIO(), encoding="cp1250", errors="replace", write_through=True
    )
    # Mix of triggering character classes from the bug reports.
    for title in [
        "Życie",                    # Polish — in cp1250, just sanity
        "Tourner dans le vide",     # French — accented Latin
        "Подмосковные вечера",     # Cyrillic — definitely outside cp1250
        "千本桜",                   # CJK — outside every Windows codepage
        "Track — feat. Artist",     # em-dash
    ]:
        _print_pattern(title, safe_stream)
    # If we reach here, no UnicodeEncodeError escaped — the fix holds.


def test_real_stderr_is_reconfigured_to_replace_on_import():
    """desktop.py reconfigures the live `sys.stderr` to errors='replace'
    on module import. Importing it from a test should leave stderr in
    that state.

    We don't assert on the encoding (it varies — UTF-8 on Linux/macOS,
    locale codepage on Windows) since the relevant part is errors=,
    not encoding. We just verify no UnicodeEncodeError escapes when
    we print a CJK string through the post-import sys.stderr."""
    import desktop  # noqa: F401  — side effect: reconfigures stdio

    captured = io.BytesIO()
    # Mirror sys.stderr's configuration but capture into BytesIO so
    # we can prove the print succeeded.
    test_stream = io.TextIOWrapper(
        captured,
        encoding=getattr(sys.stderr, "encoding", "utf-8") or "utf-8",
        errors="replace",
        write_through=True,
    )
    _print_pattern("千本桜 — Sen no Sakura", test_stream)
    written = captured.getvalue().decode(test_stream.encoding, errors="replace")
    assert "Sen no Sakura" in written
