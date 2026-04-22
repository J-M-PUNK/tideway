"""Cross-platform system notifications.

Zero new dependencies — shells out to the native command on each
platform. Every path is fire-and-forget: failures are swallowed so a
broken notification stack can't break playback or downloads.

- macOS: `osascript -e 'display notification ...'`. Works on every
  macOS version we care about. Same mechanism pystray uses internally,
  so if the tray icon works, notifications work too.
- Windows: PowerShell toast via the WinRT ToastNotificationManager.
  Only requires Windows 10+ (the supported matrix for pywebview).
- Linux: `notify-send` (libnotify). Present in every modern DE.

Payload is intentionally minimal — title + body + optional subtitle.
We explicitly don't expose click handlers, actions, or persistent
notifications. The app already has an in-window toast system for
anything that needs an affordance; OS notifications are one-shot
announcements ("track changed", "downloads done") that dismiss on
their own.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from typing import Optional

log = logging.getLogger(__name__)


def notify(title: str, body: str, subtitle: Optional[str] = None) -> None:
    """Fire a best-effort OS notification. Never raises."""
    try:
        if sys.platform == "darwin":
            _notify_macos(title, body, subtitle)
        elif sys.platform.startswith("win"):
            _notify_windows(title, body, subtitle)
        else:
            _notify_linux(title, body, subtitle)
    except Exception as exc:  # pragma: no cover - env-dependent
        log.debug("notification failed: %s", exc)


def _applescript_escape(s: str) -> str:
    """Escape a string for embedding in AppleScript string literals.

    AppleScript uses double-quoted strings with `\\` + `"` as the only
    special characters. Newlines aren't allowed inside a literal —
    replace them with a space.
    """
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _notify_macos(title: str, body: str, subtitle: Optional[str]) -> None:
    parts = [f'display notification "{_applescript_escape(body)}"']
    parts.append(f'with title "{_applescript_escape(title)}"')
    if subtitle:
        parts.append(f'subtitle "{_applescript_escape(subtitle)}"')
    script = " ".join(parts)
    subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        timeout=5,
    )


def _powershell_escape(s: str) -> str:
    """Escape for PowerShell single-quoted strings where `'` doubles."""
    return s.replace("'", "''")


def _notify_windows(title: str, body: str, subtitle: Optional[str]) -> None:
    # Toast XML — built inline so we don't depend on BurntToast. App-id
    # must exist in the Start menu for the toast to surface persistently;
    # passing "Tideway" is fine for transient "feels like a
    # system notification" UX even without AUMID registration.
    text_body = body if not subtitle else f"{subtitle} — {body}"
    ps = f"""\
$ErrorActionPreference = 'SilentlyContinue'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$template = @'
<toast><visual><binding template="ToastText02"><text id="1">{_powershell_escape(title)}</text><text id="2">{_powershell_escape(text_body)}</text></binding></visual></toast>
'@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Tideway').Show($toast)
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        check=False,
        capture_output=True,
        timeout=5,
    )


def _notify_linux(title: str, body: str, subtitle: Optional[str]) -> None:
    body_text = body if not subtitle else f"{subtitle}\n{body}"
    subprocess.run(
        ["notify-send", title, body_text],
        check=False,
        capture_output=True,
        timeout=5,
    )
