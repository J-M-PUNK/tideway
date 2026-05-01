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
          titlebar stays as its own OS-managed region above the
          WebView (we intentionally don't use FullSizeContentView,
          since that would make WKWebView absorb mouseDown events
          in the titlebar zone and break native window drag plus
          double-click-to-zoom).

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
        # FullSizeContentView lets WKWebView render under the
        # titlebar so the page body's bg-background shows behind
        # the traffic lights — that's the visual blend Spotify /
        # Notion / Linear use. It also creates a problem: WKWebView
        # absorbs mouseDown events in that band, so native window
        # drag and double-click-to-zoom break. We solve that with a
        # transparent NSView overlay (see _install_drag_overlay
        # below) that returns mouseDownCanMoveWindow=YES, making
        # AppKit treat clicks in the titlebar zone as native drags
        # before they ever reach WKWebView's JS layer. Best of both:
        # blend AND native interactions.
        try:
            mask = nswindow.styleMask()
            nswindow.setStyleMask_(
                mask | AppKit.NSWindowStyleMaskFullSizeContentView
            )
        except Exception:
            # Pre-10.10 doesn't have this constant — fall through,
            # the band stays OS-tinted and visually distinct.
            pass
        # titlebarAppearsTransparent kills the gradient so the
        # window's backgroundColor shows through. This and
        # setBackgroundColor together paint the title-bar zone our
        # chosen tone.
        nswindow.setTitlebarAppearsTransparent_(True)
        nswindow.setBackgroundColor_(color)
        # Hide the title text so "Tideway" doesn't print in the
        # middle of the (now color-matched) title bar — letting the
        # space read as part of the app body rather than as an OS
        # chrome band. Traffic-light buttons stay; only the centered
        # title text is suppressed.
        try:
            nswindow.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        except Exception:
            pass
        # Remove the 1-pixel separator macOS Big Sur+ draws between
        # the title bar and the content view. Without this, even
        # when both surfaces share a color, that hairline reads as a
        # visible seam — the user's reported "title bar doesn't
        # blend." Older macOS releases (pre-Big Sur) don't have this
        # method on NSWindow; the fall-through is a no-op so they
        # just keep the system separator they always had.
        try:
            nswindow.setTitlebarSeparatorStyle_(
                AppKit.NSTitlebarSeparatorStyleNone
            )
        except (AttributeError, Exception):
            pass
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
        # Drag overlay. With FullSizeContentView on, WKWebView
        # captures every mouseDown in the titlebar band — including
        # the ones that should start a window drag or trigger a
        # double-click zoom. We can't fix this from the JS side
        # (WKWebView ignores -webkit-app-region: drag), so we put
        # a transparent NSView on top of WKWebView in that band
        # whose mouseDownCanMoveWindow returns YES. AppKit's hit-
        # test finds the overlay first, sees the YES, kicks off the
        # native drag/double-click flow before the click ever
        # reaches WKWebView. See `_install_drag_overlay`.
        try:
            _install_drag_overlay(nswindow)
        except Exception:
            log.exception(
                "window_chrome: drag overlay install failed"
            )
        # Diagnostic — confirms the tint path actually ran. Cheap
        # (one print per window construction) and answers the most
        # common "did chrome tinting take effect on this build?"
        # bug-report question without needing a debugger.
        print(
            f"[window_chrome] macOS tinted: theme={theme} "
            f"color=#{r:02x}{g:02x}{b:02x}",
            flush=True,
        )
    except Exception:
        log.exception("window_chrome: NSWindow tint failed")


# Standard Cocoa titlebar height. The exact value doesn't matter
# for visual correctness (the WebView paints under it either way);
# it's the height the drag overlay covers so clicks in the titlebar
# band route to AppKit's drag handler. 28pt matches what AppKit
# uses for normal windows; 22pt or 38pt would also work and AppKit
# wouldn't care.
_TITLEBAR_HEIGHT_PT = 28


# Cached subclass of NSView with mouseDownCanMoveWindow overridden.
# Built once on first install — re-creating the class each time
# would crash the Objective-C runtime (you can't redeclare a class
# name). Held at module scope so the GC doesn't free it.
_DragOverlayClass = None


def _build_drag_overlay_class():
    """Lazily build a NSView subclass whose mouseDownCanMoveWindow
    returns YES. Only constructs the class on the first call; later
    calls return the cached one. Done inside a function (rather than
    at module load) so the AppKit import stays optional — machines
    that fail to import pyobjc still get a working module."""
    global _DragOverlayClass
    if _DragOverlayClass is not None:
        return _DragOverlayClass
    import AppKit  # type: ignore
    import objc  # type: ignore

    class _TidewayDragOverlay(AppKit.NSView):
        def mouseDownCanMoveWindow(self):  # noqa: N802 — AppKit name
            # Returning True is what makes AppKit treat clicks on
            # this view as window drags / double-click zooms,
            # without the click ever reaching WKWebView. Standard
            # macOS pattern for "make this region of my window
            # draggable like a titlebar."
            return True

        def acceptsFirstMouse_(self, _event):  # noqa: N802
            # Accept drags even when the window isn't currently
            # focused — clicking the titlebar to drag an unfocused
            # window is the expected macOS behaviour.
            return True

        def hitTest_(self, point):  # noqa: N802
            # Standard hit-test. We deliberately return self for
            # any point inside our bounds (AppKit's default does
            # this for opaque views; we're transparent so we have
            # to be explicit, otherwise the hit falls through to
            # WKWebView and we lose the drag handling).
            if AppKit.NSPointInRect(
                self.convertPoint_fromView_(point, None),
                self.bounds(),
            ):
                return self
            return objc.super(_TidewayDragOverlay, self).hitTest_(point)

    _DragOverlayClass = _TidewayDragOverlay
    return _TidewayDragOverlay


