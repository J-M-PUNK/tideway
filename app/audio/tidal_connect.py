"""Tidal Connect — controller-side scaffolding.

Eventual goal: from Tideway, pick a Tidal Connect device on the LAN
and have THAT device fetch the Tidal stream directly using its own
paired Tidal session. Tideway is just a remote control; no audio
encoding or HTTP serving (unlike the Cast sender).

This is fundamentally different from `app/audio/cast.py`:

  Cast:           Tideway encodes PCM to FLAC, serves over HTTP, Cast
                  device pulls and plays. Tideway is the audio source.

  Tidal Connect:  Tideway issues OpenHome SOAP commands ('play this
                  Tidal track ID') to a paired device. The device
                  fetches the stream from Tidal's CDN with its own
                  credentials and plays it. Tideway is just the
                  controller; the audio engine is dormant.

What this file actually contains right now is **Phase 1 + 2-discovery-
only** from `docs/cast-and-connect-scope.md`. Specifically:

  - SSDP-based discovery of OpenHome-capable MediaRenderer devices
    on the LAN. OpenHome (urn:av-openhome-org:service:*) is the
    strongest "this device probably does Tidal Connect" signal we
    can detect without partner documentation. False positives are
    possible — Linn / Naim / Bluesound speakers all advertise
    OpenHome and most (but not all) are Tidal-Connect-paired.
  - The thread-safe device cache + status payload that backs the
    /api/tidal-connect/devices endpoint and the picker UI.
  - Connect / disconnect stubs that return a clear 'protocol work
    not yet implemented' marker so users testing the picker get a
    deterministic message instead of a silent failure.

What this file does NOT yet contain (gated on Phase 1 protocol
scoping with packet capture against the official Tidal desktop app
+ a real Tidal Connect device):

  - The OpenHome SOAP command set the Tidal app actually sends.
  - The pairing handshake. May be hypothesis A (signed stream URL,
    Tideway's tidalapi session covers everything) or hypothesis B
    (device-cert-signed pairing, hard stop). The capture answers
    which.
  - Track-handoff flow: how the device gets the right stream URL
    with the right metadata.
  - State subscription: how 'paused on the device' or 'skipped via
    device buttons' surface back to the controller.

Module degrades gracefully when async-upnp-client is missing — the
manager still constructs but discovery is a no-op.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# async-upnp-client is the same dep the existing UPnP MediaRenderer
# module uses. If it's missing, both UPnP and Tidal Connect become
# silently unavailable. Keep the rest of the app booting.
try:
    from async_upnp_client.aiohttp import AiohttpRequester
    from async_upnp_client.client_factory import UpnpFactory
    from async_upnp_client.search import async_search

    _AVAILABLE = True
except Exception as _exc:  # pragma: no cover - environment dependent
    log.warning("async-upnp-client unavailable for tidal_connect: %s", _exc)
    AiohttpRequester = None  # type: ignore
    UpnpFactory = None  # type: ignore
    async_search = None  # type: ignore
    _AVAILABLE = False


# SSDP search target. We start broad — anything claiming to be a
# MediaRenderer — and filter post-hoc for OpenHome services. A more
# specific target like `urn:av-openhome-org:service:Product:1` would
# work too, but some Tidal Connect devices respond more reliably to
# the broader MediaRenderer query.
_ST_MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"

# OpenHome service-type prefix. A device advertising any service
# under this namespace is OpenHome-capable. Empirically, OpenHome-
# capable + MediaRenderer is the closest we can get to "Tidal
# Connect target" without partner specs. False positives are
# possible (some pure-OpenHome speakers without Tidal-pair support
# would also match), so we expose this heuristic explicitly in the
# device record so the UI can decorate accordingly.
_OPENHOME_NAMESPACE_PREFIX = "urn:av-openhome-org:service:"

# Specific OpenHome service that Tidal Connect targets in particular
# tend to expose. The Credentials service is where the user's
# paired Tidal session lives on the device. If a device has this
# service we have a much stronger 'Tidal-aware' signal than just
# OpenHome+MediaRenderer.
_OPENHOME_CREDENTIALS_SERVICE = (
    "urn:av-openhome-org:service:Credentials:"
)


@dataclass(frozen=True)
class TidalConnectDevice:
    """Concrete Tidal Connect target the user can pick from the
    Sound output picker.

    `id` is the UPnP UDN — stable across reboots. `friendly_name`
    is what the user sees in the device's own admin UI. `has_credentials_service`
    is the strongest signal we have without partner specs that the
    device actually pairs with Tidal accounts; `is_openhome` is the
    weaker, more inclusive signal.
    """

    id: str
    friendly_name: str
    manufacturer: str
    model: str
    location: str  # device description URL — needed for control plane later
    is_openhome: bool
    has_credentials_service: bool
    service_types: tuple[str, ...]


class TidalConnectManager:
    """Process-wide owner of Tidal Connect discovery and (eventually)
    sessions.

    Construct once at server boot. Discovery runs on demand —
    `refresh(timeout=5.0)` triggers a fresh SSDP scan. The picker
    polls this through the API endpoint when the dropdown opens, so
    we don't keep a continuous browser running like Cast does. Why
    different: SSDP is multicast and produces more network noise per
    scan than mDNS does, and Tidal Connect devices are a smaller set
    that the user typically already knows about, so on-demand
    refresh is enough.

    Connect / disconnect are currently stubs — see module docstring.
    The shape is in place so future protocol work can fill in
    behaviour without restructuring callers.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: dict[str, TidalConnectDevice] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._last_scan_at: float = 0.0
        # Active session placeholder. Currently always None; the
        # connect() stub doesn't transition this. Future work fills
        # in a session state object similar to cast._SessionState.
        self._session_id: Optional[str] = None
        if _AVAILABLE:
            self._start_loop_thread()

    # ---- lifecycle ---------------------------------------------------

    def is_available(self) -> bool:
        """False when async-upnp-client failed to import. The picker
        hides the Tidal Connect section in that case."""
        return _AVAILABLE

    def status(self) -> dict[str, object]:
        """Diagnostic snapshot. Surfaces enough to debug 'I don't
        see my speaker' without needing logs: whether discovery is
        even possible (lib installed), how recently we last
        scanned, how many devices we have cached."""
        with self._lock:
            return {
                "available": _AVAILABLE,
                "device_count": len(self._devices),
                "last_scan_age_s": (
                    None if self._last_scan_at == 0.0
                    else round(time.monotonic() - self._last_scan_at, 1)
                ),
                "connected_id": self._session_id,
                # Phase 2+ surface for the frontend so it can show
                # 'Tidal Connect routing not yet implemented' or
                # similar, instead of the picker silently failing
                # on click.
                "control_plane_ready": False,
            }

    def list_devices(self) -> list[TidalConnectDevice]:
        """Snapshot of currently-known Tidal Connect candidates. Sorted
        with the strongest-signal devices (Credentials service
        present) first, then alphabetical."""
        with self._lock:
            devices = list(self._devices.values())
        devices.sort(
            key=lambda d: (
                0 if d.has_credentials_service else 1,
                d.friendly_name.lower(),
            )
        )
        return devices

    def get_device(self, device_id: str) -> Optional[TidalConnectDevice]:
        with self._lock:
            return self._devices.get(device_id)

    # ---- discovery ---------------------------------------------------

    def refresh(self, timeout: float = 5.0) -> list[TidalConnectDevice]:
        """Run an SSDP scan and replace the device cache with the
        result. Blocks the caller for at most `timeout` seconds.
        Returns the new device list. No-op if the dep isn't
        available."""
        if not _AVAILABLE or self._loop is None:
            return []
        future = asyncio.run_coroutine_threadsafe(
            self._discover_async(timeout), self._loop
        )
        try:
            devices = future.result(timeout=timeout + 5.0)
        except Exception as exc:
            log.warning("tidal_connect discover failed: %s", exc)
            return []
        with self._lock:
            self._devices = {d.id: d for d in devices}
            self._last_scan_at = time.monotonic()
        return devices

    # ---- session stubs (Phase 2 protocol work) ----------------------

    def connect(self, device_id: str) -> TidalConnectDevice:
        """Open a control session against the given device.

        Currently a stub — raises NotImplementedError with a clear
        message that the protocol layer is pending. The frontend
        translates this into a 'Tidal Connect routing isn't ready
        yet' toast on click, so the picker is testable end-to-end
        for discovery without producing silent failures.

        Phase 2 work fills this in: SOAP control client, OpenHome
        service descriptor parsing, the Tidal-specific track-handoff
        commands once Phase 1 packet capture answers what they look
        like.
        """
        device = self.get_device(device_id)
        if device is None:
            raise ValueError(f"unknown tidal connect device: {device_id}")
        raise NotImplementedError(
            "Tidal Connect control plane isn't implemented yet. The "
            "device was discovered correctly, but issuing play / pause / "
            "track-load commands needs the OpenHome SOAP client + "
            "pairing flow which require packet capture against a real "
            "Tidal Connect target. See docs/cast-and-connect-scope.md."
        )

    def disconnect(self) -> None:
        """Tear down the current Tidal Connect session. Idempotent.
        Currently a no-op — there's no session to tear down. Shape
        is in place for the eventual protocol work."""
        with self._lock:
            self._session_id = None

    # ---- internals --------------------------------------------------

    def _start_loop_thread(self) -> None:
        """Dedicated asyncio loop for SSDP work. Same pattern
        UpnpManager uses — mirrors it intentionally so the two
        modules behave the same way under load."""
        ready = threading.Event()

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ready.set()
            try:
                loop.run_forever()
            finally:
                loop.close()

        t = threading.Thread(
            target=_run, name="tidal-connect-asyncio", daemon=True
        )
        t.start()
        ready.wait(timeout=2.0)
        self._loop_thread = t

    async def _discover_async(
        self, timeout: float
    ) -> list[TidalConnectDevice]:
        """SSDP search + per-device resolve. Returns only devices
        that look Tidal-Connect-shaped (OpenHome capable). Plain
        UPnP MediaRenderers without OpenHome are filtered out here
        — they belong to the existing UPnP module's jurisdiction,
        not this one."""
        devices: dict[str, TidalConnectDevice] = {}
        requester = AiohttpRequester()
        factory = UpnpFactory(requester)

        async def _handle_response(headers: dict) -> None:
            location = headers.get("LOCATION") or headers.get("location")
            if not location:
                return
            try:
                device = await factory.async_create_device(location)
            except Exception as exc:
                log.debug(
                    "tidal_connect: parse %s failed: %s", location, exc
                )
                return
            service_types = tuple(
                sorted({s.service_type for s in device.all_services})
            )
            is_openhome = any(
                st.startswith(_OPENHOME_NAMESPACE_PREFIX)
                for st in service_types
            )
            if not is_openhome:
                # Plain UPnP renderer — not a Tidal Connect target.
                return
            has_credentials = any(
                st.startswith(_OPENHOME_CREDENTIALS_SERVICE)
                for st in service_types
            )
            entry = TidalConnectDevice(
                id=device.udn or location,
                friendly_name=(
                    device.friendly_name
                    or device.model_name
                    or "OpenHome renderer"
                ),
                manufacturer=device.manufacturer or "",
                model=device.model_name or "",
                location=location,
                is_openhome=True,
                has_credentials_service=has_credentials,
                service_types=service_types,
            )
            devices[entry.id] = entry

        try:
            await async_search(
                async_callback=_handle_response,
                search_target=_ST_MEDIA_RENDERER,
                timeout=timeout,
            )
        except Exception as exc:
            log.warning("tidal_connect ssdp search raised: %s", exc)

        for d in devices.values():
            print(
                f"[tidal-connect] discovered: {d.friendly_name} "
                f"({d.manufacturer or 'unknown'}) "
                f"openhome={d.is_openhome} credentials="
                f"{d.has_credentials_service}",
                flush=True,
            )
        return list(devices.values())


# Module-level singleton. Constructed lazily on first access — like
# UpnpManager — so importing this module at server start doesn't
# spin up an event loop for users who'll never use Tidal Connect.
_singleton: Optional[TidalConnectManager] = None
_singleton_lock = threading.Lock()


def get_manager() -> TidalConnectManager:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = TidalConnectManager()
        return _singleton
