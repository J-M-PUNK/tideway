"""Tests for window_chrome — the OS-side title-bar tint helpers.

The platform-specific paths (NSWindow tinting on macOS, DWM caption
color on Windows) need real OS APIs we can't reasonably mock in
pytest. What we CAN pin here is the cross-platform contract:

  - The color values match what index.css defines for `--background`
    in dark and light themes. If a refactor of the CSS tokens drifts
    those numbers, the OS chrome would silently no longer "blend
    together" with the app body — that's the bug the user filed.
  - Unknown theme strings don't crash; they no-op.
  - The current-theme tracker survives multiple set_theme calls.
"""
from __future__ import annotations

from app import window_chrome


def setup_function(_fn):
    """Each test starts at the dark default to keep ordering stable."""
    window_chrome.set_theme("dark")
    window_chrome._macos_nswindows.clear()  # type: ignore[attr-defined]
    window_chrome._windows_hwnds.clear()  # type: ignore[attr-defined]


def test_dark_color_matches_css_background_token():
    """index.css declares --background: 0 0% 2% on the dark `:root`,
    which is rgb(5, 5, 5). If this constant drifts from the CSS
    token, the OS title bar stops blending with the React content."""
    assert window_chrome._THEME_COLORS["dark"] == (5, 5, 5)


def test_light_color_matches_css_background_token():
    """index.css declares --background: 220 16% 96% on `.light`,
    which evaluates to roughly rgb(243, 244, 246)."""
    assert window_chrome._THEME_COLORS["light"] == (243, 244, 246)


def test_default_theme_is_dark():
    """Dark is the app's default theme, so the chrome tint should
    start there too — otherwise a fresh launch would briefly show a
    light title bar on a dark app body before the React shell pushes
    its preference down."""
    assert window_chrome.get_theme() == "dark"


def test_set_theme_updates_current():
    window_chrome.set_theme("light")
    assert window_chrome.get_theme() == "light"
    window_chrome.set_theme("dark")
    assert window_chrome.get_theme() == "dark"


def test_unknown_theme_is_ignored():
    """Garbage in the request body shouldn't move the tracker, and
    must not raise — the endpoint catches exceptions but the
    underlying helper should already fail-soft."""
    window_chrome.set_theme("light")
    window_chrome.set_theme("vaporwave")
    assert window_chrome.get_theme() == "light"


def test_resolve_color_falls_back_to_dark_for_unknown():
    """If a future caller passes an unknown theme to a tint helper,
    the dark color is the safer fallback (it's the app default)."""
    assert window_chrome._resolve_color("vaporwave") == (5, 5, 5)


def test_register_macos_nswindow_no_op_off_platform():
    """Calling the macOS register from a non-darwin process must
    not append to the tracking list. The cross-platform set_theme
    iterates these lists; non-darwin tests in CI shouldn't carry
    macOS-flavored entries that subsequently fail to apply."""
    # We can't reliably test the darwin path in pytest without a
    # Cocoa context, but we CAN check the off-platform guard by
    # inspecting the list before/after.
    before = list(window_chrome._macos_nswindows)
    window_chrome.register_macos_nswindow(object())  # not on darwin
    after = list(window_chrome._macos_nswindows)
    import sys

    if sys.platform == "darwin":
        # On darwin the call appends. That's fine.
        return
    assert before == after, "register_macos_nswindow should no-op off-platform"


def test_register_windows_hwnd_no_op_off_platform():
    before = list(window_chrome._windows_hwnds)
    window_chrome.register_windows_hwnd(0xDEADBEEF)
    after = list(window_chrome._windows_hwnds)
    import sys

    if sys.platform == "win32":
        return
    assert before == after, "register_windows_hwnd should no-op off-platform"


def test_find_pywebview_hwnd_returns_none_off_platform():
    import sys

    if sys.platform == "win32":
        return
    fake_window = object()
    assert window_chrome.find_pywebview_hwnd(fake_window) is None


# ---------------------------------------------------------------------------
# /api/_internal/window-theme endpoint
# ---------------------------------------------------------------------------


def test_endpoint_rejects_non_loopback_callers():
    """The endpoint should 403 anything that doesn't come from
    loopback. TestClient defaults to client.host == 'testclient',
    which our handler treats as non-loopback. This pins the guard."""
    from fastapi.testclient import TestClient
    import server

    c = TestClient(server.app)
    r = c.post("/api/_internal/window-theme", json={"theme": "dark"})
    assert r.status_code == 403, (
        "Loopback guard regressed — non-127.0.0.1 callers must be 403, "
        f"got {r.status_code}"
    )


def test_endpoint_validates_and_applies_theme_for_loopback_callers():
    """Direct-invoke the handler with a fake loopback request to
    exercise the validation + state-mutation paths without going
    through TestClient (which can't easily fake client.host).

    Pins both: unknown themes 400, known themes flow through to
    window_chrome.set_theme so the next OS-level apply uses the
    right color."""
    import pytest as _pytest
    from fastapi import HTTPException
    import server

    class _Req:
        client = type("_Client", (), {"host": "127.0.0.1"})()

    # Unknown theme → 400.
    with _pytest.raises(HTTPException) as exc_info:
        server.set_window_theme(
            _Req(), server._WindowThemeRequest(theme="vaporwave")
        )
    assert exc_info.value.status_code == 400

    # Known theme → 200 + state mutation.
    out = server.set_window_theme(
        _Req(), server._WindowThemeRequest(theme="light")
    )
    assert out["ok"] is True
    assert out["theme"] == "light"
    assert window_chrome.get_theme() == "light"
