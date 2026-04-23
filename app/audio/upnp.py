"""UPnP / DLNA MediaRenderer output.

Talks to the protocol that Bluesound, Cambridge Audio, Linn, NAD,
Sonos, LG TVs, and many other audiophile and smart-home streamers
implement. Most hardware that advertises "Tidal Connect" also speaks
UPnP, and plenty of hardware that does not speak Connect still does
UPnP — including a lot of cheaper streamers, NAS-side renderers,
and DLNA-capable TVs. So UPnP output strictly widens our reach
compared to Connect without any partner-auth reverse engineering.

Architecture mirrors app/audio/airplay.py:

- An UpnpManager singleton owns discovery, pairing-free connection
  to a chosen renderer, and the SetAVTransportURI / Play / Pause
  / Stop commands.
- An embedded HTTP server serves the live FLAC stream on a LAN-
  reachable port; the renderer pulls from that URL.
- A FLAC encoder running on a dedicated thread consumes PCM from
  the player's audio callback tap and writes encoded bytes into a
  ring buffer the HTTP server reads from.

This module is Day 1: discovery only. SetAVTransportURI and the
streaming pipeline are stubbed until we confirm at least one
device on the user's LAN responds to SSDP.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger(__name__)

# async-upnp-client is an optional dep. The rest of the app must
# boot fine without it — the UPnP output simply doesn't appear in
# Settings. Mirrors the pattern app/audio/airplay.py uses for pyatv.
try:
    import aiohttp  # async-upnp-client brings this in
    from async_upnp_client.aiohttp import AiohttpRequester
    from async_upnp_client.client_factory import UpnpFactory
    from async_upnp_client.search import async_search

    _UPNP_AVAILABLE = True
except Exception as _exc:  # pragma: no cover - environment dependent
    log.warning("async-upnp-client unavailable: %s", _exc)
    _UPNP_AVAILABLE = False


# MediaRenderer devices advertise this UPnP device type under SSDP.
# Discovery filters on it so we don't return every NAS and printer
# the network exposes — just things that can play audio.
_ST_MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"


@dataclass(frozen=True)
class UpnpDevice:
    """Concrete renderer the user can pick from Settings."""

    id: str
    name: str
    manufacturer: str
    model: str
    # Root-device description URL, e.g. http://192.168.1.42:8060/dial/dd.xml.
    # We keep it around so a later connect() call can rebuild an
    # UpnpDevice object from the live description without another
    # SSDP scan.
    location: str
    # Service-type prefix set the device advertises. Recorded so we
    # can identify OpenHome-only devices later (Linn / some Naim)
    # which need a different command set than standard AVTransport.
    service_types: tuple[str, ...] = ()


class UpnpManager:
    """Singleton-ish owner of the UPnP output path.

    Create once at process start. The constructor is cheap (no
    network). Discovery + connect are async and run on a dedicated
    thread's event loop so sync FastAPI handlers can call them via
    `asyncio.run_coroutine_threadsafe`, the same pattern
    AirPlayManager uses.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._discovered: dict[str, UpnpDevice] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        if _UPNP_AVAILABLE:
            self._start_loop_thread()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """True when the async-upnp-client dep is importable. When
        false the Settings UI hides the UPnP section entirely."""
        return _UPNP_AVAILABLE

    def discover(self, timeout: float = 5.0) -> List[UpnpDevice]:
        """SSDP-scan the local network for MediaRenderer devices.

        Blocks the caller for at most `timeout` seconds. Results are
        cached on the manager so a subsequent connect() doesn't have
        to rescan. Returns an empty list if the library isn't
        installed or if no devices responded — a failed scan isn't
        an error worth raising.
        """
        if not _UPNP_AVAILABLE:
            return []
        if self._loop is None:
            return []
        future = asyncio.run_coroutine_threadsafe(
            self._discover_async(timeout), self._loop
        )
        try:
            devices = future.result(timeout=timeout + 5.0)
        except Exception as exc:
            log.warning("upnp discover failed: %s", exc)
            return []
        with self._lock:
            self._discovered = {d.id: d for d in devices}
        return list(devices)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _start_loop_thread(self) -> None:
        """Spin up a dedicated asyncio loop on a daemon thread. All
        async UPnP work runs here so sync callers can submit
        coroutines from any FastAPI worker thread without juggling
        loops."""
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

        t = threading.Thread(target=_run, name="upnp-asyncio", daemon=True)
        t.start()
        ready.wait(timeout=2.0)
        self._loop_thread = t

    async def _discover_async(self, timeout: float) -> List[UpnpDevice]:
        """SSDP multicast + resolve description for each responder.

        async_search yields SSDP responses as they arrive. Each
        response has the device's LOCATION header — an HTTP URL to
        the root device description XML. We fetch and parse that
        to get manufacturer, friendlyName, and service-type list.

        Responses that don't parse cleanly are dropped silently. A
        misbehaving device shouldn't break discovery for the rest.
        """
        devices: dict[str, UpnpDevice] = {}
        requester = AiohttpRequester()
        factory = UpnpFactory(requester)

        async def _handle_response(headers: dict) -> None:
            location = headers.get("LOCATION") or headers.get("location")
            if not location:
                return
            try:
                device = await factory.async_create_device(location)
            except Exception as exc:
                log.debug("upnp: parse %s failed: %s", location, exc)
                return
            service_types = tuple(sorted({s.service_type for s in device.all_services}))
            entry = UpnpDevice(
                id=device.udn or location,
                name=device.friendly_name or device.model_name or "UPnP renderer",
                manufacturer=device.manufacturer or "",
                model=device.model_name or "",
                location=location,
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
            log.warning("upnp ssdp search raised: %s", exc)
        return list(devices.values())


# Module-level singleton so the rest of the app doesn't have to
# thread it through. Instantiated lazily on first access because
# importing this module at server start should not spin up an
# event loop if UPnP is never going to be used.
_singleton: Optional[UpnpManager] = None
_singleton_lock = threading.Lock()


def get_manager() -> UpnpManager:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = UpnpManager()
        return _singleton


