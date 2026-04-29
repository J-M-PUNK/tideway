"""Native window-chrome tinting on macOS and Windows.

The OS draws the titlebar (where the close / minimize / zoom buttons
live) using its own theming, which on a default install does NOT
match Tideway's near-black dark theme — there's a visible band of
slightly-different gray above the app's content. This module gives
us a hook to push the in-app theme color into the OS so the chrome
"blends together" with the app body.

Two platform paths:

  macOS — Cocoa NSWindow `setBackgroundColor_:` plus
          `titlebarAppearsTransparent` via PyObjC. Applied at window
          creation through pywebview's BrowserView constructor patch
          (see desktop.py) and re-applied on theme change. The
          full-size-content style mask lets the WKWebView draw under
          the titlebar so any color we set there matches what the
          page renders behind it.

  Windows — `DwmSetWindowAttribute(hwnd, DWMWA_CAPTION_COLOR, ...)`
          via ctypes. Requires Windows 11 build 22000+; older builds
          ignore the attribute (no error), so we no-op gracefully.
          The companion DWMWA_TEXT_COLOR keeps the close-button glyph
          visible on dark captions.

Linux is a no-op. GTK CSD theming is too inconsistent across distros
to set reliably without writing per-DE code we can't test.

Color values are pulled from the same `--background` CSS variable
the rest of the app reads — see web/src/index.css. The numbers below
duplicate the dark and light values from that file so the OS chrome
ends up the same near-black or near-white the page body uses. If
the design system ever changes those tokens, update both places.
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

log = logging.getLogger(__name__)

# RGB triples (0-255) sourced from index.css `:root` and `.light`.
# Dark theme: hsl(0 0% 2%) ≈ #050505; light theme: hsl(220 16% 96%)
# ≈ #f3f4f6. These intentionally mirror what `bg-background` paints
# in the page body so the OS chrome blends with the React content.
_THEME_COLORS = {
    "dark": (5, 5, 5),
    "light": (243, 244, 246),
}


def _resolve_color(theme: str) -> tuple[int, int, int]:
    return _THEME_COLORS.get(theme, _THEME_COLORS["dark"])


# ---------------------------------------------------------------------------
# State — a list of platform-specific window handles we've been asked
# to keep tinted. macOS holds the NSWindow object; Windows holds the
# HWND integer. The current theme is sticky so newly registered
# windows match what the user has set.
# ---------------------------------------------------------------------------
_macos_nswindows: list[object] = []
_windows_hwnds: list[int] = []
_current_theme: str = "dark"


def set_theme(theme: str) -> None:
    """Switch the active chrome color. Called from the React shell
    via the /api/_internal/window-theme endpoint whenever the user
    flips the theme; calling without any registered windows is fine
    (the next register_* call picks up the stored theme)."""
    global _current_theme
    if theme not in _THEME_COLORS:
        return
    _current_theme = theme
    if sys.platform == "darwin":
        for nswindow in _macos_nswindows:
            _apply_macos(nswindow, theme)
    elif sys.platform == "win32":
        for hwnd in _windows_hwnds:
            _apply_windows(hwnd, theme)


def get_theme() -> str:
    return _current_theme


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


def register_macos_nswindow(nswindow: object) -> None:
    """Store an NSWindow reference and apply the current theme. Safe
    to call multiple times for the same window — the list is small
    enough that dedupe-by-identity is overkill, and re-applying the
    same color is a no-op visually."""
    if sys.platform != "darwin" or nswindow is None:
        return
    if nswindow not in _macos_nswindows:
        _macos_nswindows.append(nswindow)
    _apply_macos(nswindow, _current_theme)


def _apply_macos(nswindow: object, theme: str) -> None:
    try:
        import AppKit  # type: ignore
    except Exception:
        return
    r, g, b = _resolve_color(theme)
    try:
        color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
            r / 255.0, g / 255.0, b / 255.0, 1.0
        )
    except Exception:
        log.exception("window_chrome: NSColor build failed")
        return
    try:
        # titlebarAppearsTransparent makes the titlebar pick up the
        # window's backgroundColor instead of the system gradient,
        # which is what makes the chrome "blend" with the content.
        nswindow.setTitlebarAppearsTransparent_(True)
        nswindow.setBackgroundColor_(color)
        # FullSizeContentView lets the WKWebView extend under the
        # titlebar — without this there's still a strip of bare
        # NSWindow backgroundColor above the content, which works,
        # but the React layout doesn't naturally reserve space for
        # the traffic-light buttons. Keeping it off means the React
        # body sits below the titlebar and the titlebar shows
        # backgroundColor; either way the user sees a unified color.
        # We choose NOT to set FullSize here so the existing layout
        # (top of NavBar at y=0 of WKWebView) stays correct.
        # Match light/dark appearance so traffic-light glyphs are
        # readable against the backgroundColor we just set.
        appearance_name = (
            AppKit.NSAppearanceNameDarkAqua
            if theme == "dark"
            else AppKit.NSAppearanceNameAqua
        )
        try:
            appearance = AppKit.NSAppearance.appearanceNamed_(appearance_name)
            if appearance is not None:
                nswindow.setAppearance_(appearance)
        except Exception:
            # Older macOS releases have a different selector name —
            # fall through, the backgroundColor change still takes.
            pass
    except Exception:
        log.exception("window_chrome: NSWindow tint failed")


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

# DwmSetWindowAttribute attribute ids. From dwmapi.h.
# DWMWA_USE_IMMERSIVE_DARK_MODE switches the non-client area to dark
# theme on Windows 10 build 19041+ (legacy: 19 on 18985+).
# DWMWA_CAPTION_COLOR / DWMWA_TEXT_COLOR are Windows 11 22000+.
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36


def register_windows_hwnd(hwnd: int) -> None:
    """Store an HWND and apply the current theme. The hwnd type comes
    from pywebview's Windows backend — usually accessible as
    `window.gui.HWND` or via the underlying WebView2 host.
    """
    if sys.platform != "win32" or not hwnd:
        return
    if hwnd not in _windows_hwnds:
        _windows_hwnds.append(hwnd)
    _apply_windows(hwnd, _current_theme)


def _apply_windows(hwnd: int, theme: str) -> None:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:
        return
    set_attr = dwmapi.DwmSetWindowAttribute
    set_attr.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    set_attr.restype = ctypes.c_long  # HRESULT

    r, g, b = _resolve_color(theme)
    # COLORREF is 0x00BBGGRR — the inverse byte order of the more
    # familiar RGB hex literal. Get this wrong and you swap red and
    # blue (the symptom is "tint looks tinted, but cyan instead of
    # red").
    colorref = (b << 16) | (g << 8) | r

    # Caption color — the actual tint we want. Older Windows ignores
    # this attribute and returns E_INVALIDARG; we don't care.
    try:
        value = ctypes.c_uint(colorref)
        set_attr(
            hwnd,
            _DWMWA_CAPTION_COLOR,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except Exception:
        log.exception("window_chrome: DwmSetWindowAttribute caption color failed")

    # Text color — the close/min/max button glyphs. White on dark,
    # near-black on light, so they remain visible against whatever
    # caption color we just set.
    try:
        text_rgb = (255, 255, 255) if theme == "dark" else (16, 16, 16)
        text_colorref = (text_rgb[2] << 16) | (text_rgb[1] << 8) | text_rgb[0]
        value = ctypes.c_uint(text_colorref)
        set_attr(
            hwnd,
            _DWMWA_TEXT_COLOR,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except Exception:
        log.exception("window_chrome: DwmSetWindowAttribute text color failed")

    # Immersive dark mode — switches the title bar's chrome to its
    # dark theme so the close-button hover state, focus ring, etc.
    # match. We pass this independently of the caption color because
    # the OS still draws its own ornaments (resize edge, focus ring)
    # and they need to match the underlying tone.
    try:
        dark = 1 if theme == "dark" else 0
        value = ctypes.c_int(dark)
        set_attr(
            hwnd,
            _DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except Exception:
        log.exception(
            "window_chrome: DwmSetWindowAttribute immersive dark mode failed"
        )


def find_pywebview_hwnd(window: object) -> Optional[int]:
    """Best-effort HWND lookup against a pywebview window object. The
    attribute path differs across pywebview versions / GUIs; try a
    few likely candidates rather than pin to one. Returns None if
    we can't resolve it — caller should bail silently rather than
    error out, since tinting a non-existent handle isn't fatal.
    """
    if sys.platform != "win32" or window is None:
        return None
    candidates = (
        ("native_window_handle",),
        ("gui", "WindowHandle"),
        ("gui", "HWND"),
        ("gui", "hwnd"),
        ("uid",),  # last resort — pywebview sometimes stores HWND here
    )
    for path in candidates:
        try:
            obj = window
            for attr in path:
                obj = getattr(obj, attr)
            if isinstance(obj, int) and obj != 0:
                return obj
        except Exception:
            continue
    return None
