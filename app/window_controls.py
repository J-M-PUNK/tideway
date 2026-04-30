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
# The React titlebar's `app-region: drag` doesn't work in WebView2 —
# that CSS rule is a Chromium-Apps feature, not stock Chromium, and
# WebView2 silently ignores it. And `frameless=True` strips the
# resize border entirely. Together that means a frameless pywebview
# window on Windows is, by default, neither draggable nor resizable.
#
# The fix is to handle this at the Win32 level: subclass the
# WindowProc, add WS_THICKFRAME back so the OS recognises the window
# as resizable, override WM_NCHITTEST to declare which screen
# rectangles are caption (drag) vs resize-edge vs client, and
# override WM_NCCALCSIZE so the OS doesn't paint the standard thick
# frame on top of our content. This is the same pattern Electron,
# VS Code, and Discord use for their frameless title bars.
#
# All numeric zones (titlebar height, button area width, resize
# border) are scaled to the window's DPI so they stay correct on
# high-DPI displays.
# ---------------------------------------------------------------------------

# Hit-test response codes (winuser.h).
_HTCLIENT = 1
_HTCAPTION = 2
_HTLEFT = 10
_HTRIGHT = 11
_HTTOP = 12
_HTTOPLEFT = 13
_HTTOPRIGHT = 14
_HTBOTTOM = 15
_HTBOTTOMLEFT = 16
_HTBOTTOMRIGHT = 17

_WM_NCCALCSIZE = 0x0083
_WM_NCHITTEST = 0x0084

_GWLP_WNDPROC = -4
_GWL_STYLE = -16
_WS_THICKFRAME = 0x00040000

_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOZORDER = 0x0004
_SWP_FRAMECHANGED = 0x0020

# Zones expressed in CSS pixels (96 DPI). Scaled at hit-test time
# against the window's actual DPI. The titlebar height MUST stay in
# sync with WindowTitlebar.tsx — that React component reserves the
# top 32 px for the drag bar and the right 138 px (3 buttons × 46 px)
# for min/max/close. If those numbers move, update the constants
# below or the buttons stop accepting clicks.
_CAPTION_HEIGHT_CSS_PX = 32
_BUTTON_ZONE_WIDTH_CSS_PX = 138
_RESIZE_BORDER_CSS_PX = 6

# Module-level state for the hook. We hold the WNDPROC callable so
# Python's GC doesn't free it while Windows still has its function
# pointer, and we hold the previous WndProc address so our handler
# can forward unhandled messages.
_subclass_wndproc = None
_prev_wndproc: int = 0
_subclassed_hwnd: int = 0


