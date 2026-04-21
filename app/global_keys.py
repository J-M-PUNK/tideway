"""Global media-key listener.

Tidal's top desktop complaint is that its keyboard shortcuts only work
when the window is focused. We solve this by spinning up a `pynput`
global listener on a background thread that forwards media-key events
to our local player HTTP endpoints.

- macOS: needs the user to grant Accessibility permission to the packaged
  app (System Settings → Privacy & Security → Accessibility). pynput
  fails silently without it — we log a hint so launches from a terminal
  surface the issue.
- Windows / Linux: no permission prompt; pynput just works.

All calls go to /api/player/* on the local loopback. The listener never
touches VLCPlayer directly — keeps it engine-agnostic and avoids the
GIL/threading surprises you get when pynput's listener thread touches
libvlc.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional
from urllib import request as urlrequest

log = logging.getLogger(__name__)

try:
    from pynput import keyboard  # type: ignore

    _PYNPUT_IMPORT_ERROR: Optional[str] = None
except Exception as exc:  # pragma: no cover — env-dependent
    keyboard = None  # type: ignore
    _PYNPUT_IMPORT_ERROR = str(exc)


class GlobalMediaKeyListener:
    """Listens for the three media keys system-wide and fires HTTP
    POSTs against our local player endpoints.

    State machine is deliberately minimal — we only care about
    key-down on the three keys we handle. Everything else passes
    through untouched so the listener is transparent to the OS.
    """

    def __init__(self, base_url: str):
        if keyboard is None:
            raise RuntimeError(
                f"pynput not available: {_PYNPUT_IMPORT_ERROR}"
            )
        self._base_url = base_url.rstrip("/")
        self._listener: Optional[object] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Non-blocking — pynput runs the listener on its own thread.
        Safe to call from the main thread during app startup."""
        with self._lock:
            if self._listener is not None:
                return
            l = keyboard.Listener(on_press=self._on_press, daemon=True)
            l.start()
            self._listener = l
            log.info("global media-key listener started")

    def stop(self) -> None:
        with self._lock:
            l = self._listener
            self._listener = None
        if l is not None:
            try:
                l.stop()  # type: ignore[attr-defined]
            except Exception:
                pass

    def _on_press(self, key) -> None:  # type: ignore[no-untyped-def]
        try:
            handler = _KEY_HANDLERS.get(key)
        except Exception:
            return
        if handler is None:
            return
        action = handler
        # Fire on its own thread — HTTP calls shouldn't block the
        # pynput event loop or the OS quickly throttles us.
        threading.Thread(
            target=_safe_post, args=(self._base_url, action), daemon=True
        ).start()


def _safe_post(base_url: str, path: str) -> None:
    url = f"{base_url}{path}"
    try:
        req = urlrequest.Request(url, method="POST")
        urlrequest.urlopen(req, timeout=2).close()  # noqa: S310
    except Exception as exc:
        log.debug("global-key POST %s failed: %s", url, exc)


_KEY_HANDLERS: dict[object, str] = {}


def _build_handlers() -> None:
    """Populate the pynput-key → endpoint-path map. Done at import
    time when pynput is available so the listener's on_press path is
    just a dict lookup. The "toggle" semantics for play/pause live on
    the backend — it reads current state and chooses resume or pause."""
    if keyboard is None:
        return
    _KEY_HANDLERS[keyboard.Key.media_play_pause] = "/api/hotkey/play_pause"
    _KEY_HANDLERS[keyboard.Key.media_next] = "/api/hotkey/next"
    _KEY_HANDLERS[keyboard.Key.media_previous] = "/api/hotkey/previous"


_build_handlers()


def start_global_hotkeys(port: int) -> Optional[Callable[[], None]]:
    """Convenience helper. Returns a stop() callback or None when the
    listener couldn't start (e.g. pynput missing)."""
    if keyboard is None:
        log.warning(
            "pynput not available — global media keys disabled (%s)",
            _PYNPUT_IMPORT_ERROR,
        )
        return None
    listener = GlobalMediaKeyListener(f"http://127.0.0.1:{port}")
    listener.start()
    return listener.stop
