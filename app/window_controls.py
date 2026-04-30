"""Min / max / close primitives for the React titlebar.

When the desktop launcher creates the main window with `frameless=True`
(Windows), the OS no longer draws a caption with min / max / close
buttons. The React shell renders its own buttons and POSTs to
`/api/_internal/window/<action>`, which routes through the callbacks
registered here back to the pywebview window on the GUI thread.

macOS keeps the native traffic-light buttons — the corresponding React
controls don't render — so the Cocoa branch only needs to expose
`platform` so the shell can decide what to draw.

Maximize on Windows is the awkward one: pywebview ≥5 doesn't ship a
`Window.maximize()` method, so we go around it with `ShowWindow` over
the underlying HWND. `IsZoomed` gives us the real maximized state for
the icon, including transitions caused by Win+Up, drag-to-edge snap,
or double-click on the titlebar drag region.
"""
from __future__ import annotations

import sys
from typing import Optional


def is_windows_frameless_supported() -> bool:
    """True when we can usefully run a frameless main window on this
    OS. Windows is the only target where a custom HTML titlebar is
    standard practice; macOS keeps native traffic lights, Linux goes
    untouched (GTK CSD theming is too varied to do reliably)."""
    return sys.platform == "win32"


def is_window_maximized(hwnd: int) -> bool:
    """Return True if the given top-level window is currently maximized.
    Uses Win32 `IsZoomed`; falls through to False on any error or off-
    Windows so the caller can render a stable icon without branching.
    """
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsZoomed(hwnd))
    except Exception:
        return False


def toggle_maximize(hwnd: int) -> bool:
    """Maximize if not currently maximized, otherwise restore. Returns
    the new maximized state. No-op + returns False when we can't
    resolve the HWND or aren't on Windows."""
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        import ctypes
        # SW_RESTORE = 9 brings the window out of either minimized or
        # maximized state. SW_MAXIMIZE = 3 maximizes. Together they form
        # the standard "click the middle titlebar button" toggle.
        SW_MAXIMIZE = 3
        SW_RESTORE = 9
        if is_window_maximized(hwnd):
            ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
            return False
        else:
            ctypes.windll.user32.ShowWindow(hwnd, SW_MAXIMIZE)
            return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Launcher-registered state. Mirrors the pattern used elsewhere in
# server.py: the desktop launcher fills these in after creating the
# window, and FastAPI handlers read them on demand. Plain-browser dev
# mode leaves them at their defaults.
# ---------------------------------------------------------------------------

# True when the main window was created with frameless=True. The React
# shell uses this to decide whether to render its own min/max/close
# buttons; the OS draws the buttons otherwise.
_frameless: bool = False

# Callable returning the platform-specific HWND / NSWindow handle for
# the *currently-focused* window we should target. The desktop launcher
# wires this to a closure that resolves through `find_pywebview_hwnd`
# every call — the HWND is stable for a given pywebview window but
# changes if we ever spawn additional windows; resolving lazily keeps
# us from caching a stale handle.
_hwnd_provider: Optional["object"] = None  # type: ignore[type-arg]

# Window-method callbacks. Each is None when we're running outside the
# desktop launcher (plain-browser dev mode) — the FastAPI endpoints
# return {ok: false, reason: "no launcher"} in that case.
_minimize_callback: Optional["object"] = None  # type: ignore[type-arg]
_close_callback: Optional["object"] = None  # type: ignore[type-arg]


def configure(
    *,
    frameless: bool,
    hwnd_provider: Optional["object"] = None,  # type: ignore[type-arg]
    on_minimize: Optional["object"] = None,  # type: ignore[type-arg]
    on_close: Optional["object"] = None,  # type: ignore[type-arg]
) -> None:
    """Called once from the desktop launcher right after window
    creation. After this returns the FastAPI window-control endpoints
    are live."""
    global _frameless, _hwnd_provider, _minimize_callback, _close_callback
    _frameless = bool(frameless)
    _hwnd_provider = hwnd_provider
    _minimize_callback = on_minimize
    _close_callback = on_close


def info() -> dict:
    """Snapshot of current chrome state for the React shell. `platform`
    is the value `sys.platform` returns ("win32", "darwin", "linux");
    the React side maps it to its own constants. `frameless` decides
    whether the shell renders its own buttons. `maximized` is only
    meaningful when `platform == "win32"` and `frameless` is True."""
    hwnd: int = 0
    if _hwnd_provider is not None:
        try:
            value = _hwnd_provider()
            if isinstance(value, int):
                hwnd = value
        except Exception:
            hwnd = 0
    return {
        "platform": sys.platform,
        "frameless": _frameless,
        "maximized": is_window_maximized(hwnd) if hwnd else False,
        "launcher": _minimize_callback is not None,
    }


def minimize() -> bool:
    """Trigger pywebview's `window.minimize()` on the GUI thread.
    Returns False if no launcher is registered (plain-browser dev)."""
    if _minimize_callback is None:
        return False
    try:
        _minimize_callback()
        return True
    except Exception:
        return False


