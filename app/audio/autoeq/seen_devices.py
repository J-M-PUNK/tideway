"""Track which output devices the user has seen, for the AutoEQ
per-device profile picker.

The picker UI needs a list of "all the headphones / speakers /
DACs you've used recently" so the user can map a profile to each
without having to plug everything in simultaneously. This module
maintains that list.

Storage shape — a small JSON file in `user_data_dir()`:

    [
      {
        "fingerprint": "Scarlett Solo USB",
        "display_name": "Scarlett Solo USB",
        "kind": "usb",
        "first_seen": 1735000000,
        "last_seen": 1735999999
      },
      ...
    ]

Fingerprint = device name as `sounddevice.query_devices()` reports
it. That's stable across reconnects on every platform we ship to,
which is good enough for v1 — the alternative (some hash of
USB-VID/PID + endpoint id) would be more robust but requires
platform-specific probing.

`upsert(active_devices)` is called from the audio engine whenever
it enumerates devices. It updates `last_seen` for known devices
and inserts new ones with `first_seen = last_seen = now`.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from app.paths import user_data_dir

log = logging.getLogger(__name__)


_FILE_NAME = "autoeq_seen_devices.json"


def _store_path() -> Path:
    return user_data_dir() / _FILE_NAME


def _classify_kind(name: str) -> str:
    """Best-effort classification of a device name into one of
    `bt` / `usb` / `builtin` / `unknown`. Used by the picker UI
    to show the right icon and to surface a one-time toast on
    BT devices warning about reduced EQ accuracy.

    Heuristics, not bulletproof. A USB DAC named "AirPods Bose"
    would misclassify, but the name match is what users would
    expect."""
    lower = name.lower()
    if any(
        marker in lower
        for marker in ("bluetooth", "airpods", "wh-1000", "qc", "buds")
    ):
        return "bt"
    if "usb" in lower or "scarlett" in lower or "dac" in lower:
        return "usb"
    if any(
        marker in lower
        for marker in ("speakers", "internal", "built-in", "macbook")
    ):
        return "builtin"
    return "unknown"


class SeenDeviceStore:
    """Thread-safe JSON-backed list of seen output devices."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Keyed by fingerprint for O(1) upsert. Same dataset gets
        # serialised as a JSON list ordered by last_seen desc.
        self._records: dict[str, dict] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        path = _store_path()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    for entry in raw:
                        fp = entry.get("fingerprint")
                        if isinstance(fp, str) and fp:
                            self._records[fp] = entry
            except Exception as exc:
                log.warning(
                    "autoeq seen-devices: load failed, starting empty: %s",
                    exc,
                )
        self._loaded = True

    def _flush(self) -> None:
        """Write the current records to disk, ordered by last_seen
        desc so the picker list is naturally sorted on read."""
        path = _store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(
            self._records.values(),
            key=lambda r: r.get("last_seen", 0),
            reverse=True,
        )
        try:
            path.write_text(
                json.dumps(ordered, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("autoeq seen-devices: write failed: %s", exc)

    def upsert(
        self,
        fingerprint: str,
        display_name: Optional[str] = None,
    ) -> None:
        """Record (or update) a device sighting. `display_name`
        defaults to the fingerprint — they're usually the same
        but the parameter exists for callers that have a richer
        display string."""
        if not fingerprint:
            return
        now = int(time.time())
        with self._lock:
            self._ensure_loaded()
            entry = self._records.get(fingerprint)
            if entry is None:
                entry = {
                    "fingerprint": fingerprint,
                    "display_name": display_name or fingerprint,
                    "kind": _classify_kind(fingerprint),
                    "first_seen": now,
                    "last_seen": now,
                }
                self._records[fingerprint] = entry
            else:
                entry["last_seen"] = now
                if display_name:
                    entry["display_name"] = display_name
            self._flush()

    def list(self) -> list[dict]:
        """Return all known devices, ordered by last_seen desc."""
        with self._lock:
            self._ensure_loaded()
            return sorted(
                (dict(r) for r in self._records.values()),
                key=lambda r: r.get("last_seen", 0),
                reverse=True,
            )

    def forget(self, fingerprint: str) -> bool:
        """Remove a device from the seen list. Returns True if
        anything was removed. Used by the picker's "Forget device"
        affordance — does NOT touch the device-mapping settings,
        callers prune those separately."""
        with self._lock:
            self._ensure_loaded()
            if fingerprint not in self._records:
                return False
            del self._records[fingerprint]
            self._flush()
            return True

    def clear(self) -> None:
        """Wipe the entire seen-devices list. Test-only helper."""
        with self._lock:
            self._records.clear()
            self._loaded = True
            self._flush()


# Module-level singleton — all upsert/list calls share state.
STORE = SeenDeviceStore()
