"""Platform-specific paths for persistent app state.

Packaged desktop builds can't reliably write next to the executable —
macOS .app bundles are readonly once code-signed, and on Windows an
installer typically lands the exe under Program Files where per-user
writes require elevation. All mutable state (settings, Tidal session,
the pending download queue) therefore lives in the OS's standard
per-user data directory.

Legacy builds wrote these files to the process cwd. `migrate_legacy_cwd_state()`
performs a one-shot copy from cwd into the new location so existing dev
installs don't lose their session on upgrade. Runs implicitly on first
import — safe to call repeatedly; subsequent calls are no-ops.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
from pathlib import Path

_APP_NAME = "TidalDownloader"


def bundled_resource_dir() -> Path:
    """Directory holding bundled read-only resources (web/dist, icons).

    PyInstaller unpacks data files under sys._MEIPASS at runtime; the
    repo root is the natural equivalent when running from source. Any
    caller that needs to find a shipped asset should compose a path
    against this directory rather than `__file__`, so the same code
    works frozen and unfrozen.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    # __file__ is app/paths.py; the repo root is two levels up.
    return Path(__file__).resolve().parent.parent

_migration_lock = threading.Lock()
_migrated = False


def user_data_dir() -> Path:
    """Return the OS-appropriate per-user data directory, creating it if missing.

    Windows:  %APPDATA%\\TidalDownloader  (typically C:\\Users\\<u>\\AppData\\Roaming)
    macOS:    ~/Library/Application Support/TidalDownloader
    Linux:    $XDG_DATA_HOME/TidalDownloader, else ~/.local/share/TidalDownloader
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME")
        root = Path(base) if base else Path.home() / ".local" / "share"
    path = root / _APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


# Filenames we know how to migrate from the legacy cwd layout. Listing them
# explicitly (rather than globbing) means an unrelated settings.json in the
# launch cwd won't be pulled into our data dir by accident.
_LEGACY_FILES = (
    "settings.json",
    "tidal_session.json",
    "download_queue.json",
)


def migrate_legacy_cwd_state() -> None:
    """One-shot copy of legacy cwd state files into user_data_dir().

    Skips any file that already exists at the destination, so a packaged
    user who happens to launch from a dir with a stray settings.json
    won't clobber their real settings. Copy errors are swallowed —
    missing state degrades to first-run defaults, never a boot failure.
    """
    global _migrated
    with _migration_lock:
        if _migrated:
            return
        _migrated = True
        dest_root = user_data_dir()
        for name in _LEGACY_FILES:
            src = Path(name)
            dst = dest_root / name
            if dst.exists() or not src.exists():
                continue
            try:
                shutil.copy2(src, dst)
            except Exception:
                pass


migrate_legacy_cwd_state()