def maximize_toggle() -> bool:
    """Toggle the underlying HWND between maximized and restored.
    Returns the new maximized state, or False if unavailable."""
    if _hwnd_provider is None:
        return False
    try:
        hwnd = _hwnd_provider()
        if not isinstance(hwnd, int) or not hwnd:
            return False
        return toggle_maximize(hwnd)
    except Exception:
        return False


def close() -> bool:
    """Trigger the window's close path. On Windows this fires
    pywebview's `closing` event, which our `_on_closing` handler
    converts to hide-to-tray when the tray is up — matching the
    behavior of the OS-drawn close button."""
    if _close_callback is None:
        return False
    try:
        _close_callback()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Native drag + resize for the frameless Windows shell.
#
# Two real problems blocking drag and resize on a frameless pywebview
# window. They each need a different fix because the WebView2 child
# window owns mouse events for the entire client area:
#
#   Resize: pywebview's `frameless=True` strips both WS_CAPTION and
#     WS_THICKFRAME. Without WS_THICKFRAME the OS doesn't think the
#     window is resizable at all — there's no edge zone for the
#     cursor to grab. Restoring WS_THICKFRAME via SetWindowLong gets
#     us the OS's native resize border back. Visible cost: a thin
#     gray edge (~6 px) around the window. Functional gain: the
#     resize cursor + drag-to-resize work on every edge, plus
#     drag-to-edge snap and Aero Shake re-engage because the OS
#     sees a normal resizable top-level window again.
#
#   Drag: WebView2 silently ignores `-webkit-app-region: drag`
#     (it's a Chromium-Apps feature, not stock Chromium), and the
#     WebView2 child window covers the entire client area, so
#     subclassing the parent's WndProc to return HTCAPTION from
#     WM_NCHITTEST never fires for cursor positions inside the React
#     titlebar — those events all go to the child. The standard
#     escape hatch is the ReleaseCapture + SendMessage(WM_SYSCOMMAND,
#     SC_MOVE | HTCAPTION) trick: JS calls a Python endpoint on
#     mousedown, Python tells the OS "treat this as a caption click
#     starting a move," and the OS runs the move loop directly off
#     the cursor's current position. This is what Tauri ships for
#     its custom titlebars.
# ---------------------------------------------------------------------------

_GWL_STYLE = -16
_WS_THICKFRAME = 0x00040000

_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOZORDER = 0x0004
_SWP_FRAMECHANGED = 0x0020

_WM_SYSCOMMAND = 0x0112
_HTCAPTION = 2
_SC_MOVE = 0xF010


def enable_native_resize(hwnd: int) -> bool:
    """Add WS_THICKFRAME to a frameless pywebview window so the OS
    redraws the resize border and accepts drag-to-resize on every
    edge.

    Idempotent — adding the style when it's already present is a
    no-op. Returns False on non-Windows or if the style mutation
    fails (extremely unlikely with a valid HWND).
    """
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    user32 = ctypes.windll.user32
    try:
        get_long = user32.GetWindowLongW
        get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        get_long.restype = ctypes.c_long
        set_long = user32.SetWindowLongW
        set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        set_long.restype = ctypes.c_long
        cur = get_long(hwnd, _GWL_STYLE)
        if cur & _WS_THICKFRAME:
            return True
        set_long(hwnd, _GWL_STYLE, cur | _WS_THICKFRAME)
    except Exception:
        return False

    # Tell the OS the frame changed so it relays out the non-client
    # area immediately. Without SetWindowPos the resize border
    # doesn't appear until the user manually triggers a redraw.
    try:
        user32.SetWindowPos(
            hwnd,
            0,
            0,
            0,
            0,
            0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_FRAMECHANGED,
        )
    except Exception:
        pass
    return True


def start_window_drag() -> bool:
    """Start a native window drag from the cursor's current position.

    The React titlebar fires this on mousedown (left button only,
    not on a button child). We send WM_SYSCOMMAND with SC_MOVE so
    Windows runs the move loop using the cursor's current screen
    position as the drag anchor — drag-to-edge snap, Aero Shake,
    and double-click-to-maximize all work on the same gesture.

    Must be called from the GUI thread. Returns False when there's
    no HWND registered (plain-browser dev) or off-Windows.
    """
    if sys.platform != "win32":
        return False
    if _hwnd_provider is None:
        return False
    try:
        hwnd = _hwnd_provider()
    except Exception:
        return False
    if not isinstance(hwnd, int) or not hwnd:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        # ReleaseCapture is required because the WebView2 child window
        # owns the mouse capture from its mousedown — without
        # releasing it first, the SendMessage that follows is dropped
        # and the drag never starts.
        user32.ReleaseCapture()
        user32.SendMessageW(hwnd, _WM_SYSCOMMAND, _SC_MOVE | _HTCAPTION, 0)
        return True
    except Exception:
        return False

