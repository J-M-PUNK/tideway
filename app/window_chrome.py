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


# Diagnostic log path. The packaged Mac app has no stdout (Finder
# launches don't attach a Terminal), so chrome-tinting prints
# disappear into the void and we can't tell what worked vs failed
# from a shipped build. Writing to a known file under ~/Library
# lets the user / a tester `cat` the file after launch and report
# back what happened. Cheap (a few hundred bytes per launch).
def _diag_log_path():
    try:
        from app.paths import user_data_dir
        path = user_data_dir() / "window-chrome.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    except Exception:
        return None


def _diag(line: str) -> None:
    """Append a line to the chrome diagnostic log AND print to
    stdout. Both paths so dev runs (./run.sh) see it on the
    console and packaged-app launches see it in the log file."""
    print(f"[window_chrome] {line}", flush=True)
    path = _diag_log_path()
    if path is None:
        return
    try:
        with open(path, "a") as f:
            f.write(f"{line}\n")
    except Exception:
        pass


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


def reapply_macos_chrome() -> None:
    """Re-run _apply_macos against every registered NSWindow.

    Called from desktop.py on the pywebview `shown` event so the
    chrome modifications land AFTER pywebview has finished its own
    setup. Without this, anything pywebview does between our init
    hook and the window actually appearing on screen could
    overwrite our styleMask / frame changes — and on at least
    some pywebview versions, that's exactly what was happening,
    leaving the OS-default titlebar visible despite our calls.
    Reapplying after `shown` is the belt-and-suspenders that makes
    chrome stick reliably."""
    if sys.platform != "darwin":
        return
    for nswindow in list(_macos_nswindows):
        try:
            _apply_macos(nswindow, _current_theme)
        except Exception:
            log.exception("window_chrome: reapply failed")


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
        # drag and double-click-to-zoom break. We solve that with
        # an NSEvent local monitor (see _install_drag_monitor
        # below) that intercepts those events at the system level.
        #
        # The constant: NSWindowStyleMaskFullSizeContentView.
        # PyObjC has shipped this under TWO names depending on
        # version: NSWindowStyleMaskFullSizeContentView (modern,
        # post-10.12 SDK) and NSFullSizeContentViewWindowMask
        # (legacy, pre-10.12 name still exposed for compatibility).
        # We try both, and fall back to the numeric value (1 << 15
        # = 32768) which is the actual bit in the style mask. A
        # constant-lookup failure is the most plausible reason
        # FullSize wasn't taking effect on early test builds —
        # numeric fallback eliminates that failure mode.
        full_size_mask_bit = 0
        for name in (
            "NSWindowStyleMaskFullSizeContentView",
            "NSFullSizeContentViewWindowMask",
        ):
            value = getattr(AppKit, name, None)
            if isinstance(value, int) and value > 0:
                full_size_mask_bit = value
                break
        if full_size_mask_bit == 0:
            full_size_mask_bit = 1 << 15  # documented value
        try:
            mask = nswindow.styleMask()
            nswindow.setStyleMask_(mask | full_size_mask_bit)
            _diag(
                f"styleMask: was=0x{int(mask):x} "
                f"+ FullSize(0x{full_size_mask_bit:x}) "
                f"-> 0x{int(nswindow.styleMask()):x}"
            )
        except Exception as exc:
            _diag(f"styleMask set failed: {exc!r}")
        # FullSize on the styleMask doesn't automatically resize the
        # contentView (empirically: previous build's log showed the
        # contentView still at window-height-minus-28 even with the
        # FullSize bit set). Apple's docs imply otherwise but the
        # observed behaviour is what wins. So we forcibly set the
        # contentView's frame to fill the entire window, which DOES
        # propagate to its subviews via their autoresizingMasks.
        #
        # Then we walk the subview tree and dump every class name
        # to the diagnostic log — that gives us hard data on what
        # the WebView is actually called in the hierarchy, so we
        # can match it correctly. The previous build's log showed
        # "WebView frames resized: 0" with our "WebView in name"
        # check, meaning the WebView either isn't in the contentView
        # at all OR has a class name that doesn't match. Logging
        # every class name will tell us which.
        try:
            _diag(f"nswindow class={_objc_class_name(nswindow)}")
            content_view = nswindow.contentView()
            if content_view is not None:
                cv_class = _objc_class_name(content_view)
                window_frame = nswindow.frame()
                window_w = float(window_frame.size.width)
                window_h = float(window_frame.size.height)
                bounds = content_view.bounds()
                _diag(
                    f"contentView ObjC class={cv_class} "
                    f"bounds=({bounds.size.width:.0f}x{bounds.size.height:.0f}) "
                    f"window frame=({window_w:.0f}x{window_h:.0f})"
                )
                # If the contentView IS the WebView (some apps do
                # nswindow.setContentView_(webview) directly), we
                # don't need to find or resize anything — it
                # already fills the window by virtue of being the
                # content view. We DO need to defeat WKWebView's
                # default opaque chrome / vibrancy fallback so the
                # page's bg-background actually shows in the
                # titlebar band.
                is_webview = (
                    "WebView" in cv_class
                    or "WebKit" in cv_class
                    or content_view.respondsToSelector_(b"loadHTMLString:baseURL:")
                )
                if is_webview:
                    _diag("contentView IS a WebView — defeating opaque bg")
                    _make_webview_transparent(content_view, color)
                else:
                    _dump_subview_tree(content_view)
                    resized = _resize_webviews_to_fill(content_view, bounds)
                    _diag(f"WebView frames resized: {resized}")
                # Neutralize NSVisualEffectView in the chrome zone.
                # AppKit draws a vibrancy view in the titlebar
                # container that titlebarAppearsTransparent doesn't
                # actually suppress on macOS Sequoia+; this is
                # what's drawing the system-gray band the user
                # sees. Walk up to themeFrame and hide it.
                _neutralize_chrome_vibrancy(nswindow, color)
        except Exception as exc:
            _diag(f"WebView discovery / resize failed: {exc!r}")
        # titlebarAppearsTransparent kills the OS chrome's vibrancy
        # material so whatever the contentView paints in the
        # titlebar zone shows through. setBackgroundColor sets the
        # window's solid background color (visible in regions the
        # contentView/WebView don't cover).
        try:
            nswindow.setTitlebarAppearsTransparent_(True)
            try:
                got = nswindow.titlebarAppearsTransparent()
                _diag(f"titlebarAppearsTransparent set + read back = {got}")
            except Exception:
                _diag("titlebarAppearsTransparent set (no readback)")
        except Exception as exc:
            _diag(f"titlebarAppearsTransparent set failed: {exc!r}")
        try:
            nswindow.setBackgroundColor_(color)
        except Exception as exc:
            _diag(f"setBackgroundColor failed: {exc!r}")
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
        # Drag handler. With FullSizeContentView on, WKWebView
        # captures every mouseDown in the titlebar band — including
        # the ones that should start a window drag or trigger a
        # double-click zoom. We can't fix this with a sibling NSView
        # overlay because WKWebView is layer-backed and AppKit puts
        # layer-backed views on top regardless of subview order. The
        # robust pattern is an NSEvent local monitor that intercepts
        # leftMouseDown events BEFORE NSWindow.sendEvent_ dispatches
        # them anywhere — when the click is in the titlebar zone, we
        # call performWindowDragWithEvent_ on the NSWindow (which
        # handles drag AND double-click-to-zoom natively per its
        # docs) and consume the event. See `_install_drag_monitor`.
        try:
            _install_drag_monitor(nswindow)
        except Exception:
            log.exception(
                "window_chrome: drag monitor install failed"
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


def _neutralize_chrome_vibrancy(nswindow, color) -> None:
    """Walk up from contentView to the themeFrame (window root view)
    and disable any NSVisualEffectView found in the chrome zone.

    Even with titlebarAppearsTransparent=YES, AppKit draws a
    titlebar container with an NSVisualEffectView whose vibrancy
    material composites system gray on top of whatever the
    contentView paints. That's the gray the user keeps seeing.

    Standard pattern (Chrome / Edge / Spotify all do this): walk
    the themeFrame's subviews, find NSVisualEffectView instances,
    and either remove them from their superview or override their
    layer.backgroundColor to clear so the page color shows.

    Private-API territory but well-trodden — every Electron-based
    Mac app reaches into the themeFrame for the same purpose.
    """
    import AppKit  # type: ignore

    content_view = nswindow.contentView()
    if content_view is None:
        return

    # Walk up to find the themeFrame. contentView.superview() is
    # typically _NSThemeFrame; that's the root NSView of the window
    # and contains both the contentView AND the chrome subviews.
    theme_frame = None
    try:
        theme_frame = content_view.superview()
    except Exception:
        pass
    if theme_frame is None:
        _diag("themeFrame: contentView has no superview, skipping")
        return

    _diag(f"themeFrame class={_objc_class_name(theme_frame)}")

    # Walk every descendant of themeFrame and neutralize anything
    # that's an NSVisualEffectView in the top portion (chrome zone).
    # We could be more surgical with frame checks, but emptying ALL
    # vibrancy views in the window is safe — we're not using
    # NSVisualEffectView for anything ourselves, so any we find are
    # AppKit chrome that we want to blank out.
    neutralized = _walk_and_blank_vibrancy(theme_frame, color)
    _diag(f"NSVisualEffectViews neutralized: {neutralized}")


def _walk_and_blank_vibrancy(view, color, depth=0) -> int:
    """Recursive helper. For every NSVisualEffectView in the tree,
    set its background color and material to render as our theme
    color instead of system vibrancy gray."""
    import AppKit  # type: ignore

    if depth > 8:
        return 0
    count = 0
    try:
        subs = view.subviews() or []
    except Exception:
        subs = []

    cls_name = _objc_class_name(view)
    if "VisualEffect" in cls_name:
        # Hit the vibrancy view (the gray material). Hide it
        # entirely — the contentView painting underneath shows
        # through unchanged. NSVisualEffectView has no children
        # we care about (just renders material) so hiding is safe.
        try:
            view.setHidden_(True)
            count += 1
            _diag(f"  hid NSVisualEffectView at depth {depth}")
        except Exception as exc:
            _diag(
                f"  setHidden on NSVisualEffectView failed: {exc!r}"
            )
        # Belt: also clear the layer background in case setHidden
        # doesn't fully suppress on a future macOS revision.
        try:
            layer = view.layer()
            if layer is not None:
                layer.setBackgroundColor_(
                    AppKit.NSColor.clearColor().CGColor()
                )
        except Exception:
            pass

    # Titlebar containers (_NSTitlebarContainerView,
    # _NSTitlebarView) host the traffic-light buttons among other
    # things. Hiding them removes the buttons too — that's NOT
    # what we want. Instead, clear their LAYER background so the
    # container is visible (children render) but draws no chrome
    # of its own. The traffic lights are NSButton subviews and
    # have their own opaque rendering, so they stay visible on top
    # of whatever the WebView paints underneath.
    if "Titlebar" in cls_name or "TitleBar" in cls_name:
        try:
            view.setWantsLayer_(True)
            layer = view.layer()
            if layer is not None:
                layer.setBackgroundColor_(
                    AppKit.NSColor.clearColor().CGColor()
                )
                _diag(
                    f"  cleared layer bg on {cls_name} at depth {depth}"
                )
                count += 1
        except Exception as exc:
            _diag(f"  clear {cls_name} layer failed: {exc!r}")

    for sub in subs:
        count += _walk_and_blank_vibrancy(sub, color, depth + 1)
    return count


def _make_webview_transparent(webview, color) -> None:
    """Make WKWebView's chrome/underpage areas paint our theme color
    instead of the system-default light gray vibrancy.

    Even with FullSizeContentView + titlebarAppearsTransparent, the
    titlebar zone shows whatever is in the WebView's BACKING (layer
    backgroundColor + the underPageBackgroundColor it composites
    when not painting page content). WKWebView defaults are opaque
    light-gray on macOS — that's the gray the user keeps seeing.

    Try every API in sequence; each one targets a different layer
    of the rendering stack. Logging records which paths worked so
    we can prune the unsuccessful ones in a follow-up.
    """
    import AppKit  # type: ignore

    # 1. underPageBackgroundColor — public API on macOS 12+. This
    #    is the documented way to color the area "behind" the page,
    #    including the titlebar zone with FullSize. Setting to our
    #    theme color makes the OS chrome composite with our color
    #    instead of the system default.
    try:
        if webview.respondsToSelector_(b"setUnderPageBackgroundColor:"):
            webview.setUnderPageBackgroundColor_(color)
            _diag("setUnderPageBackgroundColor OK")
        else:
            _diag("setUnderPageBackgroundColor not available")
    except Exception as exc:
        _diag(f"setUnderPageBackgroundColor failed: {exc!r}")

    # 2. setOpaque(NO) — makes the WebView itself non-opaque so
    #    layers underneath show through. Combined with NSWindow's
    #    backgroundColor, this lets our dark color paint in any
    #    region the page hasn't covered.
    try:
        if webview.respondsToSelector_(b"setOpaque:"):
            webview.setOpaque_(False)
            _diag("setOpaque(False) OK")
    except Exception as exc:
        _diag(f"setOpaque(False) failed: {exc!r}")

    # 3. Layer backgroundColor — when WKWebView is layer-backed
    #    (always, on modern macOS), the layer's bg color is what
    #    composites under the rendered page. Setting to clear lets
    #    the NSWindow's backgroundColor show through.
    try:
        layer = webview.layer()
        if layer is not None:
            clear = AppKit.NSColor.clearColor()
            layer.setBackgroundColor_(clear.CGColor())
            _diag("layer backgroundColor set to clear")
    except Exception as exc:
        _diag(f"layer backgroundColor failed: {exc!r}")

    # 4. Private legacy API: drawsBackground via KVC. This worked
    #    in WebView (pre-WKWebView) and still works on some
    #    macOS / WKWebView combinations as a last-resort knob.
    try:
        webview.setValue_forKey_(False, "drawsBackground")
        _diag("KVC drawsBackground=False OK")
    except Exception as exc:
        _diag(f"KVC drawsBackground failed: {exc!r}")


def _objc_class_name(obj) -> str:
    """Return the ObjC class name for a PyObjC object. Falls back
    to the Python type's name. PyObjC's bridged types sometimes
    report their generic Python name ("NSView") via type().__name__
    even when the underlying ObjC class is something more specific
    (like "WKWebView"). `className()` is the source of truth."""
    try:
        return str(obj.className())
    except Exception:
        try:
            return type(obj).__name__
        except Exception:
            return "<unknown>"


def _dump_subview_tree(view, depth=0) -> None:
    """Walk the NSView tree under `view` and log every descendant's
    ObjC class name + frame. Bounded depth against runaway loops."""
    if depth > 6:
        return
    try:
        subs = view.subviews() or []
    except Exception as exc:
        _diag(f"  subviews() at depth {depth} raised: {exc!r}")
        return
    if depth == 0 and len(subs) == 0:
        _diag(f"  contentView has NO subviews — WebView is elsewhere")
    for sub in subs:
        cls_name = _objc_class_name(sub)
        try:
            frame = sub.frame()
            geom = (
                f"x={frame.origin.x:.0f} y={frame.origin.y:.0f} "
                f"w={frame.size.width:.0f} h={frame.size.height:.0f}"
            )
        except Exception:
            geom = "frame=?"
        indent = "  " * (depth + 1)
        _diag(f"{indent}subview {cls_name} {geom}")
        _dump_subview_tree(sub, depth + 1)


def _resize_webviews_to_fill(view, bounds, depth=0) -> int:
    """Walk view's subview tree recursively. For each NSView whose
    class name contains 'WebView', set its frame to `bounds` (i.e.
    fill the parent contentView) and assign a fully-flexible
    autoresizingMask so future window resizes also propagate.

    Returns the number of WebView-class views that were resized.
    Bounded depth prevents pathological cycles (NSView graphs
    aren't cyclic in practice but defensive).
    """
    import AppKit  # type: ignore
    if depth > 8:
        return 0
    count = 0
    try:
        subs = view.subviews() or []
    except Exception:
        return 0
    for sub in subs:
        cls_name = type(sub).__name__
        # Match WebView OR direct children of contentView at depth 0.
        # The latter is a hammer for the case where pywebview's
        # WebView class doesn't have "WebView" in its Python name —
        # at the top level under contentView, anything that's not
        # the traffic-light overlay is the WebView, so resizing it
        # to fill is the right move regardless of what it's called.
        is_web_view = "WebView" in cls_name or "WebKit" in cls_name
        is_top_level_candidate = depth == 0 and "Window" not in cls_name
        if is_web_view or is_top_level_candidate:
            try:
                old_frame = sub.frame()
                sub.setFrame_(bounds)
                sub.setAutoresizingMask_(
                    AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
                )
                _diag(
                    f"resized {cls_name} from "
                    f"({old_frame.size.width}x{old_frame.size.height}) "
                    f"to ({bounds.size.width}x{bounds.size.height})"
                )
                count += 1
            except Exception as exc:
                _diag(f"resize {cls_name} failed: {exc!r}")
        count += _resize_webviews_to_fill(sub, bounds, depth + 1)
    return count


# Height of the titlebar drag zone in points. Standard macOS
# titlebar height is 28pt — that's the band where the traffic
# lights live and where users expect window drag / double-click
# zoom to work. AppKit doesn't expose the exact value via API
# (NSWindow.frame() includes the chrome but doesn't break it
# down), so 28pt is hard-coded; matches what every well-behaved
# native macOS app uses.
_TITLEBAR_HEIGHT_PT = 28


# Module-level keepalive for the NSEvent monitor handle. PyObjC
# returns an opaque token from addLocalMonitorForEventsMatchingMask
# that has to stay alive for the monitor to keep firing — losing
# the reference (or letting Python GC it) deregisters the monitor.
# Held in a list keyed by NSWindow id so multiple windows (main +
# mini-player) don't share / clobber.
_drag_monitors: dict[int, object] = {}


def _install_drag_monitor(nswindow) -> None:
    """Install an NSEvent local monitor that intercepts leftMouseDown
    events in the titlebar zone and routes them to the OS's native
    window-drag handler.

    Why this and not an NSView overlay: WKWebView is layer-backed.
    When mixed with non-layer-backed sibling NSViews, AppKit puts
    layer-backed views on top in the visual stack regardless of
    addSubview order, which means a transparent overlay below
    WKWebView in the layer tree never sees the click. An NSEvent
    local monitor sits BEFORE NSWindow.sendEvent_ in the event
    pipeline — events come to us before they reach any view, and
    we can either consume them (return None) or forward them
    (return the event).

    The monitor checks the click location. If it's in the top 28pt
    of the window's content area AND the click isn't on a traffic
    light (which AppKit handles in its own chrome layer above the
    contentView), we call performWindowDragWithEvent_ on the
    NSWindow. That method's documented behaviour is to handle BOTH
    a drag (mouse moves while held) and a double-click (no move
    within the threshold) — the latter triggering zoom or
    fullscreen per System Settings.

    Idempotent per-window. Re-calling for the same NSWindow
    deregisters and replaces the existing monitor so a theme flip
    doesn't stack duplicates.
    """
    import AppKit  # type: ignore

    window_id = id(nswindow)
    # Drop any previous monitor for this window — re-registering
    # without removing the old one means BOTH fire for every
    # event, and we'd consume the same click twice.
    existing = _drag_monitors.pop(window_id, None)
    if existing is not None:
        try:
            AppKit.NSEvent.removeMonitor_(existing)
        except Exception:
            pass

    # Width of the corner zones we leave for the OS resize cursor.
    # Clicks within this many points of the left or right window
    # edge fall through to the OS (which then engages its top-left
    # / top-right corner resize). Without this, our drag monitor
    # would steal clicks meant for resize. ~10pt matches what
    # AppKit treats as the corner hit-zone for standard windows;
    # a tighter value misses near-corner clicks, a looser value
    # eats too much real drag space.
    _CORNER_RESIZE_ZONE_PT = 10

    def _handler(event):
        try:
            # event.window() is None for events not addressed to
            # any window (e.g. menu bar interactions). Skip those.
            event_window = event.window()
            if event_window is None or event_window != nswindow:
                return event
            location = event.locationInWindow()
            window_frame = nswindow.frame()
            window_width = window_frame.size.width
            window_height = window_frame.size.height
            # locationInWindow uses bottom-left origin, so the top
            # of the window is at y = window_height. The titlebar
            # band is y in [window_height - 28, window_height].
            if location.y < (window_height - float(_TITLEBAR_HEIGHT_PT)):
                return event
            if location.y > window_height:
                return event
            # Skip the corner zones so the OS can engage its
            # top-left / top-right resize cursor. Without this,
            # the very top corners of the window are stuck as drag
            # surface even though the OS would otherwise show a
            # diagonal-resize cursor and let the user resize.
            if location.x < float(_CORNER_RESIZE_ZONE_PT):
                return event
            if location.x > (window_width - float(_CORNER_RESIZE_ZONE_PT)):
                return event
            # In the titlebar zone — start a native window drag.
            # The OS's drag loop tracks mouseUp on its own and
            # decides drag vs double-click-zoom based on movement
            # within its threshold.
            try:
                nswindow.performWindowDragWithEvent_(event)
            except Exception:
                # If performWindowDrag isn't available (very old
                # macOS) or something else goes wrong, fall
                # through and let the event reach WKWebView.
                # Worse UX than a working drag, better than a
                # crash.
                return event
            # Consume the event so WKWebView doesn't ALSO see it.
            # NSWindow.performWindowDragWithEvent_ handles the
            # entire interaction internally.
            return None
        except Exception:
            log.exception(
                "window_chrome: drag monitor handler raised"
            )
            return event

    monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
        AppKit.NSEventMaskLeftMouseDown,
        _handler,
    )
    if monitor is not None:
        _drag_monitors[window_id] = monitor


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
