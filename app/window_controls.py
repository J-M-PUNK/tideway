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
