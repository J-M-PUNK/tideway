"""Auto-start on login — per-user, no admin required.

- **macOS**: `LaunchAgent` plist in `~/Library/LaunchAgents/`. Loaded
  via `launchctl load`. Agents in that dir run in the user's session
  at login. No code signing / SMAppService dance — that would require
  a helper bundle and entitlements we don't ship.
- **Windows**: a value in `HKEY_CURRENT_USER\Software\Microsoft\Windows\
  CurrentVersion\Run`. Standard per-user autostart path, unaffected
  by UAC, works in both signed and unsigned builds.
- **Linux**: an XDG autostart `.desktop` file in `~/.config/autostart/`.
  Respected by GNOME / KDE / XFCE / etc.

Only meaningful when we can locate the installed executable — i.e.
in a packaged build. In dev mode (`python desktop.py`) there's no
stable exe path so is_enabled/set_enabled report as unavailable.
"""
from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


LABEL = "com.tidaldownloader.app"
WIN_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WIN_REG_VALUE = "Tideway"


def _executable_path() -> Optional[str]:
    """Path we'd register for autostart. Returns None in dev mode so
    the UI can disable the toggle rather than silently registering the
    system Python interpreter to launch on login."""
    if not getattr(sys, "frozen", False):
        return None
    # sys.executable inside a PyInstaller bundle points at the binary
    # that macOS / Windows actually launches. On macOS the bundle
    # structure means we want ..../Tideway.app/Contents/MacOS/
    # Tideway — which is what sys.executable gives us.
    exe = sys.executable
    if not exe:
        return None
    return exe


def _mac_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _mac_is_enabled() -> bool:
    return _mac_plist_path().is_file()


def _mac_set_enabled(enabled: bool, exe: str) -> None:
    plist_path = _mac_plist_path()
    if enabled:
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "Label": LABEL,
            "ProgramArguments": [exe],
            "RunAtLoad": True,
            # KeepAlive omitted — we only want to launch at login, not
            # restart the app if the user quits it.
            "ProcessType": "Interactive",
        }
        with open(plist_path, "wb") as f:
            plistlib.dump(data, f)
        # launchctl load is optional at write time; macOS picks up the
        # agent on next login regardless. Try it anyway so toggling on
        # takes effect immediately in the current session if launchctl
        # is available. Ignore failures — this is best-effort.
        try:
            subprocess.run(
                ["launchctl", "load", "-w", str(plist_path)],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass
    else:
        if plist_path.is_file():
            try:
                subprocess.run(
                    ["launchctl", "unload", "-w", str(plist_path)],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                pass
            try:
                plist_path.unlink()
            except OSError:
                pass


def _windows_is_enabled() -> bool:
    try:
        import winreg  # type: ignore
    except Exception:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_REG_KEY) as k:
            try:
                winreg.QueryValueEx(k, WIN_REG_VALUE)
                return True
            except FileNotFoundError:
                return False
    except OSError:
        return False


def _windows_set_enabled(enabled: bool, exe: str) -> None:
    try:
        import winreg  # type: ignore
    except Exception:
        return
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, WIN_REG_KEY, 0, winreg.KEY_SET_VALUE
    ) as k:
        if enabled:
            # Quote the path — spaces in "Program Files" break the exec
            # without quoting.
            winreg.SetValueEx(
                k, WIN_REG_VALUE, 0, winreg.REG_SZ, f'"{exe}"'
            )
        else:
            try:
                winreg.DeleteValue(k, WIN_REG_VALUE)
            except FileNotFoundError:
                pass


def _linux_desktop_path() -> Path:
    return (
        Path.home() / ".config" / "autostart" / "tidal-downloader.desktop"
    )


def _linux_is_enabled() -> bool:
    return _linux_desktop_path().is_file()


def _linux_set_enabled(enabled: bool, exe: str) -> None:
    desktop = _linux_desktop_path()
    if enabled:
        desktop.parent.mkdir(parents=True, exist_ok=True)
        # Desktop Entry spec: quote any argument containing spaces or
        # reserved characters. A raw path like `/opt/Tidal Downloader/
        # run` would otherwise be parsed as three separate argv
        # tokens. Escape embedded double-quotes + backslashes per the
        # spec's quoting rules.
        quoted_exe = (
            '"' + exe.replace("\\", "\\\\").replace('"', '\\"') + '"'
        )
        desktop.write_text(
            f"""[Desktop Entry]
Type=Application
Name=Tideway
Exec={quoted_exe}
X-GNOME-Autostart-enabled=true
""",
            encoding="utf-8",
        )
        try:
            os.chmod(desktop, 0o755)
        except OSError:
            pass
    else:
        if desktop.is_file():
            try:
                desktop.unlink()
            except OSError:
                pass


def status() -> dict:
    """Report what the UI should show.

    `available` is False in dev mode (where there's no stable exe
    path). The UI uses this to grey out the toggle rather than let
    the user register a path that won't work on next login.
    """
    exe = _executable_path()
    if exe is None:
        return {"available": False, "enabled": False, "path": None}
    if sys.platform == "darwin":
        enabled = _mac_is_enabled()
    elif sys.platform.startswith("win"):
        enabled = _windows_is_enabled()
    else:
        enabled = _linux_is_enabled()
    return {"available": True, "enabled": enabled, "path": exe}


def set_enabled(enabled: bool) -> dict:
    exe = _executable_path()
    if exe is None:
        return {"available": False, "enabled": False, "path": None}
    try:
        if sys.platform == "darwin":
            _mac_set_enabled(enabled, exe)
        elif sys.platform.startswith("win"):
            _windows_set_enabled(enabled, exe)
        else:
            _linux_set_enabled(enabled, exe)
    except Exception as exc:
        log.exception("autostart toggle failed")
        raise RuntimeError(f"Couldn't toggle autostart: {exc}") from exc
    return status()


__all__ = ["status", "set_enabled"]
