"""Tidal Connect — controller-side, untested against real hardware.

From Tideway, pick a Tidal Connect device on the LAN and have THAT
device fetch the Tidal stream directly using its own paired Tidal
session. Tideway is just a remote control; no audio encoding or HTTP
serving (unlike the Cast sender).

This is fundamentally different from `app/audio/cast.py`:

  Cast:           Tideway encodes PCM to FLAC, serves over HTTP, Cast
                  device pulls and plays. Tideway is the audio source.

  Tidal Connect:  Tideway issues OpenHome SOAP commands ('play this
                  Tidal track ID') to a paired device. The device
                  fetches the stream from Tidal's CDN with its own
                  credentials and plays it. Tideway is just the
                  controller; the audio engine is dormant.

## Validation status — important

The code below is fully implemented: SSDP discovery, OpenHome
description fetch, Playlist/Volume/Time/Info service controllers,
DIDL-Lite metadata building, track-URL handoff, state polling, and
the public `connect / disconnect / load_track / play / pause / seek
/ set_volume / set_mute` surface. ~50 unit tests exercise each path
against mocked OpenHome services and all pass.

What hasn't happened yet:

  - **End-to-end test against a real Tidal Connect device.** No
    Bluesound / Linn / KEF / Naim hardware has been on the LAN with
    this code in front of it. The original scope doc
    (`docs/cast-and-connect-scope.md`) called for a Bluesound Node
    at the start of Phase 2 to validate `Hypothesis A` (signed
    stream URL works) versus `Hypothesis B` (device rejects our URL
    without partner credentials). That validation was skipped.
  - **Packet capture against the official Tidal desktop app.** The
    SOAP command set we issue is inferred from OpenHome's public
    spec, not captured from what the Tidal app actually sends. A
    real device may accept our commands; it may reject them as
    malformed or unauthorised.

Recent investigation into Tidal Connect's receiver-side auth (see
`docs/tidal-connect-receiver-scope.md`) turned up evidence that
every shipping non-Tidal **receiver** uses a per-vendor signed
certificate to identify to Tidal's backend. We don't know whether
the **controller** side has an analogous gate — i.e. whether a
real device will accept stream URLs from a non-Tidal client. That
question is answerable in 30 minutes with hardware we don't have.

Practical implication: the experimental toggle in the UI is
genuinely experimental. The code might just work, or it might fail
at the first SOAP exchange against a real device. Until somebody
runs it, we don't know.

If you have a Tidal Connect device on hand and run this: please
file an issue with the device model, the failure mode (or a
"works fine" report), and the relevant log lines from
`logger=app.audio.tidal_connect`. That's how we close the gap.

Module degrades gracefully when async-upnp-client is missing — the
manager still constructs but discovery is a no-op.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.audio.openhome import (
    InfoController,
    OpenHomeDevice,
    PlaylistController,
    TimeController,
    TrackMetadata,
    VolumeController,
    build_didl_lite,
    fetch_device,
)

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


@dataclass
class _SessionState:
    """Internal state carried by an active Tidal Connect session.

    Holds the discovered device, the parsed OpenHome device tree
    (with all SCPD action lists), and the four service controllers
    we need for transport + metadata. The current track's NewId is
    stored so SeekSecond / SeekId calls target the right queue
    entry — slice 4's track-handoff inserts at after_id=0 (head)
    each time and stores the returned NewId here.
    """

    device: TidalConnectDevice
    openhome_device: OpenHomeDevice
    playlist: PlaylistController
    volume: Optional[VolumeController]
    time: Optional[TimeController]
    info: Optional[InfoController]
    current_track_id: int = 0  # Last Insert's NewId; 0 if no track loaded
    # Stream-URL minter. Slice 4 takes a Tidal track id and produces
    # the URL we hand the device. Set by the manager based on whether
    # the user-provided session has a Tidal session attached. Kept as
    # a callable rather than a hard import so tests can substitute.
    track_url_resolver: Optional[
        "Callable[[int], tuple[str, TrackMetadata]]"
    ] = None
    # Last polled state from the device. Slice 5's state-poll loop
    # populates this and compares against the previous reading to
    # decide which listener events to fire. Position is the second
    # offset into the current track; track_count increments each
    # time the device moves to a new track (which is how we detect
    # 'song ended' without an explicit event).
    position_s: int = 0
    duration_s: int = 0
    track_count: int = 0
    volume_percent: int = 0
    muted: bool = False


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
        # Active session protector + state. Built by `connect()`,
        # cleared by `disconnect()`. The `_SessionState` carries the
        # OpenHome controllers we need for play / pause / track-
        # handoff so callers don't keep refetching service references
        # for every action.
        self._session_lock = threading.Lock()
        self._session: Optional[_SessionState] = None
        # State-change listeners. SSE bus subscribes so the frontend
        # gets push updates on track / volume changes the device-
        # side state poll detects. Held under _session_lock; the
        # poll loop snapshots the list before firing so a listener
        # raising can't break the bus for everyone else.
        self._state_listeners: list[
            "Callable[[dict[str, object]], None]"
        ] = []
        # Local-output silencer hook. Same pattern as cast.py —
        # server.py wires this to PCMPlayer's set_external_output_active
        # at startup. Called with True on connect, False on
        # disconnect, so the local sounddevice output mutes while
        # audio is going to the Tidal Connect device.
        self._local_silencer: Optional[Callable[[bool], None]] = None
        # Track URL resolver. server.py wires this at startup with
        # a closure that calls into tidalapi to mint a signed
        # stream URL + extract metadata for any Tidal track id.
        # Tests substitute a synthetic resolver via the same setter.
        self._track_url_resolver: Optional[
            Callable[[int], tuple[str, TrackMetadata]]
        ] = None
        if _AVAILABLE:
            self._start_loop_thread()

    # ---- lifecycle ---------------------------------------------------

    def is_available(self) -> bool:
        """False when async-upnp-client failed to import. The picker
        hides the Tidal Connect section in that case."""
        return _AVAILABLE

    def status(self) -> dict[str, object]:
        """Diagnostic snapshot. Surfaces discovery state and
        whether a session is open. Used by /api/tidal-connect/devices
        and the picker UI."""
        with self._lock:
            disc = {
                "available": _AVAILABLE,
                "device_count": len(self._devices),
                "last_scan_age_s": (
                    None if self._last_scan_at == 0.0
                    else round(time.monotonic() - self._last_scan_at, 1)
                ),
            }
        with self._session_lock:
            sess = self._session
            disc["connected_id"] = sess.device.id if sess else None
            disc["connected_name"] = (
                sess.device.friendly_name if sess else None
            )
            disc["current_track_id"] = sess.current_track_id if sess else 0
            # control_plane_ready flips True once slice 4 lands
            # because connect / load_track / pause / play actually
            # do something. Tests should expect True on a successful
            # connect() and False on a fresh manager.
            disc["control_plane_ready"] = sess is not None
        return disc

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

    # ---- session lifecycle ------------------------------------------

    def connect(
        self,
        device_id: str,
        track_url_resolver: Optional[
            Callable[[int], tuple[str, TrackMetadata]]
        ] = None,
    ) -> TidalConnectDevice:
        """Open a control session against the given device.

        Fetches the device's full OpenHome description (root XML +
        every service's SCPD), constructs the four service
        controllers (Playlist, Volume, Time, Info), and clears the
        device's queue with `Playlist.DeleteAll` so we start from
        a known empty state.

        `track_url_resolver` is a callable that, given a Tidal
        track id, returns `(stream_url, TrackMetadata)`. The audio
        engine wires this to `tidalapi.session.track(...).get_stream()`
        when calling `connect()` from the player path. Tests pass
        in a synthetic resolver to avoid hitting Tidal.

        Tears down any existing session before opening the new one.
        Raises ValueError on unknown device id, RuntimeError on
        descriptor fetch / DeleteAll failure.
        """
        if not _AVAILABLE:
            raise RuntimeError("async-upnp-client not available")
        device = self.get_device(device_id)
        if device is None:
            raise ValueError(f"unknown tidal connect device: {device_id}")

        # Drop any existing session first.
        self.disconnect()

        try:
            openhome_device = fetch_device(device.location)
        except Exception as exc:
            raise RuntimeError(
                f"failed to fetch OpenHome description from "
                f"{device.location}: {exc}"
            ) from exc

        playlist = PlaylistController.from_device(openhome_device)
        if playlist is None:
            raise RuntimeError(
                f"{device.friendly_name} doesn't expose an OpenHome "
                "Playlist service — it advertises OpenHome but is "
                "missing the action set we need to control playback."
            )

        # Optional services. Their absence isn't fatal; we degrade
        # gracefully (no volume control, no position polling) rather
        # than fail the whole connect.
        volume = VolumeController.from_device(openhome_device)
        time_ctl = TimeController.from_device(openhome_device)
        info = InfoController.from_device(openhome_device)

        # Clear the queue. Some devices preserve queues across
        # controller switches and we don't want our subsequent
        # Insert to land in slot 47 of someone else's leftover
        # playlist. Failure here is recoverable — log and continue,
        # the worst case is the user sees stale tracks until they
        # play something new.
        try:
            playlist.delete_all()
        except Exception as exc:
            log.debug(
                "tidal_connect: DeleteAll on %s failed: %r — "
                "continuing with possibly stale queue",
                device.friendly_name,
                exc,
            )

        # Resolver precedence: explicit per-call argument wins,
        # else fall back to the manager-level resolver wired by
        # server.py at startup. That lets tests pass a synthetic
        # resolver per-call without mucking with the global
        # while production wires once.
        effective_resolver = (
            track_url_resolver
            if track_url_resolver is not None
            else self._track_url_resolver
        )

        session = _SessionState(
            device=device,
            openhome_device=openhome_device,
            playlist=playlist,
            volume=volume,
            time=time_ctl,
            info=info,
            track_url_resolver=effective_resolver,
        )
        with self._session_lock:
            self._session = session

        # Mute local audio output. PCMPlayer's audio callback
        # writes silence while active; Tidal Connect doesn't
        # decode locally at all, but the silencer defends against
        # any latent decoder-thread output from a prior local
        # playback that's draining.
        if self._local_silencer is not None:
            try:
                self._local_silencer(True)
            except Exception as exc:
                log.debug(
                    "tidal_connect: local silencer raised: %r", exc
                )

        print(
            f"[tidal-connect] connected: {device.friendly_name} "
            f"(playlist+{'volume' if volume else 'no-volume'}+"
            f"{'time' if time_ctl else 'no-time'})",
            flush=True,
        )
        # Start the state-poll thread that fires listener events on
        # track / position / volume changes. Lives until disconnect
        # clears the session.
        self._start_state_poll_thread()
        return device

    def disconnect(self) -> None:
        """Tear down the active session. Sends `Playlist.Stop` on
        the way out so the device doesn't keep playing whatever
        track was loaded. Idempotent — fine to call with no
        session active."""
        with self._session_lock:
            session = self._session
            self._session = None
        if session is None:
            return
        try:
            session.playlist.stop()
        except Exception as exc:
            log.debug(
                "tidal_connect: Stop on %s failed during disconnect: %r",
                session.device.friendly_name,
                exc,
            )

        # Restore local audio output. The user's volume / mute
        # settings were preserved across the cycle so they don't
        # have to re-set them now.
        if self._local_silencer is not None:
            try:
                self._local_silencer(False)
            except Exception as exc:
                log.debug(
                    "tidal_connect: local silencer raised on close: %r",
                    exc,
                )

        print(
            f"[tidal-connect] disconnected: {session.device.friendly_name}",
            flush=True,
        )

    # ---- track handoff ---------------------------------------------

    def load_track(self, tidal_track_id: int) -> int:
        """Hand a Tidal track to the active device. Returns the
        device's NewId so callers can later target SeekSecond /
        SeekId at the same queue entry.

        Resolves the streamable URL + metadata via the session's
        configured `track_url_resolver`, builds DIDL-Lite, issues
        `Playlist.Insert` followed by `Playlist.Play`. Replaces
        whatever was previously playing — slice 4's model is one
        track at a time, mirroring how Tidal's own controllers act
        when the user picks a single track to play.

        Raises RuntimeError if no session is open, if the resolver
        isn't configured, or if any of the SOAP calls fail.
        """
        with self._session_lock:
            session = self._session
        if session is None:
            raise RuntimeError(
                "no active Tidal Connect session — call connect() first"
            )
        if session.track_url_resolver is None:
            raise RuntimeError(
                "session has no track URL resolver configured — the "
                "audio engine should have wired it on connect()"
            )

        try:
            stream_url, metadata = session.track_url_resolver(tidal_track_id)
        except Exception as exc:
            raise RuntimeError(
                f"failed to resolve Tidal track {tidal_track_id}: {exc}"
            ) from exc
        didl = build_didl_lite(metadata)
        try:
            new_id = session.playlist.insert(
                after_id=0, uri=stream_url, metadata=didl
            )
            session.playlist.play()
        except Exception as exc:
            raise RuntimeError(
                f"failed to load track on {session.device.friendly_name}: {exc}"
            ) from exc

        with self._session_lock:
            if self._session is session:
                # Update only if our session is still the active one.
                # A concurrent disconnect could have cleared it
                # between the play() call and now; nothing wrong with
                # that, just don't write into a torn-down session.
                session.current_track_id = new_id
        print(
            f"[tidal-connect] loaded track {tidal_track_id} on "
            f"{session.device.friendly_name} (NewId={new_id})",
            flush=True,
        )
        return new_id

    def pause(self) -> None:
        """Pause the active session's playback. Raises if no session
        is open or the SOAP call fails."""
        session = self._require_session()
        session.playlist.pause()

    def play(self) -> None:
        """Resume the active session's playback."""
        session = self._require_session()
        session.playlist.play()

    def seek(self, position_s: int) -> None:
        """Seek the currently-loaded track to the given second
        offset."""
        session = self._require_session()
        session.playlist.seek_second(position_s)

    def set_volume(self, level_percent: int) -> None:
        """Set device volume from a 0-100 percentage. No-op if the
        device doesn't expose a Volume service."""
        session = self._require_session()
        if session.volume is not None:
            session.volume.set_volume(level_percent)

    def set_mute(self, muted: bool) -> None:
        session = self._require_session()
        if session.volume is not None:
            session.volume.set_mute(muted)

    def _require_session(self) -> _SessionState:
        with self._session_lock:
            session = self._session
        if session is None:
            raise RuntimeError("no active Tidal Connect session")
        return session

    # ---- state polling + listener bus -------------------------------

    def set_local_silencer(
        self, callback: Optional[Callable[[bool], None]]
    ) -> None:
        """Wire the audio engine's local-output silencer. The
        callback gets `True` when a Tidal Connect session opens
        and `False` when it closes, so the audio engine can mute
        the local sounddevice output. Same pattern Cast uses;
        server.py wires both at startup."""
        self._local_silencer = callback

    def set_track_url_resolver(
        self,
        resolver: Optional[Callable[[int], tuple[str, TrackMetadata]]],
    ) -> None:
        """Wire a tidal-track-id → (stream_url, TrackMetadata)
        resolver. The audio engine wires this with a closure that
        calls tidalapi.session.track(...).get_stream() to mint a
        signed CDN URL. Tests substitute a synthetic resolver to
        avoid hitting Tidal."""
        self._track_url_resolver = resolver

    def add_state_listener(
        self,
        callback: "Callable[[dict[str, object]], None]",
    ) -> "Callable[[], None]":
        """Subscribe to state-change events. Called whenever the
        polling loop detects a meaningful change (track advanced,
        position updated, volume / mute changed). Returns an
        unsubscribe callable. Mirrors the listener pattern in
        `cast_manager` so the SSE bus in server.py can hand
        either manager's events to the same frontend stream.

        The callback receives a dict of the current state — same
        shape as `status()`'s session block — so listeners don't
        have to remember which fields exist.
        """
        with self._session_lock:
            self._state_listeners.append(callback)

        def _unsub() -> None:
            with self._session_lock:
                if callback in self._state_listeners:
                    self._state_listeners.remove(callback)
        return _unsub

    def poll_state_once(self) -> Optional[dict[str, object]]:
        """Single state poll. Reads Time + Volume + Mute from the
        active session, stores the values back into the session
        record, fires listeners on any change, and returns the
        new state dict.

        Designed to be called from a single owning thread (the
        polling loop) — multiple concurrent calls would race on
        which listener event each fires for. Returns None when no
        session is active.

        SOAP failures during polling are not fatal — log them and
        return the last-known state. A device that briefly stops
        responding shouldn't tear down the session; the user
        explicitly disconnects to stop.
        """
        with self._session_lock:
            session = self._session
        if session is None:
            return None

        new_state = {
            "position_s": session.position_s,
            "duration_s": session.duration_s,
            "track_count": session.track_count,
            "volume_percent": session.volume_percent,
            "muted": session.muted,
        }

        if session.time is not None:
            try:
                t = session.time.time()
                new_state["position_s"] = t["seconds"]
                new_state["duration_s"] = t["duration"]
                new_state["track_count"] = t["track_count"]
            except Exception as exc:
                log.debug("tidal_connect: time poll failed: %r", exc)

        if session.volume is not None:
            try:
                new_state["volume_percent"] = session.volume.get_volume()
                new_state["muted"] = session.volume.get_mute()
            except Exception as exc:
                log.debug("tidal_connect: volume poll failed: %r", exc)

        # Detect changes worth firing on. Position-only changes
        # are too chatty for SSE-grade listeners (they'd get an
        # event every poll). Fire only when something else moved
        # OR when position has drifted by ≥ 1s (typical poll
        # cadence) since the last fire.
        with self._session_lock:
            if self._session is not session:
                # Disconnected mid-poll. Don't write into a torn-
                # down session.
                return new_state
            previous_position = session.position_s
            track_changed = new_state["track_count"] != session.track_count
            volume_changed = (
                new_state["volume_percent"] != session.volume_percent
                or new_state["muted"] != session.muted
            )
            position_changed = (
                abs(new_state["position_s"] - previous_position) >= 1
            )
            session.position_s = new_state["position_s"]
            session.duration_s = new_state["duration_s"]
            session.track_count = new_state["track_count"]
            session.volume_percent = new_state["volume_percent"]
            session.muted = new_state["muted"]
            should_notify = (
                track_changed or volume_changed or position_changed
            )
            listeners = list(self._state_listeners) if should_notify else []

        for cb in listeners:
            try:
                cb(new_state)
            except Exception as exc:
                log.debug("tidal_connect: listener raised: %r", exc)
        return new_state

    def _start_state_poll_thread(self) -> None:
        """Spawn the per-session poll thread. Lives until disconnect()
        clears the session — sees `self._session is None` on the
        next iteration and exits. One-second cadence is enough for
        UI-grade state updates without flooding the device with
        SOAP requests."""
        def _run() -> None:
            while True:
                with self._session_lock:
                    if self._session is None:
                        return
                try:
                    self.poll_state_once()
                except Exception as exc:
                    log.debug(
                        "tidal_connect: poll loop raised: %r", exc
                    )
                # Use a short sleep with checks rather than a
                # single 1s sleep so disconnect() returns
                # promptly.
                for _ in range(10):
                    with self._session_lock:
                        if self._session is None:
                            return
                    time.sleep(0.1)

        t = threading.Thread(
            target=_run,
            name="tidal-connect-poll",
            daemon=True,
        )
        t.start()

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
