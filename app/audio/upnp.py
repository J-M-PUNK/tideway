"""UPnP / DLNA MediaRenderer output.

DLNA's `MediaRenderer` profile is the universal target for "play
this audio on a network device" across every consumer streamer
that doesn't speak Tidal Connect natively: WiiM, most Bluesound,
Cambridge, Yamaha, Denon AVRs, NAD streamers, LG / Samsung TVs,
and a long tail of cheaper Hi-Fi network bridges. This module
ships a sender for that protocol so those devices appear in the
Sound Output picker alongside Cast.

## Architecture

Mirrors `app/audio/cast.py` very deliberately. The streaming half
is identical: Tideway encodes PCM to FLAC into a ring buffer, an
embedded HTTP server serves the buffer at a LAN-reachable URL, the
device pulls from that URL. The control half differs: Cast issues
`MediaController.play_media`, DLNA issues UPnP/SOAP
`AVTransport.SetAVTransportURI` + `Play`. Both put the device in a
"pull our stream" state and we just keep encoding.

  PCMPlayer         ─push_pcm()──▶  FlacStreamEncoder ─bytes─▶ RingBuffer
  audio callback                                                   │
                                                                   ▼
  Renderer ◀──HTTP GET stream────  StreamHTTPServer  ◀───reads── RingBuffer
       ▲
       │ SetAVTransportURI(stream_url) + Play
       │
  AVTransportController (SOAP over HTTP)

## Why a separate manager from `tidal_connect.py`

That module targets OpenHome-flavoured devices (Linn, some Naim,
some Bluesound) and assumes the device fetches audio directly from
Tidal with its own paired session. The device is the audio source.
DLNA is the opposite: Tideway is the audio source, the device is
just an output. The discovery filters, control plane, audio
plumbing, and silencer behaviour all differ. Trying to merge the
two managers produced a flag-soup that obscured both paths;
keeping them separate keeps each one's invariants legible.

## Why this won't accidentally surface OpenHome-only devices

`_filter_dlna_renderer` rejects devices that don't expose
AVTransport. A Linn DSM (OpenHome-only) doesn't show up here; it
only shows up in `tidal_connect.py`'s discovery. A WiiM (DLNA-only)
shows up here and not there. Devices that expose both (some
Bluesound) appear in both lists. The picker lets the user choose
how they'd rather drive it.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set, Tuple

import numpy as np

from app.audio.avtransport import (
    AVTransportController,
    RenderingControlController,
)
from app.audio.http_stream import (
    FlacPassthroughEncoder,
    FlacStreamEncoder,
    RingBuffer,
    StreamHTTPServer,
    primary_lan_ip,
    start_stream_http_server,
)
from app.audio.openhome import (
    OpenHomeDevice,
    TrackMetadata,
    build_didl_lite,
    fetch_device,
)

log = logging.getLogger(__name__)

# async-upnp-client is the SSDP discovery library. Optional dep:
# rest of the app boots if it's missing, just no DLNA in the picker.
try:
    from async_upnp_client.aiohttp import AiohttpRequester
    from async_upnp_client.client_factory import UpnpFactory

    _UPNP_AVAILABLE = True
except Exception as _exc:  # pragma: no cover - environment dependent
    log.warning("async-upnp-client unavailable: %s", _exc)
    AiohttpRequester = None  # type: ignore
    UpnpFactory = None  # type: ignore
    _UPNP_AVAILABLE = False


# SSDP discovery is done with our own socket rather than
# async-upnp-client's async_search(). The reason is a real-world
# interop bug: async_search sends its M-SEARCH from a socket that, on
# Linux, is never bound to the SSDP port (it binds only on win32), so
# the OS gives it an ephemeral source port. Spec-compliant renderers
# reply via unicast to that source port and are heard. But some
# renderers — notably USB Audio Player PRO and other Android-based
# devices — always reply to port 1900 of the requester regardless of
# the M-SEARCH source port. Nothing is listening there, so their reply
# is dropped and they never appear in the picker. gssdp-discover (the C
# reference that works against these devices) binds a single socket to
# 1900 for both send and receive; we mirror that. See GitHub #234 and
# #220. The v1.18.1 attempt (a second async_search for the AVTransport
# service type) didn't help because the lost-reply problem is in the
# socket, not the search target.
_SSDP_MCAST_ADDR = "239.255.255.250"
_SSDP_PORT = 1900

# Search targets we burst in one scan. Devices vary in which they
# answer: most answer the MediaRenderer device type, some Android
# renderers only answer a service-type or the catch-all queries.
# Sending all four in one round catches the union; responses are
# deduplicated by LOCATION before any descriptor fetch.
_SSDP_SEARCH_TARGETS: Tuple[str, ...] = (
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:schemas-upnp-org:service:AVTransport:1",
    "upnp:rootdevice",
    "ssdp:all",
)

# We only want devices that expose AVTransport. Any service-type URN
# starting with this prefix qualifies (covers :1, :2, :3 etc.).
_AVTRANSPORT_PREFIX = "urn:schemas-upnp-org:service:AVTransport:"


def _build_msearch(search_target: str, mx: int) -> bytes:
    """One SSDP M-SEARCH datagram for the given target. MX is the max
    seconds a device may wait before replying; it must be smaller than
    our receive window so late repliers still land inside it."""
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {_SSDP_MCAST_ADDR}:{_SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {mx}\r\n"
        f"ST: {search_target}\r\n"
        "\r\n"
    ).encode("ascii")


def _parse_ssdp_location(data: bytes) -> Optional[str]:
    """Pull the LOCATION header out of an SSDP response or NOTIFY.
    Returns None for datagrams without one (e.g. byebye NOTIFYs)."""
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        return None
    for line in text.split("\r\n"):
        if line.lower().startswith("location:"):
            return line.split(":", 1)[1].strip() or None
    return None


def _collect_ssdp_locations(
    timeout: float, lan_ip: str
) -> Tuple[Set[str], bool]:
    """Blocking single-socket SSDP search. Binds one UDP socket to
    port 1900, joins the SSDP multicast group, bursts an M-SEARCH for
    every target, and collects LOCATION URLs from every reply (unicast
    or multicast) until the timeout elapses.

    Returns the set of discovered descriptor URLs and whether we
    actually got port 1900. If 1900 is already held (another SSDP
    listener on the box), we fall back to an ephemeral port: we lose
    the port-1900-only repliers like UAPP but still find every
    spec-compliant device via the unicast-to-source-port path, which is
    strictly better than failing the whole scan.
    """
    deadline = time.monotonic() + timeout
    locations: Set[str] = set()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        # SO_REUSEPORT is absent on some platforms; without it a second
        # listener on 1900 just means we take the fallback path.
        pass

    bound_1900 = True
    try:
        sock.bind(("", _SSDP_PORT))
    except OSError as exc:
        log.debug("upnp: port %d busy (%s); falling back to ephemeral",
                  _SSDP_PORT, exc)
        bound_1900 = False
        try:
            sock.bind(("", 0))
        except OSError as exc2:
            log.warning("upnp: could not bind any SSDP socket: %s", exc2)
            sock.close()
            return locations, False

    # Join the multicast group and pin the outgoing interface to the LAN
    # IP so the M-SEARCH leaves the right NIC on multi-homed machines.
    iface = lan_ip if lan_ip and lan_ip != "127.0.0.1" else "0.0.0.0"
    try:
        mreq = socket.inet_aton(_SSDP_MCAST_ADDR) + socket.inet_aton(iface)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        if iface != "0.0.0.0":
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(iface),
            )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    except OSError as exc:
        log.debug("upnp: multicast setup partial: %s", exc)

    # MX must be < the receive window. Cap at 5 (the SSDP-recommended
    # ceiling) and keep at least 1.
    mx = max(1, min(5, int(timeout) - 1))

    def _burst() -> None:
        for st in _SSDP_SEARCH_TARGETS:
            try:
                sock.sendto(
                    _build_msearch(st, mx),
                    (_SSDP_MCAST_ADDR, _SSDP_PORT),
                )
            except OSError as exc:
                log.debug("upnp: M-SEARCH send for %s failed: %s", st, exc)

    print(
        f"[upnp] ssdp scan: bound_1900={bound_1900} iface={iface} "
        f"mx={mx} timeout={timeout:.0f}s",
        flush=True,
    )
    _burst()
    # A second burst partway through helps Android renderers that take a
    # few seconds to acquire the multicast lock after the first probe.
    second_burst_at = time.monotonic() + max(1.0, timeout / 2.0)
    did_second = False

    sock.settimeout(0.5)
    try:
        while time.monotonic() < deadline:
            if not did_second and time.monotonic() >= second_burst_at:
                _burst()
                did_second = True
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            loc = _parse_ssdp_location(data)
            if loc:
                locations.add(loc)
    finally:
        sock.close()
    return locations, bound_1900

# Path the embedded HTTP server exposes for the live FLAC stream.
# Devices use this as part of the URL we hand them in
# SetAVTransportURI; the path itself is arbitrary as long as it's
# stable across the session.
_STREAM_PATH = "/dlna/stream"


@dataclass(frozen=True)
class UpnpDevice:
    """A discovered DLNA renderer the user can pick from Settings.

    `id` is the device UDN, stable across reboots. `service_types`
    is sorted for deterministic display + so the equality check on
    `discover()` cache hit doesn't churn. `has_avtransport` is the
    discovery-time gate: devices without AVTransport never make it
    into the manager's device map.
    """

    id: str
    name: str
    manufacturer: str
    model: str
    location: str  # device description URL, needed to rebuild
                   # services on connect without re-running SSDP
    service_types: tuple[str, ...] = ()
    has_avtransport: bool = False


@dataclass
class _SessionState:
    """Internal state for an active DLNA session.

    Same fields as cast.py's _SessionState (encoder, ring buffer,
    HTTP server, byte counter) plus the AVTransport / RenderingControl
    controllers and the parsed OpenHomeDevice. That's what
    AVTransportController.from_device wraps. Held as a single
    dataclass so `disconnect()` doesn't have to coordinate teardown
    across multiple maps.
    """

    device: UpnpDevice
    openhome_device: OpenHomeDevice
    av: AVTransportController
    rc: Optional[RenderingControlController]
    buffer: RingBuffer = field(default_factory=RingBuffer)
    http_server: Optional[StreamHTTPServer] = None
    encoder: Optional[FlacStreamEncoder] = None
    encoder_lock: threading.Lock = field(default_factory=threading.Lock)
    encoder_rate: int = 0
    encoder_channels: int = 0
    encoder_dtype: str = ""
    bytes_encoded: int = 0
    media_loaded: bool = False
    stream_url: str = ""
    encode_failed: bool = False
    # Passthrough fields — populated by start_passthrough()
    passthrough_encoder: Optional[FlacPassthroughEncoder] = None
    passthrough_active: bool = False
    _passthrough_source_urls: Optional[tuple[str, ...]] = None


def _filter_dlna_renderer(service_types: tuple[str, ...]) -> bool:
    """True iff the service-type list contains AVTransport. SSDP
    responses include every OpenHome / vendor service in addition to
    the standard AV ones; we don't care about those, only that the
    renderer can accept SetAVTransportURI."""
    return any(st.startswith(_AVTRANSPORT_PREFIX) for st in service_types)


class UpnpManager:
    """Process-wide owner of the DLNA renderer output.

    Construct once at server boot. Discovery is on-demand
    (`refresh()`); SSDP multicast is intentionally not held open
    continuously because it produces more network noise per scan
    than mDNS. The picker triggers `refresh()` when the dropdown
    opens. `connect()` opens an audio session against a discovered
    device, `disconnect()` tears it down, `push_pcm()` feeds the
    encoder from the player's audio callback.

    At most one session at a time. `connect()` to a different device
    tears the existing session down first. Same single-session
    invariant Cast and Tidal Connect use.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: dict[str, UpnpDevice] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._last_scan_at: float = 0.0

        # Active session and its lock. Held under a separate lock
        # from the discovery dict so a slow connect() doesn't block
        # list_devices().
        self._session_lock = threading.Lock()
        self._session: Optional[_SessionState] = None

        # External listeners (SSE bus). Notified on connect /
        # disconnect transitions only, not per-byte. Same shape
        # cast manager uses so server.py can hand the events to the
        # same SSE channel without translation.
        self._listeners: list[Callable[[Optional[UpnpDevice]], None]] = []

        # Local-output silencer: server.py wires this to PCMPlayer's
        # set_external_output_active so the local sounddevice mutes
        # when DLNA is active. Optional. Without it the user hears
        # both local and remote audio, which is inconvenient but not
        # catastrophic.
        self._local_silencer: Optional[Callable[[bool], None]] = None

        # Source provider: returns the current track's segment URLs
        # (list[str]) or None. The player registers this so connect()
        # can start passthrough even when a track is already loaded
        # before the DLNA session.
        self._source_provider: Optional[Callable[[], Optional[list[str]]]] = None

        # Metadata provider: returns the current track's metadata dict
        # (title, artist, album, duration_s, cover_url) or None. Used
        # to notify the renderer on track changes via SetAVTransportURI.
        self._metadata_provider: Optional[Callable[[], Optional[dict]]] = None

        if _UPNP_AVAILABLE:
            self._start_loop_thread()

    # ---- availability + state surface ------------------------------

    def is_available(self) -> bool:
        """False when async-upnp-client failed to import. The picker
        hides the DLNA section in that case."""
        return _UPNP_AVAILABLE

    def status(self) -> dict[str, object]:
        """Diagnostic snapshot. Used by /api/dlna/devices for the
        picker UI and as a quick health probe in support traces."""
        with self._lock:
            disc: dict[str, object] = {
                "available": _UPNP_AVAILABLE,
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
                sess.device.name if sess else None
            )
            disc["bytes_encoded"] = sess.bytes_encoded if sess else 0
            disc["media_loaded"] = sess.media_loaded if sess else False
            disc["stream_url"] = sess.stream_url if sess else ""
        return disc

    def list_devices(self) -> List[UpnpDevice]:
        """Snapshot of currently-known DLNA renderers. Sorted
        alphabetically; no equivalent of Cast's audio-only-first
        heuristic since AVTransport is audio-or-video without
        distinction."""
        with self._lock:
            devices = list(self._devices.values())
        devices.sort(key=lambda d: d.name.lower())
        return devices

    def get_device(self, device_id: str) -> Optional[UpnpDevice]:
        with self._lock:
            return self._devices.get(device_id)

    def is_active(self) -> bool:
        """Cheap, lock-free probe for the audio callback. Same
        guarantee as `CastManager.is_active`: a single attribute
        read; false positives are caught when the encoder lock is
        actually taken."""
        return self._session is not None

    # ---- listener bus ----------------------------------------------

    def set_local_silencer(
        self, callback: Optional[Callable[[bool], None]]
    ) -> None:
        """Wire the audio engine's local-output silencer. Same hook
        Cast uses; server.py wires both at startup."""
        self._local_silencer = callback

    def set_source_provider(
        self, callback: Optional[Callable[[], Optional[list[str]]]]
    ) -> None:
        """Register a callback that returns the current track's segment
        URLs (list[str]) or None. The player sets this so connect()
        can start passthrough for tracks loaded before the DLNA session
        was established."""
        self._source_provider = callback

    def set_metadata_provider(
        self, callback: Optional[Callable[[], Optional[dict]]]
    ) -> None:
        """Register a callback that returns the current track's metadata
        dict (title, artist, album, duration_s, cover_url) or None.
        Called on track changes to notify the renderer via
        SetAVTransportURI + Play."""
        self._metadata_provider = callback

    def add_listener(
        self, callback: Callable[[Optional[UpnpDevice]], None]
    ) -> Callable[[], None]:
        """Subscribe to session-change events. Called with the new
        device on connect, None on disconnect. Returns an
        unsubscribe callable.
        """
        with self._session_lock:
            self._listeners.append(callback)

        def _unsub() -> None:
            with self._session_lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)
        return _unsub

    def _notify_listeners(self, device: Optional[UpnpDevice]) -> None:
        with self._session_lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(device)
            except Exception as exc:
                log.debug("upnp: listener raised: %r", exc)

    # ---- discovery -------------------------------------------------

    def refresh(self, timeout: float = 5.0) -> List[UpnpDevice]:
        """Run an SSDP scan and replace the device cache. Blocks the
        caller for at most `timeout` seconds. Returns the new device
        list. No-op when the dep isn't available; the picker will
        just see an empty list, which is the right UX for "no UPnP."
        """
        if not _UPNP_AVAILABLE or self._loop is None:
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
            self._devices = {d.id: d for d in devices}
            self._last_scan_at = time.monotonic()
        return devices

    # ---- passthrough -----------------------------------------------

    def start_passthrough(
        self, source, prefetched=None, metadata=None
    ) -> None:
        """Start bit-perfect FLAC passthrough for the current track.

        Opens the same fMP4 source that the Decoder uses, but instead
        of decoding to PCM, demuxes raw FLAC packets and remuxes them
        into a continuous FLAC stream written directly to the RingBuffer.

        Sends SetAVTransportURI + Play to the renderer so it updates
        its now-playing display and initiates a fresh HTTP GET for the
        new track's FLAC stream (needed for gapless passthrough).

        This preserves the original STREAMINFO and seektable from the
        Tidal encoder, which strict DLNA renderers like UAPP require.

        Args:
            source: URL list (SegmentReader-compatible) or file-like
            prefetched: dict of {segment_idx: bytes} for pre-fetched segments
            metadata: dict with title, artist, album, duration_s, cover_url
                      for the new track; falls back to _metadata_provider
                      if not given and the provider is registered.
        """
        with self._session_lock:
            session = self._session
        if session is None:
            print("[upnp] passthrough: no active session", flush=True)
            return

        # ---- GUARD: skip if already running for the same source ----
        # player.py calls start_passthrough() in 2 places (lines 819
        # and 1299 in the original; now also in _build_load_pipeline).
        # On gapless transitions both fire for the same track. Without
        # this guard, the 2nd call closes the encoder, clears the
        # buffer, and re-notifies UAPP — causing duplicated audio.
        # Key on the URL tuple rather than id() so the guard survives
        # object reuse across garbage-collection cycles.
        _source_urls = tuple(source) if isinstance(source, (list, tuple)) else None
        if session.passthrough_active and session._passthrough_source_urls == _source_urls:
            print(
                "[upnp] passthrough: already running for this source, skipping",
                flush=True,
            )
            return
        session._passthrough_source_urls = _source_urls

        # Stop existing passthrough if any
        if session.passthrough_encoder is not None:
            try:
                session.passthrough_encoder.close()
            except Exception:
                pass
            session.passthrough_encoder = None

        # Build SegmentReader from URLs
        try:
            from app.audio.segment_reader import SegmentReader
            if isinstance(source, (list, tuple)):
                reader = SegmentReader(source, prefetched=prefetched)
            else:
                reader = source  # already a file-like
        except Exception as exc:
            print(f"[upnp] passthrough: failed to build source: {exc!r}", flush=True)
            return

        stop_flag = threading.Event()
        done_event = threading.Event()

        # Set passthrough_active BEFORE flush so the audio callback
        # (which checks this flag in push_pcm) doesn't land PCM bytes
        # in the freshly cleared buffer behind the passthrough header.
        session.passthrough_active = True

        # Generate a unique track identifier before flush, so it can
        # be set on the buffer AND embedded in the URL the renderer
        # receives. The HTTP handler validates incoming ?ts= against
        # this value, rejecting stale requests from a previous track.
        import time as _time
        _track_ts = int(_time.monotonic() * 1_000_000)

        # Flush old buffer content so receiver starts at live edge.
        # After this point, push_pcm early-returns because the flag is
        # already set; only the passthrough encoder feeds the buffer.
        session.buffer.flush()
        session.buffer.set_track_id(_track_ts)

        session.passthrough_encoder = FlacPassthroughEncoder(
            source=reader,
            buffer=session.buffer,
            stop_flag=stop_flag,
            done_event=done_event,
        )
        session.passthrough_encoder.start()

        # Notify the renderer of the new track. Falls back to the
        # metadata provider callback if no metadata dict was passed.
        if metadata is None and self._metadata_provider is not None:
            try:
                metadata = self._metadata_provider()
            except Exception as exc:
                print(f"[upnp] metadata provider raised: {exc!r}", flush=True)
        if metadata:
            self._notify_track_change(metadata, session, track_ts=_track_ts)

        print(
            "[upnp] passthrough ON -- bitperfect FLAC passthrough enabled",
            flush=True,
        )

    # ---- track-change notification ----------------------------------

    def _notify_track_change(
        self, metadata: dict, session: _SessionState, track_ts: Optional[int] = None,
    ) -> None:
        """Send SetAVTransportURI + Play to the renderer so it updates
        its now-playing display and initiates a fresh HTTP GET for the
        new track's FLAC stream.

        Without this, the renderer keeps showing the previous track's
        name and its decoder may not re-parse the new STREAMINFO header
        that the passthrough encoder writes into the ring buffer.

        URL UNIQUENESS: appends ``?ts=<timestamp>`` to the stream URL
        so UAPP sees a different URI per track. UAPP ignores
        SetAVTransportURI when the URI is unchanged — it processes
        only Play and continues reading stale buffer data (which was
        flushed with new track content), causing a crash. A fresh URI
        forces UAPP to re-initialize its decoder for the new track.

        ``track_ts`` is the track identifier generated by
        ``start_passthrough`` and set on the RingBuffer. When
        provided, it must match the buffer's current ``track_id``
        so the HTTP handler can validate incoming requests against it.
        """
        if session.av is None or session.stream_url is None:
            return
        try:
            # Unique URL per track: append ?ts= so UAPP re-initializes
            # instead of ignoring the notification (same URI = ignored).
            if track_ts is None:
                import time as _time
                _ts = int(_time.monotonic() * 1_000_000)
            else:
                _ts = track_ts
            _sep = "&" if "?" in session.stream_url else "?"
            _uri = f"{session.stream_url}{_sep}ts={_ts}"
            track_meta = TrackMetadata(
                title=metadata.get("title", ""),
                artist=metadata.get("artist", ""),
                album=metadata.get("album", ""),
                duration_s=metadata.get("duration_s", 0),
                cover_url=metadata.get("cover_url", ""),
                track_uri=_uri,
                mime_type="audio/flac",
            )
            didl = build_didl_lite(track_meta)
            session.av.set_av_transport_uri(_uri, didl)
            session.av.play()
            print(
                f"[upnp] track change notified: {metadata.get('title', '?')} "
                f"url={_uri}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[upnp] track change notification failed: {exc!r}",
                flush=True,
            )

    def signal_source_done(self) -> None:
        """Signal that the current track's source has reached EOF.

        Called by the player when the last track ends with no preload,
        so the passthrough encoder tells the ring buffer the source is
        done. The HTTP serve loop then closes the connection once any
        remaining buffered data is drained.
        """
        with self._session_lock:
            session = self._session
        if session is None:
            return
        if session.passthrough_encoder is not None:
            session.passthrough_encoder.signal_source_done()

    def stop_passthrough(self) -> None:
        """Stop passthrough and revert to PCM re-encode mode."""
        with self._session_lock:
            session = self._session
        if session is None:
            return
        if session.passthrough_encoder is not None:
            try:
                session.passthrough_encoder.close()
            except Exception:
                pass
            session.passthrough_encoder = None
        session.passthrough_active = False
        session._passthrough_source_urls = None
        print("[upnp] passthrough OFF", flush=True)

    # ---- session lifecycle -----------------------------------------

    def connect(self, device_id: str) -> UpnpDevice:
        """Open a session against the given device. Tears down any
        existing session first. Returns the connected UpnpDevice on
        success; raises ValueError / RuntimeError on failure
        (unknown device, descriptor fetch failure, AVTransport
        rejection, HTTP server bind failure).

        Blocks for the duration of the SOAP handshake. Typical
        latency on the LAN is a few hundred ms. We cap at 10s on the
        SOAP requests via `invoke()`'s default.
        """
        if not _UPNP_AVAILABLE:
            raise RuntimeError("async-upnp-client not available")
        device = self.get_device(device_id)
        if device is None:
            raise ValueError(f"unknown DLNA device: {device_id}")

        # Drop any existing session first. Held outside the session
        # lock because disconnect() takes the same lock and would
        # self-deadlock.
        self.disconnect()

        # Re-fetch the full device description. Discovery records
        # only the metadata fields we need for the picker; connect
        # needs the parsed service tree to find the AVTransport
        # control URL.
        try:
            openhome_device = fetch_device(device.location)
        except Exception as exc:
            raise RuntimeError(
                f"failed to fetch device description from "
                f"{device.location}: {exc}"
            ) from exc

        av = AVTransportController.from_device(openhome_device)
        if av is None:
            # Discovery filter should have caught this, but the
            # device may have rebooted between scan and connect.
            raise RuntimeError(
                f"{device.name} does not expose AVTransport. "
                "Device description may have changed since discovery."
            )
        rc = RenderingControlController.from_device(openhome_device)

        # Build the streaming pipeline before issuing
        # SetAVTransportURI so the device's first GET on our URL
        # hits a serving listener. If we issued the SOAP first the
        # device might pull before the HTTP server bound and reject
        # the URL as unreachable.
        session = _SessionState(
            device=device,
            openhome_device=openhome_device,
            av=av,
            rc=rc,
        )
        try:
            session.http_server = start_stream_http_server(
                session.buffer,
                stream_path=_STREAM_PATH,
                content_type="audio/flac",
                dlna=True,
            )
            host_port = session.http_server.server_address[1]
            session.stream_url = (
                f"http://{primary_lan_ip()}:{host_port}{_STREAM_PATH}"
            )
        except Exception as exc:
            raise RuntimeError(
                f"failed to start http stream server: {exc}"
            ) from exc

        # Build a minimal DIDL-Lite for the metadata argument. Real
        # track metadata is filled in later when the player's
        # current track changes. At session start we don't know
        # what's about to play, just that audio is about to start
        # flowing. Empty title / artist are fine; the device
        # displays "Tideway" or just the friendly name from the
        # protocolInfo. `track_uri` is required and matches the URL
        # we send in CurrentURI.
        metadata = TrackMetadata(
            title="Tideway",
            artist="",
            album="",
            duration_s=0,  # 0 = unknown / live stream
            cover_url="",
            track_uri=session.stream_url,
            mime_type="audio/flac",
        )
        didl = build_didl_lite(metadata)

        # Register the session before calling start_passthrough (so it
        # can find the active session) and before play() (so the FLAC
        # header lands in the ring buffer before the renderer's first
        # GET). Duplicate assignment below is idempotent.
        with self._session_lock:
            self._session = session

        # If a track is already loaded, start passthrough before play()
        # so the FLAC header is in the ring buffer when the renderer's
        # first GET arrives. Reduces the race to zero in the common case.
        _notified = False
        if self._source_provider is not None:
            try:
                urls = self._source_provider()
                if urls and isinstance(urls, list):
                    self.start_passthrough(urls)
                    _notified = True
            except Exception as exc:
                log.debug("upnp: source provider raised: %r", exc)

        try:
            if not _notified:
                av.set_av_transport_uri(session.stream_url, didl)
                av.play()
                session.media_loaded = True
        except Exception as exc:
            # Undo the early session registration
            with self._session_lock:
                self._session = None
            # Tear down the HTTP server we just stood up; otherwise
            # we leak a port until the manager's process exits.
            try:
                if session.http_server is not None:
                    session.http_server.shutdown()
                    session.http_server.server_close()
            except Exception:
                pass
            try:
                session.buffer.close()
            except Exception:
                pass
            raise RuntimeError(
                f"AVTransport handshake to {device.name} failed: {exc}"
            ) from exc

        # Mute local audio output. The PCM tap above feeds the DLNA
        # encoder via push_pcm; the silencer just prevents the
        # local sounddevice from also playing.
        if self._local_silencer is not None:
            try:
                self._local_silencer(True)
            except Exception as exc:
                log.debug("upnp: local silencer raised: %r", exc)

        print(
            f"[upnp] connected: {device.name} streaming from "
            f"{session.stream_url}",
            flush=True,
        )
        self._notify_listeners(device)
        return device

    def disconnect(self) -> None:
        """Tear down any active session. Idempotent. Same teardown
        order as Cast: encoder first (drains pending FLAC bytes),
        buffer close (unblocks the HTTP serve loop's read), HTTP
        server shutdown (the loop notices closed buffer and exits),
        AVTransport.Stop last so the device drops its pull cleanly
        rather than seeing a 502 on a half-shut server."""
        with self._session_lock:
            session = self._session
            self._session = None
        if session is None:
            return

        # Stop passthrough encoder first
        if session.passthrough_encoder is not None:
            try:
                session.passthrough_encoder.close()
            except Exception as exc:
                print(
                    f"[upnp] passthrough: encoder close error: {exc!r}",
                    flush=True,
                )
            session.passthrough_encoder = None
            session.passthrough_active = False
            session._passthrough_source_urls = None
        try:
            with session.encoder_lock:
                if session.encoder is not None:
                    try:
                        tail = session.encoder.close()
                        if tail:
                            session.buffer.write(tail, block=False)
                    except Exception as exc:
                        log.debug("encoder close failed: %r", exc)
                    session.encoder = None
        except Exception as exc:
            log.debug("encoder teardown error: %r", exc)
        try:
            session.buffer.close()
        except Exception as exc:
            log.debug("buffer close failed: %r", exc)
        try:
            if session.http_server is not None:
                session.http_server.shutdown()
                session.http_server.server_close()
        except Exception as exc:
            log.debug("http server shutdown failed: %r", exc)
        # Tell the device to stop pulling. Best-effort: if the
        # device has already disconnected we'll get a transport
        # error and that's fine.
        try:
            session.av.stop()
        except Exception as exc:
            log.debug("AVTransport.Stop on disconnect failed: %r", exc)

        if self._local_silencer is not None:
            try:
                self._local_silencer(False)
            except Exception as exc:
                log.debug(
                    "upnp: local silencer raised on close: %r", exc
                )

        print(
            f"[upnp] disconnected: {session.device.name}",
            flush=True,
        )
        self._notify_listeners(None)

    # ---- transport control passthroughs ---------------------------

    def pause(self) -> None:
        """Send AVTransport.Pause. Used by the diversion in
        server.py when DLNA is the active output."""
        with self._session_lock:
            session = self._session
        if session is None:
            return
        try:
            session.av.pause()
        except Exception as exc:
            log.debug("upnp pause failed: %r", exc)

    def play(self) -> None:
        with self._session_lock:
            session = self._session
        if session is None:
            return
        try:
            session.av.play()
        except Exception as exc:
            log.debug("upnp play failed: %r", exc)

    def set_volume(self, level_percent: int) -> None:
        """Set device volume via RenderingControl. No-op when the
        device doesn't expose RC."""
        with self._session_lock:
            session = self._session
        if session is None or session.rc is None:
            return
        try:
            session.rc.set_volume(level_percent)
        except Exception as exc:
            log.debug("upnp set_volume failed: %r", exc)

    def set_mute(self, muted: bool) -> None:
        with self._session_lock:
            session = self._session
        if session is None or session.rc is None:
            return
        try:
            session.rc.set_mute(muted)
        except Exception as exc:
            log.debug("upnp set_mute failed: %r", exc)

    # ---- PCM tap (called from PCMPlayer's audio callback) ---------

    def push_pcm(
        self, pcm: np.ndarray, sample_rate: int, dtype: str
    ) -> None:
        """Feed a PCM chunk into the active session's FLAC encoder.

        Same shape and contract as `CastManager.push_pcm`. Called
        from PCMPlayer's realtime audio callback, so it has to be
        cheap in the no-session case (the lock-free `is_active()`
        short-circuits before this method is even called) and fast
        on the encode path (~1ms per 4096-frame stereo chunk).

        `dtype` may be 'int16', 'int32', or 'float32'. WASAPI
        shared mode delivers the audio callback's PCM as float32
        (the device-mixer format); FLAC is integer-only, so float
        gets converted to int32 here. Same conversion math Cast
        uses for the same reason.
        """
        if pcm.size == 0:
            return
        with self._session_lock:
            session = self._session
        if session is None:
            return
        if session.passthrough_active:
            return  # passthrough encoder feeds the buffer directly
        if dtype == "float32":
            scaled = pcm.astype(np.float64) * 2147483647.0
            np.clip(scaled, -2147483648.0, 2147483647.0, out=scaled)
            pcm = scaled.astype(np.int32)
            dtype = "int32"
        channels = 1 if pcm.ndim == 1 else pcm.shape[1]
        with session.encoder_lock:
            need_new = (
                session.encoder is None
                or session.encoder_rate != sample_rate
                or session.encoder_channels != channels
                or session.encoder_dtype != dtype
            )
            if need_new:
                if session.encoder is not None:
                    try:
                        tail = session.encoder.close()
                        if tail:
                            session.buffer.write(tail, block=False)
                    except Exception as exc:
                        log.debug("encoder close on rebuild: %r", exc)
                try:
                    session.encoder = FlacStreamEncoder(
                        sample_rate=sample_rate,
                        channels=channels,
                        dtype=dtype,
                    )
                    session.encoder_rate = sample_rate
                    session.encoder_channels = channels
                    session.encoder_dtype = dtype
                except Exception as exc:
                    print(
                        f"[upnp] encoder build failed: {exc!r}",
                        flush=True,
                    )
                    return
            if pcm.ndim == 1:
                pcm = pcm.reshape(-1, 1)
            try:
                encoded = session.encoder.encode(pcm)
            except Exception as exc:
                # Same survival posture as Cast: don't crash the
                # realtime thread, let the session run dry, frontend
                # surfaces "0 bytes encoded" plateau.
                #
                # But surface the FIRST failure loudly. A persistent
                # encode failure (e.g. PyAV/FFmpeg dying on a device
                # whose locale makes av.error mis-decode the message)
                # means the device connects to our stream URL and then
                # gets silence forever — the exact "stream never plays"
                # symptom. Burying that at debug level left it
                # undiagnosable. One print per session, then debug for
                # the rest so the realtime thread isn't flooded.
                if not session.encode_failed:
                    session.encode_failed = True
                    print(
                        f"[upnp] flac encode failed (no audio will "
                        f"reach the device until this clears): {exc!r}",
                        flush=True,
                    )
                else:
                    log.debug("flac encode failed: %r", exc)
                return
        if encoded:
            # A successful encode clears the failure latch so a later
            # failure episode reports again instead of staying silent.
            session.encode_failed = False
            session.buffer.write(encoded, block=False)
            session.bytes_encoded += len(encoded)

    # ---- internals -------------------------------------------------

    def _start_loop_thread(self) -> None:
        """Dedicated asyncio loop for SSDP work. Same pattern
        TidalConnectManager uses; keeping the two parallel makes the
        cross-module behaviour predictable."""
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
        """Single-socket SSDP search + per-device descriptor parse.
        Returns only AVTransport-capable devices. Pure OpenHome devices
        get filtered here so they don't pollute the DLNA picker.

        The M-SEARCH/listen step runs on a worker thread (blocking
        socket bound to port 1900 — see _collect_ssdp_locations for
        why); the descriptor fetch + parse stays on the asyncio loop so
        it can reuse async-upnp-client's UpnpFactory.
        """
        devices: dict[str, UpnpDevice] = {}
        requester = AiohttpRequester()
        factory = UpnpFactory(requester)

        loop = asyncio.get_event_loop()
        lan_ip = primary_lan_ip()
        locations, _bound_1900 = await loop.run_in_executor(
            None, _collect_ssdp_locations, timeout, lan_ip
        )

        for location in locations:
            try:
                device = await factory.async_create_device(location)
            except Exception as exc:
                log.debug("upnp: parse %s failed: %s", location, exc)
                continue
            service_types = tuple(
                sorted({s.service_type for s in device.all_services})
            )
            if not _filter_dlna_renderer(service_types):
                # OpenHome-only or otherwise non-DLNA. Skip; the
                # tidal_connect module's discovery handles those.
                continue
            entry = UpnpDevice(
                id=device.udn or location,
                name=(
                    device.friendly_name
                    or device.model_name
                    or "DLNA renderer"
                ),
                manufacturer=device.manufacturer or "",
                model=device.model_name or "",
                location=location,
                service_types=service_types,
                has_avtransport=True,
            )
            devices[entry.id] = entry

        for d in devices.values():
            print(
                f"[upnp] discovered: {d.name} "
                f"({d.manufacturer or 'unknown'}) "
                f"avtransport={d.has_avtransport}",
                flush=True,
            )
        return list(devices.values())


# Module-level singleton, eagerly constructed at first import. Same
# shape as `cast.cast_manager`. The audio callback hits this from
# the realtime thread on every chunk, so the lookup has to be a
# bare module-attribute read with no lock and no lazy-init branch.
# Construction is cheap (one daemon asyncio thread that sits idle
# until refresh() is called).
upnp_manager = UpnpManager()
