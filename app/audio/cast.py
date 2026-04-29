"""Chromecast sender.

Discovers Cast devices on the LAN, exposes them to the frontend
as a picker, and (in a later phase of this work) routes Tideway's
audio to a selected device by handing the Cast Default Media
Receiver an HTTP URL serving Tideway's encoded stream.

The Cast story splits into two responsibilities. This file owns
both behind one process-wide singleton:

  Discovery — keep an up-to-date list of Cast devices on the LAN.
              pychromecast's `CastBrowser` does the mDNS work; we
              translate its callbacks into a thread-safe dict and
              hand snapshots to the API layer. Discovery is cheap
              and runs continuously so the picker is up-to-date
              when the user opens it.

  Session   — once the user picks a device, connect to it, send
              `play_media` against the encoded-stream URL, and
              relay state changes (paused, volume, disconnect)
              back to the player engine. This part lands in a
              follow-up commit; right now we expose discovery
              only so the picker has something real to show
              before any audio routing exists.

The module mirrors `app/audio/upnp.py`'s pattern of degrading
gracefully when the optional dep is missing — if pychromecast
fails to import (wheel mismatch, hostile pip environment, etc.)
the manager still constructs but `list_devices()` returns []
and `start_discovery()` is a no-op. The rest of the app boots
fine; the picker just shows "no Cast devices found."
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# pychromecast is an optional dep — if the wheel is unavailable
# (it isn't, on the platforms we ship to today, but pip environments
# in the wild are unpredictable) the rest of the audio stack still
# works. The Cast picker will just show as empty in the UI.
try:
    import pychromecast
    from pychromecast.discovery import CastBrowser, SimpleCastListener
    import zeroconf

    _CAST_AVAILABLE = True
except Exception as _exc:  # pragma: no cover - environment dependent
    log.warning("pychromecast unavailable: %s", _exc)
    _CAST_AVAILABLE = False


@dataclass(frozen=True)
class CastDevice:
    """Concrete Cast target the user can pick from the now-playing
    devices menu.

    `id` is the Cast UUID — stable across reboots, comes from the
    device firmware. We use it as the persistence key for "remember
    last device." `friendly_name` is what the user sees ("Living
    Room speaker", "Kitchen Nest Mini"). The rest is metadata that
    helps disambiguate (two Nest Minis next to each other) and
    is logged for support purposes.
    """

    id: str
    friendly_name: str
    model_name: str
    manufacturer: str
    cast_type: str  # "audio", "cast" (video device), "group"
    host: str
    port: int


# Marker value used by `is_audio_only` for Cast types that don't
# pass through video. Keeping it as a small set rather than a
# substring check on model name — the model name is locale-dependent
# ("Nest Audio", "Google Home Mini", "Chromecast Audio") and a
# substring match on "audio" would miss any third-party Cast-
# enabled speaker that doesn't use the word.
_AUDIO_CAST_TYPES = frozenset({"audio", "group"})


def is_audio_only(device: CastDevice) -> bool:
    """True for speakers and speaker groups, false for Cast-built-in
    TVs and standalone Chromecast HDMI dongles. Used by the picker
    to put audio targets first — most users casting from a music
    app want a speaker, not their TV."""
    return device.cast_type in _AUDIO_CAST_TYPES


class CastManager:
    """Process-wide owner of Cast discovery.

    Construct once at server boot. `start_discovery()` is non-
    blocking and spawns a zeroconf browser thread; the manager
    accumulates devices as they appear and prunes them on the
    `remove_cast` callback. `list_devices()` returns a snapshot
    that's safe to hand to a request thread.

    Stopping is best-effort: zeroconf's browser holds OS-level
    sockets and can take a moment to release. We don't block
    server shutdown on it — the daemon flag handles cleanup if
    the OS reaps us before zeroconf finishes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: dict[str, CastDevice] = {}
        self._browser: Optional["CastBrowser"] = None
        self._zconf: Optional["zeroconf.Zeroconf"] = None
        # Track the last time we received an "add_cast" or
        # "update_cast" callback for any device. Useful for the
        # diagnostic endpoint that reports "discovery has been
        # quiet for N seconds, are you on the right network?"
        self._last_event_at: float = 0.0

    # ---- lifecycle --------------------------------------------------

    def start_discovery(self) -> None:
        """Begin browsing for Cast devices on the LAN. Idempotent —
        a second call while the browser is already running is a
        no-op (logs a debug line)."""
        if not _CAST_AVAILABLE:
            print("[cast] pychromecast unavailable, discovery skipped",
                  flush=True)
            return
        with self._lock:
            if self._browser is not None:
                log.debug("cast discovery already running")
                return
            try:
                self._zconf = zeroconf.Zeroconf()
                listener = SimpleCastListener(
                    add_callback=self._on_add,
                    remove_callback=self._on_remove,
                    update_callback=self._on_update,
                )
                self._browser = CastBrowser(listener, self._zconf)
                self._browser.start_discovery()
                print("[cast] discovery started", flush=True)
            except Exception as exc:
                # Zeroconf can fail to bind on locked-down networks
                # (managed corporate Wi-Fi, VPNs that block multicast)
                # or if another process has the mDNS port. Fall
                # through with no browser — list_devices() returns
                # [] and the picker shows empty, which is the right
                # UX for "no Cast devices reachable."
                print(f"[cast] discovery failed to start: {exc!r}",
                      flush=True)
                self._zconf = None
                self._browser = None

    def stop_discovery(self) -> None:
        """Tear down discovery. Called from the FastAPI shutdown
        hook — keeps zeroconf from leaking sockets across process
        restarts in dev runs."""
        with self._lock:
            browser = self._browser
            zconf = self._zconf
            self._browser = None
            self._zconf = None
        if browser is not None:
            try:
                browser.stop_discovery()
            except Exception as exc:
                log.debug("cast browser stop failed: %r", exc)
        if zconf is not None:
            try:
                zconf.close()
            except Exception as exc:
                log.debug("zeroconf close failed: %r", exc)

    # ---- public surface --------------------------------------------

    def list_devices(self) -> list[CastDevice]:
        """Snapshot of the currently-known devices, sorted with
        audio-only targets first then alphabetical by friendly
        name. The sort is for the picker UX — speakers above TVs,
        which is what users casting from a music app expect."""
        with self._lock:
            devices = list(self._devices.values())
        devices.sort(
            key=lambda d: (0 if is_audio_only(d) else 1,
                           d.friendly_name.lower())
        )
        return devices

    def get_device(self, device_id: str) -> Optional[CastDevice]:
        """Lookup by Cast UUID. Returns None if the device hasn't
        been discovered yet (or has gone offline since)."""
        with self._lock:
            return self._devices.get(device_id)

    def status(self) -> dict[str, object]:
        """Diagnostic snapshot for /api/cast/status. Surfaces enough
        to debug "I don't see my speaker" without needing logs:
        whether the browser is running, how many devices we know
        about, when we last saw a discovery event."""
        with self._lock:
            return {
                "available": _CAST_AVAILABLE,
                "running": self._browser is not None,
                "device_count": len(self._devices),
                "last_event_age_s": (
                    None if self._last_event_at == 0.0
                    else round(time.monotonic() - self._last_event_at, 1)
                ),
            }

    # ---- pychromecast callbacks -------------------------------------

    def _on_add(self, uuid, _service) -> None:
        self._upsert(uuid)

    def _on_update(self, uuid, _service) -> None:
        self._upsert(uuid)

    def _on_remove(self, uuid, _service, _cast_info) -> None:
        sid = str(uuid)
        with self._lock:
            existing = self._devices.pop(sid, None)
            self._last_event_at = time.monotonic()
        if existing is not None:
            print(f"[cast] removed: {existing.friendly_name} ({sid})",
                  flush=True)

    def _upsert(self, uuid) -> None:
        """Translate pychromecast's CastInfo into our CastDevice and
        cache it. Called from zeroconf's listener thread, so the
        actual cache mutation is locked.

        We pull the CastInfo back out of `browser.devices[uuid]`
        instead of trusting the callback's `service` arg because
        in pychromecast 14.x the CastBrowser's `devices` dict is
        the canonical post-resolution view (the listener's `service`
        arg can fire with a partial record before the full device
        info is resolved)."""
        if self._browser is None:
            return
        try:
            info = self._browser.devices.get(uuid)
        except Exception:
            info = None
        if info is None:
            return
        device = CastDevice(
            id=str(info.uuid),
            friendly_name=info.friendly_name or "Cast device",
            model_name=info.model_name or "",
            manufacturer=info.manufacturer or "",
            cast_type=info.cast_type or "cast",
            host=info.host or "",
            port=int(info.port or 8009),
        )
        with self._lock:
            previously_known = device.id in self._devices
            self._devices[device.id] = device
            self._last_event_at = time.monotonic()
        if not previously_known:
            print(f"[cast] discovered: {device.friendly_name} "
                  f"({device.model_name or 'unknown model'}) "
                  f"@ {device.host}:{device.port}", flush=True)


# Module-level singleton. server.py calls `cast_manager.start_discovery()`
# at boot and `cast_manager.stop_discovery()` on shutdown. Importing
# this module is cheap (no network, no thread) so the singleton is
# safe to construct even in test runs that don't actually exercise
# Cast.
cast_manager = CastManager()