def _install_drag_overlay(nswindow) -> None:
    """Add a transparent draggable NSView at the top of the window's
    contentView, spanning the full width and 28pt tall. AppKit's
    hit-test on a click in that band finds this view first, sees
    mouseDownCanMoveWindow=YES, and starts a native window drag —
    bypassing WKWebView's event capture which would otherwise eat
    the mouseDown.

    Idempotent. Tagged via setIdentifier_ so we can find and skip
    re-installing on subsequent _apply_macos calls (e.g. on theme
    flip). Without that, every theme change would stack another
    overlay and they'd accumulate.
    """
    import AppKit  # type: ignore

    content_view = nswindow.contentView()
    if content_view is None:
        return

    # Skip if we already installed one.
    OVERLAY_IDENTIFIER = "tideway-titlebar-drag-overlay"
    for sub in content_view.subviews() or []:
        try:
            if sub.identifier() == OVERLAY_IDENTIFIER:
                return
        except Exception:
            continue

    overlay_class = _build_drag_overlay_class()
    frame = content_view.bounds()
    width = float(frame.size.width)
    height = float(frame.size.height)
    # NSView coords: y=0 at bottom. We want the overlay at the TOP
    # of the contentView, so y = height - 28.
    overlay_frame = AppKit.NSMakeRect(
        0.0,
        height - float(_TITLEBAR_HEIGHT_PT),
        width,
        float(_TITLEBAR_HEIGHT_PT),
    )
    overlay = overlay_class.alloc().initWithFrame_(overlay_frame)
    # Pin to top edge with flexible width so the overlay tracks
    # window resize — without this it'd stay at its initial frame
    # while the window grew, leaving uncovered drag area on the
    # right side of a resized window.
    overlay.setAutoresizingMask_(
        AppKit.NSViewWidthSizable | AppKit.NSViewMinYMargin
    )
    try:
        overlay.setIdentifier_(OVERLAY_IDENTIFIER)
    except Exception:
        pass
    # addSubview puts the overlay AT THE END of the subviews array
    # which means it's drawn on top — exactly what we want for hit-
    # testing to find it first.
    content_view.addSubview_(overlay)


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
    """Best-effort HWND lookup against a pywebview window object.

    Two strategies, in order:

      1. Walk a list of attribute paths the various pywebview backends
         expose. Cheap, version-tolerant for the common cases.
      2. Fall back to EnumWindows looking for a top-level window owned
         by our process whose title matches the pywebview window's
         title. Works regardless of how pywebview internally tracks
         the handle — we just ask the OS which top-level window it
         created on our behalf. Required for pywebview ≥ 5.4 where
         the EdgeChromium backend stopped exposing the HWND on the
         BrowserView and the attribute walk above all returns None.

    Returns None when neither resolves (e.g. before the window is
    actually shown — `shown` event hasn't fired yet — or off-Windows).
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

    # Fallback: enumerate top-level windows with our PID and title.
    try:
        import ctypes
        import os
        from ctypes import wintypes
    except Exception:
        return None
    try:
        user32 = ctypes.windll.user32
        target_title = ""
        try:
            target_title = str(getattr(window, "title", "") or "")
        except Exception:
            target_title = ""

        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
        )
        get_thread_proc_id = user32.GetWindowThreadProcessId
        get_thread_proc_id.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
        ]
        get_thread_proc_id.restype = wintypes.DWORD

        get_text_len = user32.GetWindowTextLengthW
        get_text_len.argtypes = [wintypes.HWND]
        get_text_len.restype = ctypes.c_int

        get_text = user32.GetWindowTextW
        get_text.argtypes = [
            wintypes.HWND, wintypes.LPWSTR, ctypes.c_int
        ]
        get_text.restype = ctypes.c_int

        is_visible = user32.IsWindowVisible
        is_visible.argtypes = [wintypes.HWND]
        is_visible.restype = wintypes.BOOL

        our_pid = os.getpid()
        found = [0]

        def _enum(hwnd, _lparam):
            if not is_visible(hwnd):
                return True
            pid = wintypes.DWORD(0)
            get_thread_proc_id(hwnd, ctypes.byref(pid))
            if pid.value != our_pid:
                return True
            # Match by title when we have one — there can be multiple
            # top-level windows per process (tray helper, mini player,
            # WebView2 host children that escape parenting). When no
            # title hint, pick the first visible top-level we own and
            # hope it's the main one.
            if target_title:
                length = get_text_len(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                get_text(hwnd, buf, length + 1)
                if buf.value != target_title:
                    return True
            found[0] = int(hwnd)
            return False  # stop enumeration

        user32.EnumWindows(EnumWindowsProc(_enum), 0)
        if found[0]:
            return found[0]
    except Exception:
        return None
    return None
