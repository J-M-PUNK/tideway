"""Platform specific paths for persistent app state.

Packaged desktop builds cannot reliably write next to the
executable. On macOS the .app bundle is read only once it has
been code signed. On Windows an installer typically lands the
exe under Program Files, and writes there require elevation. So
all mutable state lives in the standard per user data directory.
That includes settings, the Tidal session, the pending download
queue, and anything else the app persists.

`migrate_legacy_cwd_state()` runs implicitly on first import and
handles two older layouts. The first is individual state files
left in the process cwd by pre packaging dev installs. The
second is the full previous app data directory from before the
rename, which gets copied forward so the Tidal session and
settings survive the move to Tideway. The function is safe to
call more than once. After the first call it is a no-op.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
from pathlib import Path

_APP_NAME = "Tideway"
# Previous on-disk folder name. Kept so migrate_legacy_cwd_state()
# can copy the old per-user directory forward across the rename.
_LEGACY_APP_NAME = "TidalDownloader"


def _app_data_root() -> Path:
    """Platform-specific base dir that holds the per-app data folder.

    Returns the parent — caller appends the app name. Split out so the
    current-name and legacy-name lookups can't drift (they share the
    same OS picker logic)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return Path(base) if base else Path.home() / "AppData" / "Roaming"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    base = os.environ.get("XDG_DATA_HOME")
    return Path(base) if base else Path.home() / ".local" / "share"


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

    Windows:  %APPDATA%\\Tideway  (typically C:\\Users\\<u>\\AppData\\Roaming)
    macOS:    ~/Library/Application Support/Tideway
    Linux:    $XDG_DATA_HOME/Tideway, else ~/.local/share/Tideway
    """
    path = _app_data_root() / _APP_NAME
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


def _legacy_app_data_root() -> Path:
    """Sibling directory of user_data_dir() under the previous app
    name, without creating it. Used by the one-shot rename migration."""
    return _app_data_root() / _LEGACY_APP_NAME


def migrate_legacy_cwd_state() -> None:
    """Copy older state layouts into the current user_data_dir().

    There are two layouts this handles. The first is individual
    files sitting in the process cwd, which is where pre packaging
    dev installs used to keep things. The second is a full
    previous per user data directory under the old `TidalDownloader`
    name. That one is copied forward so the Tidal session,
    settings, and download queue survive the rename to Tideway.

    If a file already exists at the destination it is left alone.
    That way a stray file in the launch cwd cannot clobber real
    user data. Copy errors are swallowed. Missing state degrades
    to first run defaults, and a failed copy should never take
    the app down at boot.
    """
    global _migrated
    with _migration_lock:
        if _migrated:
            return
        _migrated = True
        dest_root = user_data_dir()
        # (2) Pull forward a full previous-name app-data directory in
        # one shot. Done first so individual cwd files (from even
        # older layouts) can still override specific entries if the
        # user has newer copies lying around.
        legacy_root = _legacy_app_data_root()
        if legacy_root.is_dir() and legacy_root != dest_root:
            for entry in legacy_root.iterdir():
                dst = dest_root / entry.name
                if dst.exists():
                    continue
                try:
                    if entry.is_dir():
                        shutil.copytree(entry, dst)
                    else:
                        shutil.copy2(entry, dst)
                except Exception:
                    pass
        # (1) Pull individual files from the process cwd.
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