def install_native_hit_test(hwnd: int) -> bool:
    """Make a frameless pywebview window draggable and resizable
    using native Win32 hit testing.

    Returns True when the hook is installed, False on non-Windows or
    if the HWND can't be subclassed. Idempotent — calling twice on
    the same HWND no-ops.
    """
    global _subclass_wndproc, _prev_wndproc, _subclassed_hwnd
    if sys.platform != "win32" or not hwnd:
        return False
    if _subclassed_hwnd == hwnd and _subclass_wndproc is not None:
        return True
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    user32 = ctypes.windll.user32

    # Restore WS_THICKFRAME. pywebview's frameless mode strips it
    # along with WS_CAPTION; without it Windows doesn't recognise the
    # window as resizable even when WM_NCHITTEST returns HTLEFT/etc.
    # We don't add WS_CAPTION back — the React titlebar is the title
    # bar — and WM_NCCALCSIZE below collapses the thick-frame visual
    # so the user never sees a stray border.
    try:
        get_long = user32.GetWindowLongW
        get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        get_long.restype = ctypes.c_long
        set_long = user32.SetWindowLongW
        set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        set_long.restype = ctypes.c_long
        cur_style = get_long(hwnd, _GWL_STYLE)
        set_long(hwnd, _GWL_STYLE, cur_style | _WS_THICKFRAME)
    except Exception:
        return False

    # Window-proc callable signature: LRESULT(HWND, UINT, WPARAM, LPARAM).
    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    def _scale(hwnd_arg: int) -> float:
        # GetDpiForWindow exists from Windows 10 1607+. Older versions
        # fall back to 96 (no scaling) — those builds are out of
        # support so the degraded behaviour is acceptable.
        try:
            get_dpi = user32.GetDpiForWindow
            get_dpi.argtypes = [wintypes.HWND]
            get_dpi.restype = wintypes.UINT
            dpi = int(get_dpi(hwnd_arg))
            return (dpi / 96.0) if dpi > 0 else 1.0
        except Exception:
            return 1.0

    def _hit_test(hwnd_arg: int, screen_x: int, screen_y: int) -> int:
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd_arg, ctypes.byref(rect)):
            return _HTCLIENT
        rel_x = screen_x - rect.left
        rel_y = screen_y - rect.top
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        scale = _scale(hwnd_arg)
        border = max(1, int(round(_RESIZE_BORDER_CSS_PX * scale)))
        cap_h = max(1, int(round(_CAPTION_HEIGHT_CSS_PX * scale)))
        btn_w = max(1, int(round(_BUTTON_ZONE_WIDTH_CSS_PX * scale)))

        # Resize edges only when the window isn't maximised — a
        # maximised window shouldn't accept resize-from-edge.
        is_max = bool(user32.IsZoomed(hwnd_arg))
        if not is_max:
            on_left = rel_x < border
            on_right = rel_x >= width - border
            on_top = rel_y < border
            on_bottom = rel_y >= height - border
            if on_top and on_left:
                return _HTTOPLEFT
            if on_top and on_right:
                return _HTTOPRIGHT
            if on_bottom and on_left:
                return _HTBOTTOMLEFT
            if on_bottom and on_right:
                return _HTBOTTOMRIGHT
            if on_left:
                return _HTLEFT
            if on_right:
                return _HTRIGHT
            if on_top:
                return _HTTOP
            if on_bottom:
                return _HTBOTTOM

        # Caption (drag) zone: top of the client area, minus the
        # button strip on the right. Returning HTCLIENT for the button
        # strip lets WebView2 receive the click and fire the React
        # button's onClick.
        if rel_y < cap_h and rel_x < width - btn_w:
            return _HTCAPTION

        return _HTCLIENT

    def _wnd_proc(hwnd_arg, msg, wparam, lparam):
        if msg == _WM_NCCALCSIZE and wparam:
            # Returning 0 with the rgrc[0] rect unchanged tells
            # Windows the client area equals the whole window rect,
            # which collapses the WS_THICKFRAME visual to zero. The
            # frame still exists for hit testing, so resize edges
            # work — the user just doesn't see a stray border.
            return 0
        if msg == _WM_NCHITTEST:
            # lParam packs the cursor's screen coords as (y << 16) | x,
            # interpreted as 16-bit signed. Use ctypes.c_short to
            # decode the sign bit on multi-monitor setups where
            # secondary monitors can have negative coords.
            x = ctypes.c_short(lparam & 0xFFFF).value
            y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            return _hit_test(hwnd_arg, x, y)
        # Forward everything else to the original WndProc so
        # WebView2's input handling, focus, paint, etc. all keep
        # working.
        return user32.CallWindowProcW(
            _prev_wndproc, hwnd_arg, msg, wparam, lparam
        )

    # SetWindowLongPtrW is the 64-bit-safe version of SetWindowLongW
    # for setting a WndProc. On 32-bit Python (rare on modern
    # Windows) the symbol may be absent — fall through to the 32-bit
    # variant in that case.
    try:
        set_ptr = user32.SetWindowLongPtrW
        set_ptr.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        set_ptr.restype = ctypes.c_void_p
    except AttributeError:
        set_ptr = user32.SetWindowLongW
        set_ptr.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        set_ptr.restype = ctypes.c_long

    new_wndproc = WNDPROC(_wnd_proc)
    proc_addr = ctypes.cast(new_wndproc, ctypes.c_void_p).value
    prev = set_ptr(hwnd, _GWLP_WNDPROC, proc_addr)
    if not prev:
        return False
    _subclass_wndproc = new_wndproc  # keep alive across GC
    _prev_wndproc = int(prev)
    _subclassed_hwnd = int(hwnd)

    # Tell the OS the frame changed so WM_NCCALCSIZE fires once and
    # the client rect adopts the new "client = window" geometry. No
    # SetWindowPos = the visual border lingers until the first manual
    # resize, which looks janky.
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
