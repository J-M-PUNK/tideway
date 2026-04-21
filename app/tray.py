"""System-tray / menu-bar icon.

Pairs with the global media-key listener (app/global_keys.py) to let
users close the window without killing playback. Click the icon to
restore the window; the menu exposes play-pause / next / previous /
quit so you can drive the player without bringing it back into focus.

Cross-platform via pystray. On macOS it uses NSStatusItem under the
hood; on Windows, Shell_NotifyIcon. Both require `run_detached()` in
our case because pywebview already owns the main event loop.

All action callbacks are plain HTTP POSTs at the local loopback — same
surface the global-keys listener uses, so queue / shuffle / repeat
stay in the frontend. Nothing on the tray touches VLC directly.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Optional
from urllib import request as urlrequest

log = logging.getLogger(__name__)

try:
    import pystray  # type: ignore
    from PIL import Image  # type: ignore

    _IMPORT_ERR: Optional[str] = None
except Exception as exc:  # pragma: no cover - env-dependent
    pystray = None  # type: ignore
    Image = None  # type: ignore
    _IMPORT_ERR = str(exc)


class TrayIcon:
    def __init__(
        self,
        *,
        icon_path: Path,
        port: int,
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        if pystray is None:
            raise RuntimeError(f"pystray not available: {_IMPORT_ERR}")
        self._port = port
        self._on_show = on_show
        self._on_quit = on_quit
        self._icon_path = icon_path
        self._icon: Optional[object] = None
        self._lock = threading.Lock()

    def _hotkey(self, action: str) -> None:
        """Fire the same endpoint the global-keys listener uses. Runs
        on pystray's menu-callback thread; keep non-blocking."""
        url = f"http://127.0.0.1:{self._port}/api/hotkey/{action}"
        try:
            req = urlrequest.Request(url, method="POST")
            urlrequest.urlopen(req, timeout=2).close()  # noqa: S310
        except Exception as exc:
            log.debug("tray hotkey %s failed: %s", action, exc)

    def _on_icon_activate(self, _icon, _item) -> None:  # type: ignore[no-untyped-def]
        # pystray passes (icon, item) to the default-action callback.
        try:
            self._on_show()
        except Exception:
            log.exception("tray show-window failed")

    def _build_menu(self):  # type: ignore[no-untyped-def]
        MenuItem = pystray.MenuItem
        Menu = pystray.Menu
        return Menu(
            MenuItem(
                "Show Window",
                self._on_icon_activate,
                default=True,  # also fires on single-click
            ),
            Menu.SEPARATOR,
            MenuItem("Play / Pause", lambda _i, _it: self._hotkey("play_pause")),
            MenuItem("Next", lambda _i, _it: self._hotkey("next")),
            MenuItem("Previous", lambda _i, _it: self._hotkey("previous")),
            Menu.SEPARATOR,
            MenuItem("Quit", lambda _i, _it: self._quit()),
        )

    def _quit(self) -> None:
        try:
            self._on_quit()
        except Exception:
            log.exception("tray quit handler failed")
        # Stop the icon after the quit handler runs so the app has a
        # chance to tear the window down first.
        with self._lock:
            icon = self._icon
        if icon is not None:
            try:
                icon.stop()  # type: ignore[attr-defined]
            except Exception:
                pass

    def start(self) -> None:
        """Create the icon + run its event loop on a background thread.

        pystray's run_detached() handles the platform-specific thread
        bridging — on macOS it marshals NSStatusItem creation onto the
        main thread under the hood; on Windows it runs the Shell_Notify
        message loop on the thread it was started from. Non-blocking
        from the caller's perspective either way.
        """
        with self._lock:
            if self._icon is not None:
                return
            image = Image.open(self._icon_path)
            icon = pystray.Icon(
                "tidal-downloader",
                image,
                "Tidal Downloader",
                menu=self._build_menu(),
            )
            self._icon = icon
        icon.run_detached()
        log.info("tray icon started")

    def stop(self) -> None:
        with self._lock:
            icon = self._icon
            self._icon = None
        if icon is not None:
            try:
                icon.stop()  # type: ignore[attr-defined]
            except Exception:
                pass


def start_tray(
    *,
    icon_path: Path,
    port: int,
    on_show: Callable[[], None],
    on_quit: Callable[[], None],
) -> Optional[TrayIcon]:
    """Returns the started TrayIcon or None if pystray couldn't load.
    Callers should stop() it on shutdown."""
    if pystray is None:
        log.warning("pystray not available (%s) — tray icon disabled", _IMPORT_ERR)
        return None
    try:
        tray = TrayIcon(
            icon_path=icon_path, port=port, on_show=on_show, on_quit=on_quit
        )
        tray.start()
        return tray
    except Exception as exc:
        log.warning("tray icon startup failed: %s", exc)
        return None
