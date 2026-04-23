"""Packaged desktop entry point.

Starts the FastAPI server on a fixed loopback port, opens a pywebview
window pointed at it, and tears everything down cleanly when the window
closes. Single-instance: a second launch detects the running copy via
/api/health, asks it to focus its window, and exits.

Import chain note: importing `server` eagerly instantiates TidalClient,
Downloader, and everything else. That's intentional — we want to fail
fast (missing data dir, broken session file, etc.) before showing the
window, so the user sees a real error in the console instead of a
hung blank webview.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


def _configure_webview2_autoplay() -> None:
    """Let WebView2 autoplay audible media without a user gesture.

    Windows-only. Music-video playback in the app relies on hls.js
    (Chromium doesn't decode HLS natively), and Chromium's default
    autoplay policy blocks audible playback until the Media
    Engagement Index for the origin is high enough. That makes
    videos start muted with a one-click-to-unmute.

    For the packaged app, the "origin" is a fresh loopback with no
    engagement history every launch — the policy never relaxes
    naturally. Overriding via the Chromium flag matches what every
    Electron-based music app (Spotify desktop, Tidal desktop,
    Apple Music Web) does.

    WebView2 reads WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS at startup,
    so setting it before `webview.start()` is sufficient. No-op on
    macOS (WKWebView has its own autoplay handling) and on Linux
    (WebKitGTK uses a different mechanism that users can configure
    through GNOME / KDE policy).
    """
    if not sys.platform.startswith("win"):
        return
    # Merge with any pre-existing value — dev builds may already
    # set other flags we shouldn't stomp on.
    existing = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    flag = "--autoplay-policy=no-user-gesture-required"
    if flag not in existing:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
            f"{existing} {flag}".strip()
        )


# Binding 127.0.0.1 (not 0.0.0.0) keeps the server invisible to the LAN —
# the desktop app is a single-user tool and nothing on it should be
# reachable from another device.
HOST = "127.0.0.1"
# Port is deterministic so the single-instance probe always knows where
# to look. Picked from the IANA user/ephemeral range, far from common
# dev-server ports to minimize conflicts. If you hit a clash, change
# here AND in the launcher probe below.
PORT = 47823

HEALTH_URL = f"http://{HOST}:{PORT}/api/health"
FOCUS_URL = f"http://{HOST}:{PORT}/api/_internal/focus"


def _probe_existing_instance(timeout: float = 0.5) -> bool:
    """Return True if a sibling app is already serving on PORT.

    False covers both "port is free" and "port is held by something
    else." In the latter case we let uvicorn's bind error surface —
    squatting on our own port without the health marker is something
    the user will want to see, not silently suppress.
    """
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            import json
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("app") == "tidal-downloader"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _ask_existing_to_focus() -> None:
    """Best-effort: poke the running instance to raise its window."""
    try:
        req = urllib.request.Request(FOCUS_URL, method="POST")
        with urllib.request.urlopen(req, timeout=1.0):
            pass
    except Exception:
        # The running instance may not have a window (headless dev run)
        # or may be shutting down — nothing useful we can do either way.
        pass


def _run_uvicorn_in_thread() -> "uvicorn.Server":  # type: ignore[name-defined]
    """Start uvicorn on a daemon thread and return the Server handle so
    the main thread can stop it on window close."""
    import uvicorn

    # Import server here, not at module top, so a caller that only wants
    # the probe helpers (e.g. tests) doesn't pay the full import cost.
    import server as _server  # noqa: F401 — side-effectful init

    config = uvicorn.Config(
        "server:app",
        host=HOST,
        port=PORT,
        log_level="info",
        # Disable reload / workers — single process is what pywebview
        # expects, and reload would spawn a second server that doesn't
        # share the download broker state.
        reload=False,
        workers=1,
        # Keep the access log quiet in packaged builds; uvicorn's default
        # formatter is noisy for a GUI app.
        access_log=False,
    )
    server = uvicorn.Server(config)

    def _run() -> None:
        try:
            server.run()
        except Exception as exc:
            print(f"[desktop] uvicorn crashed: {exc!r}", file=sys.stderr, flush=True)

    threading.Thread(target=_run, daemon=True, name="uvicorn").start()

    # Wait for the server to accept connections before we open the
    # window — opening too early gives the user a white flash while
    # pywebview retries the initial load.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if server.started:
            return server
        time.sleep(0.05)
    # Started flag never flipped — likely a bind failure or import
    # error. Let pywebview open anyway so the user sees *something*;
    # the console will show the real error.
    print("[desktop] uvicorn did not report started within 10s", file=sys.stderr, flush=True)
    return server


def _graceful_shutdown(server: "uvicorn.Server") -> None:  # type: ignore[name-defined]
    """Signal uvicorn to stop and give in-flight requests a brief grace
    period to drain. Runs on the main thread after the pywebview window
    closes."""
    try:
        server.should_exit = True
    except Exception:
        pass
    # Flush the downloader's pending-queue snapshot so a reopen picks up
    # where we left off. The worker threads are daemons and will be
    # killed on process exit; the state on disk is what matters.
    try:
        import server as _server
        _server.downloader._persist_pending()  # type: ignore[attr-defined]
    except Exception:
        pass


def _enable_webview_media_prefs() -> None:
    """Patch the Cocoa backend with the WKWebView config pywebview 6.2
    ships without: private `fullScreenEnabled` pref so HTML
    Fullscreen works on macOS; inline-media playback; PiP media
    playback; and — critically — an autoresizing mask on the WKWebView
    so zooming the window actually grows the content instead of
    leaving the web view stranded at its initial size while the OS
    window background (white) fills the gap.

    Best-effort: any import / attribute failure falls through silently
    — we'd rather ship with one tweak disabled than crash at startup
    if Apple renames a key.
    """
    if sys.platform != "darwin":
        return
    try:
        from webview.platforms.cocoa import BrowserView
        import AppKit
    except Exception:
        return
    original_init = BrowserView.__init__

    def patched_init(self, window):  # type: ignore[no-untyped-def]
        original_init(self, window)
        try:
            config = self.webview.configuration()
            prefs = config.preferences()
            # Private WKPreferences SPI — all addressed via KVC using
            # the underscore-stripped key (KVC strips the leading `_`
            # when mapping to the `_setXxx:` selector). Names verified
            # against WebKit source in WKPreferencesPrivate.h.
            for key in (
                "fullScreenEnabled",
                "allowsPictureInPictureMediaPlayback",
                "allowsInlineMediaPlayback",
                "inlineMediaPlaybackRequiresPlaysInlineAttribute",
                "mediaSourceEnabled",
            ):
                try:
                    prefs.setValue_forKey_(
                        False if key == "inlineMediaPlaybackRequiresPlaysInlineAttribute" else True,
                        key,
                    )
                except Exception:
                    # New SDKs have started throwing on certain
                    # private keys — keep going so one-off rejections
                    # don't stop the others from applying.
                    pass
            # Public API — explicit belt-and-suspenders even though
            # it's also handled via the pref above on macOS.
            try:
                config.setAllowsInlineMediaPlayback_(True)
            except Exception:
                pass
        except Exception:
            pass
        # Window resize fix — without this mask, the WKWebView keeps
        # its initial 1280x800 frame and the OS window chrome (white)
        # shows around it whenever the user zooms or drags the edge.
        try:
            self.webview.setAutoresizingMask_(
                AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
            )
        except Exception:
            pass

    BrowserView.__init__ = patched_init


# Held at module scope so the Objective-C runtime keeps a strong ref
# to the observer for the lifetime of the process.
_macos_notification_observer: Optional[object] = None
_macos_quit_observer: Optional[object] = None
_macos_quit_delegate: Optional[object] = None


def _wire_macos_dock_reopen(window) -> None:  # type: ignore[no-untyped-def]
    """Restore the window on Dock-icon click after close-to-tray.

    The canonical macOS API for "Dock click on a running app" is
    `applicationShouldHandleReopen:hasVisibleWindows:` on the app
    delegate. Empirically it doesn't fire under pywebview's run-loop
    + pyobjc setup even when the delegate is installed and responds
    to the selector — diagnosed in session logs from 2026-04-21.
    `NSApplicationDidBecomeActive` does fire reliably, provided the
    app is actually deactivated first (see `_on_closing` below,
    where we call `NSApp.hide_(None)` after `window.hide()`). With
    that in place, any activation event (Dock click, cmd-tab,
    launch) routes through the observer and restores the window.
    Calling `window.show()` on an already-visible window is a no-op,
    so redundant activations are harmless.
    """
    global _macos_notification_observer
    if sys.platform != "darwin":
        return
    try:
        from PyObjCTools import AppHelper
        from Foundation import NSObject
        import AppKit
    except Exception as exc:
        print(f"[desktop] dock-reopen imports failed: {exc!r}",
              file=sys.stderr, flush=True)
        return

    class _AppActiveObserver(NSObject):
        def didBecomeActive_(self, _n):  # noqa: N802
            try:
                AppHelper.callAfter(window.show)
            except Exception:
                pass

    try:
        observer = _AppActiveObserver.alloc().init()
        AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            observer,
            "didBecomeActive:",
            AppKit.NSApplicationDidBecomeActiveNotification,
            None,
        )
        _macos_notification_observer = observer
    except Exception as exc:
        print(f"[desktop] dock-reopen observer install failed: {exc!r}",
              file=sys.stderr, flush=True)


def _wire_macos_app_quit(window, actually_quitting) -> None:  # type: ignore[no-untyped-def]
    """Make Cmd+Q, Dock right-click Quit, and menu-bar Quit actually
    quit the app instead of falling into the hide-to-tray branch.

    The mechanism: wrap pywebview's NSApp delegate with an Objective-C
    proxy. Our wrapper intercepts `applicationShouldTerminate:`, sets
    the quit flag, and forwards the call to the original delegate so
    pywebview still drives its normal close sequence. Our window close
    handler then sees the flag set and returns True, allowing the
    termination to proceed. Every other selector is forwarded to the
    original delegate unchanged via standard NSProxy forwarding, so
    pywebview's delegate behaviour is otherwise untouched.

    pywebview installs its delegate during `webview.start()`, which is
    after this function is called, so we wait for the
    `NSApplicationDidFinishLaunching` notification to do the swap.
    """
    global _macos_quit_observer, _macos_quit_delegate
    if sys.platform != "darwin":
        return
    try:
        import AppKit
        import objc
        from Foundation import NSObject
    except Exception as exc:
        print(f"[desktop] quit hook imports failed: {exc!r}",
              file=sys.stderr, flush=True)
        return

    class _QuitWrapperDelegate(NSObject):
        def initWithInner_flag_(self, inner, flag_ref):  # noqa: N802
            s = objc.super(_QuitWrapperDelegate, self).init()
            if s is None:
                return None
            s._inner = inner
            s._flag = flag_ref
            return s

        def applicationShouldTerminate_(self, sender):  # noqa: N802
            try:
                self._flag["value"] = True
            except Exception:
                pass
            inner = getattr(self, "_inner", None)
            if inner is not None and inner.respondsToSelector_(b"applicationShouldTerminate:"):
                return inner.applicationShouldTerminate_(sender)
            return AppKit.NSTerminateNow

        # ---- Forwarding plumbing so pywebview's delegate keeps
        # receiving every other selector exactly as before.
        def methodSignatureForSelector_(self, sel):  # noqa: N802
            sig = objc.super(_QuitWrapperDelegate, self).methodSignatureForSelector_(sel)
            if sig is not None:
                return sig
            inner = getattr(self, "_inner", None)
            if inner is not None:
                return inner.methodSignatureForSelector_(sel)
            return None

        def forwardInvocation_(self, invocation):  # noqa: N802
            inner = getattr(self, "_inner", None)
            if inner is not None and inner.respondsToSelector_(invocation.selector()):
                invocation.invokeWithTarget_(inner)
            else:
                objc.super(_QuitWrapperDelegate, self).forwardInvocation_(invocation)

        def respondsToSelector_(self, sel):  # noqa: N802
            if objc.super(_QuitWrapperDelegate, self).respondsToSelector_(sel):
                return True
            inner = getattr(self, "_inner", None)
            if inner is not None and inner.respondsToSelector_(sel):
                return True
            return False

    def _install_wrapper() -> None:
        global _macos_quit_delegate
        app = AppKit.NSApplication.sharedApplication()
        existing = app.delegate()
        if existing is None:
            print("[desktop] quit hook: no NSApp delegate to wrap yet",
                  file=sys.stderr, flush=True)
            return
        wrapper = _QuitWrapperDelegate.alloc().initWithInner_flag_(existing, actually_quitting)
        if wrapper is None:
            return
        app.setDelegate_(wrapper)
        # Strong reference; without this the Objective-C runtime
        # eventually releases the wrapper and we silently revert to
        # pywebview's original delegate.
        _macos_quit_delegate = wrapper

    class _DidLaunchObserver(NSObject):
        def didFinishLaunching_(self, _n):  # noqa: N802
            _install_wrapper()

    try:
        # If pywebview's run loop already fired didFinishLaunching we
        # install right now; otherwise wait for the notification.
        app = AppKit.NSApplication.sharedApplication()
        if app.delegate() is not None:
            _install_wrapper()
        else:
            observer = _DidLaunchObserver.alloc().init()
            AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                observer,
                "didFinishLaunching:",
                AppKit.NSApplicationDidFinishLaunchingNotification,
                None,
            )
            _macos_quit_observer = observer
    except Exception as exc:
        print(f"[desktop] quit hook install failed: {exc!r}",
              file=sys.stderr, flush=True)


def main(argv: Optional[list[str]] = None) -> int:
    # Applied before the webview backend loads — WebView2 reads the
    # env var at init time.
    _configure_webview2_autoplay()

    parser = argparse.ArgumentParser(description="Tideway desktop app")
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Open in the default browser instead of a pywebview window "
        "(useful on systems without WebView2).",
    )
    args = parser.parse_args(argv)

    # Single-instance guard: if /api/health responds, a sibling is
    # already up. Ask it to focus and exit quietly.
    if _probe_existing_instance():
        _ask_existing_to_focus()
        return 0

    server = _run_uvicorn_in_thread()

    if args.browser:
        import webbrowser
        webbrowser.open(f"http://{HOST}:{PORT}/")
        try:
            # Block main thread until Ctrl-C or uvicorn exits on its own.
            while not server.should_exit:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        _graceful_shutdown(server)
        return 0

    try:
        import webview  # pywebview
        _enable_webview_media_prefs()
    except ImportError:
        print(
            "[desktop] pywebview not installed; falling back to default browser. "
            "Install with: pip install pywebview",
            file=sys.stderr,
            flush=True,
        )
        import webbrowser
        webbrowser.open(f"http://{HOST}:{PORT}/")
        try:
            while not server.should_exit:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        _graceful_shutdown(server)
        return 0

    window = webview.create_window(
        "Tideway",
        f"http://{HOST}:{PORT}/",
        width=1280,
        height=800,
        min_size=(800, 600),
    )

    # --- Hide-on-close + tray wiring ---------------------------------
    # Ordering is load-bearing here. We want close-to-tray behavior
    # (red-X hides the window so playback keeps running, tray menu
    # exposes Quit) *only* when the tray is actually up. Otherwise the
    # window disappears into limbo — playback still running, nothing in
    # the menu bar, no way to restore or terminate — and the user has
    # to kill the process from Activity Monitor. So: try the tray
    # first, and install the hide handler only if it started.
    actually_quitting = {"value": False}

    def _show_window() -> None:
        try:
            window.show()
        except Exception:
            pass
        try:
            window.restore()
        except Exception:
            pass

    def _quit_from_tray() -> None:
        actually_quitting["value"] = True
        try:
            window.destroy()
        except Exception:
            pass

    # Mini-player state. Held here so a second request doesn't spawn a
    # duplicate window — we just focus the existing one. We don't try
    # to share state with the main window beyond URL routing; both
    # windows subscribe to the same SSE stream and POST to the same
    # transport endpoints, so playback stays coherent for free.
    mini_window = {"value": None}

    def _open_mini_player() -> None:
        w = mini_window.get("value")
        if w is not None:
            try:
                w.show()
                w.restore()
                return
            except Exception:
                # Stale ref — previous mini-player was destroyed.
                mini_window["value"] = None
        try:
            mw = webview.create_window(
                "Tidal Mini",
                f"http://{HOST}:{PORT}/mini",
                width=360,
                height=120,
                min_size=(280, 100),
                on_top=True,
                resizable=True,
                frameless=False,
            )
        except Exception as exc:
            print(
                f"[desktop] mini-player open failed: {exc!r}",
                file=sys.stderr,
                flush=True,
            )
            return
        # Clear the ref when the window closes so a reopen spawns a
        # fresh one instead of waking a destroyed reference.
        try:
            mw.events.closed += lambda: mini_window.__setitem__("value", None)
        except Exception:
            pass
        mini_window["value"] = mw

    # Register the focus callback so a second launch can raise us. The
    # callable runs on whatever thread the FastAPI handler lives on, so
    # schedule the actual restore onto the pywebview thread via its
    # event-dispatch mechanism.
    import server as _server
    _server.register_focus_callback(_show_window)
    # Register the quit callback so the in-app "Quit" menu entry can
    # force a real shutdown that bypasses close-to-tray.
    _server.register_quit_callback(_quit_from_tray)
    # Register the mini-player callback so the in-app "Open mini
    # player" control can spawn a second pywebview window.
    _server.register_mini_player_callback(_open_mini_player)

    # Start the tray icon. Non-blocking (run_detached internally).
    # `tray is None` when pystray's platform deps are missing, the icon
    # asset can't be found, or creation raised — in all those cases we
    # fall back to "close = quit" so the user can always terminate.
    tray = None
    try:
        from app.tray import start_tray

        icon_path = _find_tray_icon()
        if icon_path is None:
            print(
                "[desktop] tray icon asset not found — close button will quit.",
                file=sys.stderr,
                flush=True,
            )
        else:
            tray = start_tray(
                icon_path=icon_path,
                port=PORT,
                on_show=_show_window,
                on_quit=_quit_from_tray,
            )
            if tray is None:
                print(
                    "[desktop] tray startup returned None — close button will quit.",
                    file=sys.stderr,
                    flush=True,
                )
    except Exception as exc:
        print(
            f"[desktop] tray startup failed ({exc!r}) — close button will quit.",
            file=sys.stderr,
            flush=True,
        )
        tray = None

    # Hide-on-close only if the tray is up to give the user a way back.
    # Without a tray, leave pywebview's default close-quits behavior so
    # the X button works as expected.
    if tray is not None:
        is_windows = sys.platform.startswith("win")

        def _on_closing() -> bool:
            if actually_quitting["value"]:
                return True  # Quit from tray — let pywebview really close
            try:
                if is_windows:
                    # Windows: minimize keeps the taskbar entry, which
                    # is the native restore affordance.
                    window.minimize()
                else:
                    # macOS: hide the window, then hide the app. The
                    # second call is load-bearing: without it the app
                    # stays active with no visible windows and the OS
                    # never fires a reactivation on Dock click (and
                    # `applicationShouldHandleReopen:hasVisibleWindows:`
                    # doesn't fire under pywebview's run loop either).
                    # Hiding the app at the NSApp level is what the
                    # standard Cmd+H flow does, which means a Dock
                    # click reactivates us and our observer in
                    # `_wire_macos_dock_reopen` calls window.show().
                    window.hide()
                    try:
                        import AppKit  # local to keep Windows deps clean
                        AppKit.NSApplication.sharedApplication().hide_(None)
                    except Exception:
                        pass
            except Exception as exc:
                print(f"[desktop] close handler failed: {exc!r}",
                      file=sys.stderr, flush=True)
            return False  # cancel the close; app stays up behind the tray

        try:
            window.events.closing += _on_closing
        except Exception:
            # Older pywebview versions used a different hook shape;
            # fall through to legacy close-kills-everything behavior.
            pass

        # macOS: wire the Dock-click → window.show() path. No-op on
        # Windows (minimize handles it natively via the taskbar).
        _wire_macos_dock_reopen(window)
        # Cmd+Q / Dock right-click Quit / menu-bar "Quit Tideway" all go
        # through NSApp.terminate → applicationShouldTerminate:, which
        # pywebview's delegate translates into our window's closing
        # event. Without special handling our closing handler hides
        # instead of quitting, so the user can never fully exit except
        # via the tray menu. Wrap the app delegate so those three
        # paths set the quit flag first, then let pywebview close
        # windows normally.
        _wire_macos_app_quit(window, actually_quitting)

    try:
        # gui=None lets pywebview pick the native backend
        # (edgechromium/WebView2 on Windows, WebKit on macOS).
        webview.start()
    finally:
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        _graceful_shutdown(server)

    return 0


def _find_tray_icon() -> Optional["Path"]:
    """Locate the tray icon PNG in dev mode and in a PyInstaller bundle.
    Returns None when no icon can be resolved — the caller treats that
    as "skip the tray" rather than crashing."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", ""))
        if meipass.is_dir():
            candidates.append(meipass / "assets" / "tray-icon.png")
    repo_root = Path(__file__).resolve().parent
    candidates.append(repo_root / "assets" / "tray-icon.png")
    for p in candidates:
        if p.is_file():
            return p
    return None


if __name__ == "__main__":
    sys.exit(main())
