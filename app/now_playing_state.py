"""Backend persistence backstop for "what was playing when the user
quit."

The frontend already writes to localStorage (see `usePlayer.ts`),
but pywebview's WKWebView on macOS doesn't always preserve
localStorage between app launches the way a regular browser tab
does — depending on the data-store mode pywebview opens, the OS
may discard the database on app quit. That made our
"reopen-and-resume" promise unreliable in the packaged .app even
though dev-mode (regular browser) testing looked fine.

This module is the safety net. The frontend now POSTs the same
JSON it writes to localStorage to `/api/now-playing/state` on
every persist tick + lifecycle event. The server writes it
atomically to `user_data_dir/now_playing.json`. On startup the
frontend reads it back via `GET /api/now-playing/state` and
prefers it when localStorage is empty.

Format is intentionally opaque: the server doesn't parse the
fields, just round-trips the dict. The frontend is the only
consumer; the server's job is durable storage that survives
across launches regardless of the WebView's quirks.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Any, Optional

from app.paths import user_data_dir

log = logging.getLogger(__name__)

_FILE = user_data_dir() / "now_playing.json"
_lock = threading.Lock()


def write_state(state: dict[str, Any]) -> None:
    """Atomically persist `state` to disk. Writes to a sibling tmp
    file then os.replace so a crash mid-write can't corrupt the
    file. Errors are logged and swallowed — persistence is a
    nice-to-have, not a hard requirement.
    """
    if not isinstance(state, dict):
        return
    target = _FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".now_playing.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp_path, target)
        except Exception:
            log.exception("now-playing state write failed")
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def read_state() -> Optional[dict[str, Any]]:
    """Return the last persisted state, or None when no file exists
    or it can't be parsed. Empty / corrupt files are treated as
    None — the frontend then falls back to localStorage or a clean
    empty player.
    """
    if not _FILE.exists():
        return None
    with _lock:
        try:
            with open(_FILE, encoding="utf-8") as f:
                obj = json.load(f)
        except Exception:
            log.warning("now-playing state read failed; ignoring")
            return None
    if not isinstance(obj, dict):
        return None
    return obj


def clear_state() -> None:
    """Remove the persisted file. Called when the user explicitly
    stops playback so a relaunch doesn't restore something they
    just dismissed."""
    with _lock:
        try:
            _FILE.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("now-playing state clear failed")
