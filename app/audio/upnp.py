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
import os
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
    TrackFileSource,
    primary_lan_ip,
    start_stream_http_server,
)
from app.audio.openhome import (
    OpenHomeDevice,
    OpenHomeSOAPError,
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


# How long to wait for a bounded per-track file to finish demuxing
# before announcing its URI to the renderer. A TrackFileSource only
# becomes servable once the whole track is on disk, so announcing early
# makes the renderer's first GET block on that gate; strict renderers
# (UAPP) time out after ~3s and restart their decoder from 0, which
# desyncs them from the desktop clock (issue #354, first-track cut).
# The preload starts ~10s into the current track, so a hi-res track's
# per-track demux (whole file fetched over the LAN) still has minutes to
# finish; this bound only exists so a wedged demux can't hold the
# announce forever.
_TRACK_DEMUX_WAIT_S = 90.0

# How long prepare_next_passthrough waits for the current bounded file's
# TIDAL reader to be released before opening the next track. TIDAL drops
# the session when two different tracks are open (renderer vendor note,
# #354), so a whole-track demux must finish first. Generous bound so a
# slow download doesn't spuriously skip the pre-stage.
_NEXT_SOURCE_RELEASE_WAIT_S = 120.0


# Do not pre-stage the next track on the renderer until the current one
# is within this many seconds of its end. Issue #354 hardware bring-up:
# UAPP crashes in its HTTP client when the pre-staged next file is opened
# while the current track still has minutes to run (it holds two stream
# connections at once). Staging late keeps that overlap brief.
_NEXT_URI_LEAD_S = 30.0
_NEXT_URI_POLL_S = 2.0

# A renderer's UPnP control endpoint can go away mid-session: UAPP
# re-registers its renderer after some track boundaries and its SOAP
# endpoint answers 404 then refuses connections for a while. The session
# keeps the control URL it connected to, so every call fails meanwhile.
# A failed SetNextAVTransportURI is retried across that window (bounded)
# with a re-bind to the freshly discovered device, rather than leaving
# the renderer with no `next` and cutting at track end.
_NEXT_URI_RETRY_INTERVAL_S = 3.0
_NEXT_URI_RETRY_MAX_S = 90.0
# Minimum spacing between re-bind attempts (a re-bind runs a blocking
# SSDP scan), so a dead endpoint can't turn into a discovery storm.
_RENDERER_REBIND_MIN_INTERVAL_S = 15.0


# Renderer-clock watchdog tuning (issue #354). The watchdog polls the
# renderer for its own playback position so the queue advances at the
# renderer's boundary rather than at the desktop decoder's earlier EOF.
# One probe a second: renderers report position at ~1s granularity, so
# faster polling would only add SOAP traffic for no better resolution.
# On renderers that support SetNextAVTransportURI the audio advance is
# native and exact; the poll just catches the CurrentURI flip so the
# frontend queue follows within a second.
_RENDERER_POLL_INTERVAL_S = 1.0
# Consecutive failed/unsupported probes before the watchdog gives up and
# hands the clock back to the player's local decoder.
_RENDERER_POLL_ERROR_LIMIT = 3
# Treat the track as ended once position is this close to the duration.
# Covers the poll granularity plus the renderer's own rounding.
_RENDERER_EOS_EPSILON_S = 0.75
# Hard deadline margin over the expected track length. If none of the
# end-of-track signals fire within the track's duration plus this, the
# watchdog advances anyway rather than hanging the session.
_RENDERER_EOS_MARGIN_S = 20.0
# Position-based end-of-stream may fire at most this many seconds before
# the track's own duration has elapsed in wall time. Guards against a
# renderer that reports RelTime == TrackDuration for a moment right after
# promoting a pre-staged track (UAPP does), which would otherwise fire an
# early advance and cut the track.
_RENDERER_EOS_WALL_TOLERANCE_S = 3.0


def _same_track_uri(current: str, staged: str) -> bool:
    """Whether `current` is the renderer playing the URI we pre-staged.

    Match the full URI first (the common case), then fall back to the
    `?ts=` track id so a renderer that normalizes the URL (host case,
    trailing params) still counts as having consumed it."""
    if current == staged:
        return True
    ts = staged.rsplit("ts=", 1)
    if len(ts) == 2 and ts[1] and current.endswith(f"ts={ts[1]}"):
        return True
    return False


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
    # User opted into gapless album playback: the bounded per-track path
    # with SetNextAVTransportURI pre-staging (a single TIDAL track open
    # at a time) so the renderer advances at the natural boundary.
    gapless: bool = False
    _passthrough_source_urls: Optional[tuple[str, ...]] = None
    _passthrough_done_event: Optional[threading.Event] = None
    # Bounded per-track preload: the NEXT track's buffered file, demuxed
    # during the current track so the renderer's SetAVTransportURI switch
    # can promote it instantly instead of pausing for a full demux.
    next_track_source: Optional[TrackFileSource] = None
    next_source_urls: Optional[tuple[str, ...]] = None
    # Stable key for promotion. TIDAL segment URLs are signed per
    # request and change on every resolve, so URL equality can't be the
    # match — the TIDAL track id can.
    next_track_id: Optional[str] = None
    # True while a watcher thread is retrying a SetNextAVTransportURI
    # that failed because the renderer's control endpoint was
    # unreachable. Keeps a burst of failed calls from spawning several
    # retry threads that each re-stage the same URI.
    next_uri_retry_pending: bool = False
    # The exact URI sent in SetNextAVTransportURI for the pre-staged
    # track, or None. Kept so a watchdog can tell "renderer is
    # TRANSITIONING into the expected next track" from "renderer
    # genuinely stopped" before falling back to a gapped transition.
    renderer_next_uri: Optional[str] = None
    # Set once the CURRENT track's SetAVTransportURI + Play has gone
    # out. SetNextAVTransportURI must not be sent before this: a
    # renderer with no current URI yet treats the "next" as current
    # (UAPP does), so the pre-stage is consumed and lost. Cleared each
    # time a new current track starts in start_passthrough.
    current_announced: threading.Event = field(default_factory=threading.Event)
    # Renderer-clock watchdog state. Incremented every time a new current
    # bounded track starts; a watchdog armed for an older generation sees
    # the mismatch and exits. `renderer_clock_usable` records whether the
    # renderer answers position probes — until the watchdog confirms it
    # can, the player's local (muted) decode clock keeps driving the
    # advance. `renderer_ended_fired` makes the callback fire once per
    # track. See UpnpManager._arm_renderer_watch.
    renderer_watch_gen: int = 0
    renderer_clock_usable: bool = False
    renderer_ended_fired: bool = False
    # Last position the renderer reported for the current track (seconds)
    # and when (monotonic). The player reports this as the playback
    # position while the renderer clock is active: the local decode clock
    # does NOT advance in DLNA passthrough mode, so without this the
    # frontend's position stays near zero and its "10s in, preload the
    # next track" trigger never fires — leaving the following track
    # unprepared and cutting the transition after an auto-advance.
    renderer_position_s: Optional[float] = None
    renderer_duration_s: Optional[float] = None
    renderer_position_at: float = 0.0
    # Monotonic time the current track's watch armed. The renderer can
    # report an implausibly-forward position right after it promotes a
    # pre-staged track (UAPP returns RelTime == TrackDuration for a
    # moment), so the reported position is clamped to the wall time
    # elapsed since the track started — a track cannot be further in
    # than it has actually been playing.
    renderer_started_at: float = 0.0
    # Set when the watchdog ends a track that had a pre-staged next URI.
    # The renderer advances into it on its own at the natural end, so the
    # promotion skips the redundant re-announce without needing
    # GetMediaInfo (which UAPP doesn't answer usefully).
    renderer_consumed_next: bool = False
    # TIDAL track id carried alongside renderer_next_uri, so the watchdog
    # can record WHICH track the renderer advanced into (see
    # renderer_consumed_track_id).
    renderer_next_track_id: Optional[str] = None
    # Sticky evidence that the renderer consumed the pre-staged next URI
    # and advanced into `renderer_consumed_track_id`. Deliberately NOT
    # cleared by _arm_renderer_watch: the consumption is recorded before
    # the new track's watchdog generation resets renderer_consumed_next /
    # renderer_next_uri, and the player's follow-up play_track(next) may
    # arrive after that reset. Cleared only when a fresh next is staged.
    renderer_consumed_track_id: Optional[str] = None
    # Serializes every read-modify-write of the passthrough fields above.
    # Touched from three threads — player pipeline (start/stop), realtime
    # audio callback (push_pcm), and last-track EOF (signal_source_done).
    # Never held across encoder close() or reader build I/O so the audio
    # callback can't stall.
    passthrough_lock: threading.Lock = field(default_factory=threading.Lock)


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
        # Serialises session re-binds (only one discovery+fetch at a
        # time) when several callers notice the control endpoint is
        # unreachable at once.
        self._rebind_lock = threading.Lock()

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

        # Position provider: returns the desktop decoder's current
        # playback position in seconds, or None. Consulted when the
        # session is established so the bounded file served to the
        # renderer starts at the same offset (issue #354: a resume-seek
        # otherwise leaves the renderer at 0 while the desktop is
        # already N seconds in, and the desktop's EOF cuts the tail).
        self._position_provider: Optional[Callable[[], Optional[float]]] = None

        # Next-source provider: returns the NEXT track's segment URLs and
        # metadata, or None. Consulted on connect() so a preload that
        # already exists when the DLNA session starts gets staged on the
        # renderer (otherwise it has no `next` and stops at track end).
        self._next_source_provider: Optional[
            Callable[[], Optional[tuple[list[str], Optional[dict]]]]
        ] = None

        # Renderer-ended callback: invoked when the renderer-clock
        # watchdog detects the current bounded track reached its real
        # end (issue #354). The player uses it to emit `ended` so the
        # frontend advances its queue at the renderer's boundary, not at
        # the desktop decoder's earlier EOF. Only fires while serving a
        # bounded file.
        self._renderer_ended_callback: Optional[Callable[[], None]] = None

        # Lock-free probe for the audio callback: True while the
        # renderer's own clock is driving track advance for a bounded
        # file (so the player must NOT advance on its local EOF). Set by
        # the watchdog only once it confirms the renderer answers
        # position probes; stays False for renderers that don't, leaving
        # the local clock in charge.
        self._renderer_clock_active: bool = False

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

    def bounded_serving(self) -> bool:
        """True while a bounded per-track DLNA file is being served to
        the renderer (a `TrackFileSource`, not the live RingBuffer).

        Used by the player's seek path: while DLNA is serving a bounded
        file, the renderer is the playback clock, so a desktop seek must
        NOT tear down the renderer's source (which would drop it onto
        the ~1TB synthetic RingBuffer stream). Takes the session lock —
        only called from non-realtime paths."""
        with self._session_lock:
            session = self._session
        if session is None:
            return False
        http = getattr(session, "http_server", None)
        return bool(
            http is not None
            and getattr(http, "dlna", False)
            and getattr(http, "track_source", None) is not None
        )

    def is_gapless(self) -> bool:
        """True when the connected session opted into gapless album
        playback (bounded per-track + SetNext pre-stage)."""
        with self._session_lock:
            session = self._session
        return bool(session is not None and session.gapless)

    def last_consumed_track_id(self) -> Optional[str]:
        """The TIDAL track id the renderer auto-advanced into, recorded
        by the watchdog when it detected the pre-staged next URI was
        consumed, or None.

        Sticky across the watchdog's generation reset so the player can
        still match a preload to the renderer's advance after the new
        track's watchdog has armed (issue #354: the reset used to clear
        the evidence before the frontend's follow-up play_track arrived,
        so the redundant SetAVTransportURI went out and UAPP
        Stop/restarted — the boundary cut)."""
        with self._session_lock:
            session = self._session
        if session is None:
            return None
        with session.passthrough_lock:
            return session.renderer_consumed_track_id

    def current_bounded_file(
        self, source_urls, timeout: float = 60.0
    ) -> Optional[str]:
        """Path of the bounded FLAC file being served to the renderer
        for `source_urls`, once it is fully demuxed, or None.

        The player uses this so its own decoder reads the same local
        file the renderer is streaming instead of opening a second TIDAL
        connection. That keeps the account to ONE open track at a time
        (the renderer only ever sees a local file), which is what lets
        the next track be downloaded while the current one plays — see
        issue #354, renderer vendor note about TIDAL's two-track limit.
        """
        with self._session_lock:
            session = self._session
        if session is None:
            return None
        http = getattr(session, "http_server", None)
        if http is None or not getattr(http, "dlna", False):
            return None
        ts = getattr(http, "track_source", None)
        if ts is None:
            return None
        if session._passthrough_source_urls != tuple(source_urls):
            return None
        if not ts.ready.wait(timeout=timeout):
            return None
        if ts.failed or not ts.path:
            return None
        return ts.path

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

    def set_position_provider(
        self, callback: Optional[Callable[[], Optional[float]]]
    ) -> None:
        """Register a callback returning the desktop decoder's current
        playback position in seconds, or None.

        Used when a session is established: the renderer is the
        playback clock, so the bounded file it is handed must begin at
        the offset the desktop decoder is already at. Without this a
        resume-from-position leaves the renderer at 0 while the desktop
        is N seconds in, and the desktop's EOF cuts the tail
        (issue #354)."""
        self._position_provider = callback

    def set_next_source_provider(
        self,
        callback: Optional[
            Callable[[], Optional[tuple[list[str], Optional[dict]]]]
        ],
    ) -> None:
        """Register a callback returning the preloaded next track's
        `(segment_urls, metadata)` or None.

        Consulted once per `connect()` so a preload that already exists
        when the DLNA session starts is staged on the renderer via
        SetNextAVTransportURI. Without it the renderer finishes the
        current track with no next and stops."""
        self._next_source_provider = callback

    def set_renderer_ended_callback(
        self, callback: Optional[Callable[[], None]]
    ) -> None:
        """Register a callback invoked when the renderer reaches the end
        of the current bounded track.

        The player uses it to emit `ended` so the frontend advances its
        queue on the renderer's clock. Only fires while the renderer-clock
        watchdog is active (a bounded file is being served and the
        renderer answers position probes); the Cast / live-RingBuffer
        paths never reach it."""
        self._renderer_ended_callback = callback

    def renderer_clock_active(self) -> bool:
        """Lock-free probe for the audio callback: True while the
        renderer's own clock is driving track advance for a bounded
        file. When True the player must not advance the queue on its
        local decoder's EOF (the muted desktop decoder finishes ahead of
        the renderer and would cut the track)."""
        return self._renderer_clock_active

    def _set_renderer_clock_active(self, active: bool) -> None:
        self._renderer_clock_active = active

    def renderer_position_ms(self) -> Optional[int]:
        """The renderer's own playback position for the current track, in
        milliseconds, or None when there is no usable recent reading.

        The player reports this as the snapshot position while the
        renderer clock is active: in DLNA passthrough the local decode
        clock does not advance, so the frontend's position-based preload
        trigger (fire ~10s into the track) would never fire and the next
        track would go unprepared. Returns None when the clock is not
        active or the last probe is stale (>=3s old), so callers fall
        back to the local clock."""
        progress = self.renderer_progress_s()
        if progress is None:
            return None
        return int(max(0.0, progress[0]) * 1000)

    def renderer_progress_s(self) -> Optional[tuple[float, float]]:
        """The renderer's `(position_s, duration_s)` for the current
        bounded track, or None when the clock is inactive or the reading
        is stale. `duration_s` is the served file's length, so the two
        are on the same timeline and `duration_s - position_s` is the
        true remaining time — the basis for deciding when to pre-stage
        the next URI without relying on the local decode clock."""
        if not self._renderer_clock_active:
            return None
        session = self._session
        if session is None:
            return None
        with session.passthrough_lock:
            position_s = session.renderer_position_s
            duration_s = session.renderer_duration_s
            at = session.renderer_position_at
            started_at = session.renderer_started_at
        if position_s is None or duration_s is None or duration_s <= 0:
            return None
        now = time.monotonic()
        if (now - at) > 3.0:
            return None
        # Never trust a position further ahead than the wall time this
        # track has actually been playing: UAPP reports RelTime ==
        # TrackDuration for a moment right after promoting a pre-staged
        # track, which would fire an early end-of-stream.
        if started_at > 0:
            position_s = min(float(position_s), max(0.0, now - started_at))
        return float(position_s), float(duration_s)

    def _resume_offset_s(self) -> float:
        """Current desktop position as a trim offset, 0.0 when unknown
        or negligible. Sub-second values are decoder startup jitter,
        not a resume."""
        provider = self._position_provider
        if provider is None:
            return 0.0
        try:
            position_s = provider()
        except Exception as exc:
            print(f"[upnp] position provider raised: {exc!r}", flush=True)
            return 0.0
        if not position_s or position_s <= 0.5:
            return 0.0
        return float(position_s)

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
        self, source, prefetched=None, metadata=None,
        *, wait_for_ready: bool = False, start_s: float = 0.0,
        track_id: Optional[str] = None,
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

        http_server = getattr(session, "http_server", None)

        # ---- GUARD + stop old encoder + setup new source ------------
        # The passthrough-state transition is split into two short
        # locked sections with the slow work (encoder close + reader
        # build) BETWEEN them, outside the lock. close() can block up to
        # the ring-buffer write timeout when the old encoder is stuck in
        # a full-buffer write, and push_pcm takes this same lock on the
        # realtime audio thread — holding it across close() would stall
        # audio. Between the two sections passthrough_active stays True
        # with passthrough_encoder cleared: push_pcm reads (active, its
        # done_event) atomically and either skips (event unset) or does
        # a cleanup that the second section's flush() resets.
        _source_urls = tuple(source) if isinstance(source, (list, tuple)) else None
        with session.passthrough_lock:
            if (
                session.passthrough_active
                and session._passthrough_source_urls == _source_urls
            ):
                print(
                    "[upnp] passthrough: already running for this source, "
                    "skipping",
                    flush=True,
                )
                return
            old_encoder = session.passthrough_encoder
            session.passthrough_encoder = None

        # Stop old encoder OUTSIDE the lock — close() may block on a
        # full-buffer write until the timeout, and the audio callback
        # must not wait that out on passthrough_lock.
        if old_encoder is not None:
            try:
                old_encoder.close()
            except Exception:
                pass

        # Build SegmentReader from URLs (network/parse work — off-lock).
        try:
            from app.audio.segment_reader import SegmentReader
            if isinstance(source, (list, tuple)):
                reader = SegmentReader(source, prefetched=prefetched)
            else:
                reader = source
        except Exception as exc:
            print(f"[upnp] passthrough: failed to build source: {exc!r}", flush=True)
            return

        stop_flag = threading.Event()
        done_event = threading.Event()
        import time as _time
        _track_ts = int(_time.monotonic() * 1_000_000)

        with session.passthrough_lock:
            session._passthrough_source_urls = _source_urls
            session._passthrough_done_event = done_event
            session.passthrough_active = True
            session.buffer.flush()
            session.buffer.set_track_id(_track_ts)
            session.passthrough_encoder = None
            # New current track: no announce yet, so any NextURI staged
            # from a previous track must not be sent until this one is.
            session.current_announced.clear()

        # Bounded per-track file: demux the whole track to a temp file so
        # the DLNA response can advertise the REAL byte length. The live
        # RingBuffer encoder (FlacPassthroughEncoder) plus a synthetic
        # ~1TB Content-Length made strict renderers (UAPP) seek past the
        # real track end -> 'unexpected end of stream' -> avcodec
        # -1094995529 (a cut before the end). Buffering per track lets us
        # serve the actual size instead.
        http_server = getattr(session, "http_server", None)
        track_source = None
        promoted = False
        staged_uri: Optional[str] = None
        consumed_next = False
        consumed_track_id: Optional[str] = None
        if (
            http_server is not None
            and getattr(http_server, "dlna", False)
        ):
            prev = getattr(http_server, "track_source", None)
            # Promote a preloaded next-track file if it matches this
            # source (the renderer's track change then serves it
            # immediately — gapless, no demux pause). Otherwise build a
            # fresh bounded file (first track, or a skip to a track that
            # wasn't preloaded).
            with session.passthrough_lock:
                pre_loaded = session.next_track_source
                pre_urls = session.next_source_urls
                pre_track_id = session.next_track_id
                staged_uri = session.renderer_next_uri
                consumed_next = session.renderer_consumed_next
                # Sticky across the watchdog generation reset; see
                # _fire_renderer_ended. Not cleared here so a repeated
                # start_passthrough for the same advance stays a no-op.
                consumed_track_id = session.renderer_consumed_track_id
                session.next_track_source = None
                session.next_source_urls = None
                session.next_track_id = None
                # The pre-staged URI is either promoted below or
                # discarded; either way nothing is pending any more.
                session.renderer_next_uri = None
                session.renderer_consumed_next = False
            # Match by TIDAL track id when both sides have one: the
            # segment URLs are signed per request, so a fresh resolve
            # yields a different URL list for the same track and URL
            # equality would wrongly rebuild the whole file (a multi-
            # second gap). Fall back to URL equality for callers that
            # don't carry an id.
            _promote = pre_loaded is not None and (
                (track_id is not None and pre_track_id == track_id)
                or pre_urls == _source_urls
            )
            if _promote:
                promoted = True
                if prev is not None and prev is not pre_loaded:
                    try:
                        prev.close()
                    except Exception:
                        pass
                # Reuse the pre-staged ts instead of renumbering it. The
                # renderer fetched (or is fetching) this file under the
                # ?ts= sent in SetNextAVTransportURI; giving it a new id
                # here would make those in-flight requests stale (410)
                # and orphan the connection it is reading from.
                _track_ts = pre_loaded.track_id
                with session.passthrough_lock:
                    session.buffer.set_track_id(_track_ts)
                http_server.track_source = pre_loaded
                http_server.next_track_source = None
                track_source = pre_loaded
            else:
                if pre_loaded is not None:
                    log.debug(
                        "upnp: pre-staged file not promoted (urls differ); "
                        "building fresh"
                    )
                    try:
                        pre_loaded.close()
                    except Exception:
                        pass
                if prev is not None:
                    try:
                        prev.close()
                    except Exception:
                        pass
                http_server.next_track_source = None
                # No done_event: the file is served to the renderer up to
                # its real Content-Length, so the track change is driven
                # by the player, not by a demux-complete signal (which
                # for a bounded file happens seconds before the audio
                # ends and would cut off the tail).
                track_source = TrackFileSource(
                    source=reader,
                    track_id=_track_ts,
                    done_event=None,
                    start_s=start_s,
                )
                http_server.track_source = track_source
                track_source.start()
        else:
            session.passthrough_encoder = FlacPassthroughEncoder(
                source=reader,
                buffer=session.buffer,
                stop_flag=stop_flag,
                done_event=done_event,
            )
            session.passthrough_encoder.start()

        # Resolve metadata for the renderer notify below, falling back
        # to the metadata provider callback if no dict was passed.
        if metadata is None and self._metadata_provider is not None:
            try:
                metadata = self._metadata_provider()
            except Exception as exc:
                print(f"[upnp] metadata provider raised: {exc!r}", flush=True)
        # Bounded per-track file: the renderer is the playback clock, so
        # arm the watchdog that advances the queue at the renderer's real
        # end instead of the desktop decoder's earlier EOF. Live-encoder
        # (non-bounded) sessions keep the local clock.
        if track_source is not None:
            self._arm_renderer_watch(session, track_source, metadata, start_s)
        else:
            self._set_renderer_clock_active(False)
        # Passthrough (the encoder -> ring-buffer pipeline) is on as
        # soon as the encoder starts; the device notify below is a
        # separate step, so log it here before the notify can raise.
        print(
            "[upnp] passthrough ON -- bitperfect FLAC passthrough enabled",
            flush=True,
        )
        # Notify the renderer. _notify_track_change raises on failure;
        # the caller decides how bad that is. connect() (initial notify)
        # lets it propagate and tears the session down; mid-session
        # callers in player.py catch and keep the existing stream going.
        if metadata:
            if self._renderer_already_advanced(
                session,
                promoted,
                staged_uri,
                _track_ts,
                consumed_next,
                consumed_track_id=consumed_track_id,
                track_id=track_id,
            ):
                # The renderer consumed the pre-staged SetNextAVTransportURI
                # on its own; it is already playing this track. Sending
                # SetAVTransportURI now would Stop and re-open the stream
                # (an audible gap), so we only sync our side.
                print(
                    f"[upnp] renderer auto-advanced to {metadata.get('title', '?')}; "
                    "not re-announcing",
                    flush=True,
                )
                with session.passthrough_lock:
                    session.current_announced.set()
                return
            self._announce_track(
                session,
                track_source,
                metadata,
                _track_ts,
                wait_for_ready=wait_for_ready,
            )

    # ---- renderer-clock watchdog ------------------------------------
    #
    # Issue #354: the renderer is the playback clock, but it buffers
    # ahead and its decoder re-inits on a SetAVTransportURI, so the
    # desktop decoder's EOF is not a safe "track ended" trigger — it
    # fires early and cuts the tail. When the renderer can report its
    # own position, poll it and fire the player's ended callback exactly
    # at the renderer's boundary. Renderers that don't answer position
    # probes leave the local decode clock in charge (degraded, but no
    # worse than before this watchdog existed).

    def _arm_renderer_watch(
        self,
        session: "_SessionState",
        track_source: "TrackFileSource",
        metadata: Optional[dict],
        start_s: float = 0.0,
    ) -> None:
        """Start the watchdog for a freshly-served bounded track.

        Bumps the session generation so any watchdog still polling for
        the previous track exits, and resets the clock-active gate until
        this watchdog confirms the renderer is pollable.

        ``start_s`` is the trim applied to the served file, so a fallback
        duration derived from the track's full metadata is reduced by it:
        the renderer's position runs over the trimmed file, starting at
        zero."""
        fallback_duration: Optional[float] = None
        if metadata:
            try:
                duration = metadata.get("duration_s")
                if duration:
                    fallback_duration = max(0.0, float(duration) - float(start_s))
            except (TypeError, ValueError):
                fallback_duration = None
        with session.passthrough_lock:
            session.renderer_watch_gen += 1
            gen = session.renderer_watch_gen
            session.renderer_clock_usable = False
            session.renderer_ended_fired = False
            session.renderer_consumed_next = False
            # renderer_consumed_track_id is deliberately NOT cleared here:
            # it is sticky evidence of the advance the renderer just made,
            # and the player's follow-up play_track(next) may still be in
            # flight. It is cleared when a fresh next is staged.
            session.renderer_next_track_id = None
            # A fresh current track has no pre-staged next yet (that is
            # sent later, when the frontend preloads). The previous
            # track's staged URI is exactly what the renderer just
            # consumed, so leaving it here makes the new watchdog's
            # Signal 1 see CurrentURI == renderer_next_uri on its first
            # poll and fire `ended` immediately — a premature advance.
            session.renderer_next_uri = None
            session.renderer_started_at = time.monotonic()
        self._set_renderer_clock_active(False)
        print(
            f"[upnp] renderer watch armed gen={gen} "
            f"fallback_dur={fallback_duration}",
            flush=True,
        )
        threading.Thread(
            target=self._renderer_watch,
            args=(session, track_source, gen, fallback_duration),
            name="upnp-renderer-watch",
            daemon=True,
        ).start()

    def _renderer_watch(
        self,
        session: "_SessionState",
        track_source: "TrackFileSource",
        gen: int,
        fallback_duration: Optional[float],
    ) -> None:
        """Poll the renderer until it finishes the current bounded track.

        End-of-track signals, in order of reliability:

        1. A pre-staged SetNextAVTransportURI was consumed — the
           renderer's CurrentURI is now that URI. This is the DLNA-native
           gapless advance and is exact.
        2. GetPositionInfo's RelTime reached TrackDuration (or the
           metadata duration when the renderer omits TrackDuration).

        GetTransportInfo's STOPPED state is deliberately NOT used: UAPP
        reports a transient STOPPED while re-initialising its decoder at
        the start of a track, which fired this watchdog prematurely and
        desynced the session. Position polling is enough for renderers
        that report it.

        A deadline (the track duration plus a margin) guarantees the
        watchdog fires even when the renderer answers neither signal
        usefully: stalling playback at end-of-track is worse than an
        early advance.
        """
        errors = 0
        deadline: Optional[float] = None
        last_rebind = 0.0
        # Reference for the wall-time guard: when this track started.
        armed_at = session.renderer_started_at or time.monotonic()
        if fallback_duration and fallback_duration > 0:
            deadline = time.monotonic() + fallback_duration + _RENDERER_EOS_MARGIN_S
        while True:
            time.sleep(_RENDERER_POLL_INTERVAL_S)
            with self._session_lock:
                if self._session is not session:
                    return
            with session.passthrough_lock:
                if session.renderer_watch_gen != gen:
                    return
                if session.renderer_ended_fired:
                    return
                staged_uri = session.renderer_next_uri

            # Signal 1: the renderer consumed the pre-staged next URI.
            if staged_uri:
                try:
                    current_uri = session.av.get_current_uri()
                except Exception as exc:
                    current_uri = None
                    log.debug("renderer CurrentURI probe failed: %r", exc)
                    # The renderer's control endpoint may have moved (UAPP
                    # re-registers its UPnP device at a boundary). Re-bind
                    # so the staged-URI probe recovers instead of missing
                    # the auto-advance and falling through to the deadline.
                    now = time.monotonic()
                    if now - last_rebind >= _RENDERER_REBIND_MIN_INTERVAL_S:
                        last_rebind = now
                        self._rebind_session_renderer(session)
                if current_uri and _same_track_uri(current_uri, staged_uri):
                    self._fire_renderer_ended(
                        session,
                        gen,
                        f"uri-consumed current={current_uri} staged={staged_uri}",
                    )
                    return

            # Signal 2: reported position reached the track duration.
            try:
                position = session.av.get_position_seconds()
            except Exception as exc:
                position = None
                log.debug("renderer position probe failed: %r", exc)
            if position is not None:
                errors = 0
                position_s, renderer_duration = position
                # Prefer the metadata duration: a renderer that just
                # promoted a pre-staged track can report the previous
                # track's TrackDuration (UAPP does), and using it would
                # fire end-of-stream far too early.
                duration_s = fallback_duration if fallback_duration else renderer_duration
                with session.passthrough_lock:
                    session.renderer_position_s = position_s
                    session.renderer_duration_s = duration_s
                    session.renderer_position_at = time.monotonic()
                if duration_s and duration_s > 0:
                    if deadline is None:
                        deadline = (
                            time.monotonic() + duration_s + _RENDERER_EOS_MARGIN_S
                        )
                    # The renderer answers position probes: from here on
                    # the player must stop advancing on its own (earlier)
                    # local EOF.
                    if not session.renderer_clock_usable:
                        with session.passthrough_lock:
                            session.renderer_clock_usable = True
                        self._set_renderer_clock_active(True)
                        print(
                            f"[upnp] renderer clock engaged: pos={position_s:.1f} "
                            f"dur={duration_s:.1f} fallback={fallback_duration}",
                            flush=True,
                        )
                    if position_s >= duration_s - _RENDERER_EOS_EPSILON_S:
                        # Guard against a renderer reporting RelTime ==
                        # TrackDuration right after it promotes a
                        # pre-staged track (UAPP does): the track cannot
                        # have ended before its duration has actually
                        # elapsed, so only fire once the wall clock
                        # agrees. Otherwise keep polling.
                        if (
                            time.monotonic() - armed_at
                            >= duration_s - _RENDERER_EOS_WALL_TOLERANCE_S
                        ):
                            self._fire_renderer_ended(
                                session,
                                gen,
                                f"position {position_s:.1f}/{duration_s:.1f}",
                            )
                            return
            else:
                errors += 1

            # Safety net: no usable signal within the track's length plus
            # a generous margin. Fire rather than hang the session — but
            # ONLY when no next URI is staged. With a next staged, Signal 1
            # (the renderer consumed that URI) is authoritative; firing on
            # the deadline instead is what produced a late re-announce of
            # the track the renderer was already playing (a stop/restart).
            if (
                deadline is not None
                and time.monotonic() > deadline
                and not staged_uri
            ):
                self._fire_renderer_ended(
                    session, gen, f"deadline pos={position_s if position else None}"
                )
                return
            if errors >= _RENDERER_POLL_ERROR_LIMIT:
                # Position is transiently unavailable — UAPP reports
                # NOT_IMPLEMENTED while re-initialising its decoder right
                # after an auto-advance. Firing `ended` here would advance
                # the queue at the very start of the new track and desync
                # the session. Never advance on probe failure: the
                # deadline above (or a staged-URI change) fires the real
                # end. Keep polling.
                errors = 0
                # The endpoint may also have moved (UAPP re-registers its
                # UPnP device at some track boundaries). Try to re-bind so
                # the clock and the staged-URI probe recover, rate-limited
                # so a dead endpoint doesn't spam discovery.
                now = time.monotonic()
                if now - last_rebind >= _RENDERER_REBIND_MIN_INTERVAL_S:
                    last_rebind = now
                    self._rebind_session_renderer(session)

    def _fire_renderer_ended(
        self, session: "_SessionState", gen: int, reason: str = ""
    ) -> None:
        """Fire the ended callback once for this track generation.

        Sets the clock-active gate first: the player's callback checks it
        and would otherwise no-op, and by the time we fire we know the
        renderer's clock is the one that matters."""
        with session.passthrough_lock:
            if session.renderer_watch_gen != gen or session.renderer_ended_fired:
                return
            session.renderer_ended_fired = True
            session.renderer_clock_usable = True
            if session.renderer_next_uri:
                session.renderer_consumed_next = True
                # Sticky evidence of WHICH track the renderer advanced
                # into. Survives the new track's _arm_renderer_watch
                # (which clears renderer_consumed_next / renderer_next_uri)
                # so the player can still match a cast preload when the
                # frontend's play_track(next) lands after the reset.
                session.renderer_consumed_track_id = (
                    session.renderer_next_track_id
                )
        self._set_renderer_clock_active(True)
        print(
            f"[upnp] renderer reached end; firing advance ({reason})",
            flush=True,
        )
        cb = self._renderer_ended_callback
        if cb is None:
            return
        try:
            cb()
        except Exception as exc:
            print(f"[upnp] renderer-ended callback failed: {exc!r}", flush=True)

    def _renderer_already_advanced(
        self,
        session: "_SessionState",
        promoted: bool,
        staged_uri: Optional[str],
        track_ts: int,
        consumed_next: bool = False,
        *,
        consumed_track_id: Optional[str] = None,
        track_id: Optional[str] = None,
    ) -> bool:
        """True when the renderer is already on the track we promoted.

        ``consumed_track_id`` is the sticky, probe-free evidence that the
        watchdog recorded the renderer consuming the pre-staged next URI
        and advancing into that TIDAL track. It is authoritative and
        survives the watchdog generation reset, so the redundant announce
        is skipped even when the renderer's control endpoint has moved
        and ``get_current_uri`` would fail. ``consumed_next`` is the
        older boolean form of the same signal. The renderer's CurrentURI
        (matching the staged URI or carrying the promoted ts) is a
        fallback. Announcing now would Stop and reopen the stream (an
        audible stop/restart).
        """
        # The watchdog saw the renderer end while a next URI was staged,
        # so it advanced into it. Authoritative regardless of whether we
        # also promoted the file server-side.
        if consumed_next:
            return True
        if not promoted:
            return False
        if (
            consumed_track_id is not None
            and track_id is not None
            and str(track_id) == str(consumed_track_id)
        ):
            return True
        if session.av is None:
            return False
        if not session.av.supports_next_uri():
            return False
        try:
            current = session.av.get_current_uri()
        except Exception as exc:
            log.debug("renderer CurrentURI probe failed: %r", exc)
            return False
        if not isinstance(current, str) or not current:
            return False
        if staged_uri and current == staged_uri:
            return True
        return current.endswith(f"ts={track_ts}")

    def _announce_track(
        self,
        session: "_SessionState",
        track_source: Optional[TrackFileSource],
        metadata: dict,
        track_ts: int,
        *,
        wait_for_ready: bool,
    ) -> None:
        """Send SetAVTransportURI + Play, waiting for the bounded file
        if it is still demuxing.

        A ``TrackFileSource`` is only servable once ``ready`` is set,
        and ``_serve_track`` blocks on that gate before answering. A
        renderer that GETs during the demux therefore waits on the
        server; UAPP times out after ~3s and restarts its decoder from
        0, desyncing it from the desktop clock (issue #354). So the
        URI is not announced until the file can be served immediately.

        ``wait_for_ready`` is True on the connect() path, which needs a
        rejected URI to propagate so the session tears down instead of
        reporting a false "connected". Mid-session changes stay
        best-effort on a watcher thread, matching
        ``_notify_track_change``'s documented policy.
        """
        if track_source is None or track_source.ready.is_set():
            self._notify_track_change(metadata, session, track_ts=track_ts)
            return
        if wait_for_ready:
            if not track_source.ready.wait(timeout=_TRACK_DEMUX_WAIT_S):
                # Still demuxing after the bound. Announce anyway so the
                # renderer is not left with no track; _serve_track keeps
                # waiting on the same gate. Degraded, but no worse than
                # announcing immediately.
                print(
                    f"[upnp] bounded file not ready after "
                    f"{_TRACK_DEMUX_WAIT_S:.0f}s; announcing anyway",
                    flush=True,
                )
            elif track_source.failed or track_source.path is None:
                # The file never materialised: there is nothing to serve,
                # so fail the connect rather than report a false good.
                raise RuntimeError("bounded track file failed to demux")
            self._notify_track_change(metadata, session, track_ts=track_ts)
            return
        threading.Thread(
            target=self._notify_when_ready,
            args=(session, track_source, metadata, track_ts),
            name="upnp-announce-ready",
            daemon=True,
        ).start()

    def _notify_when_ready(
        self,
        session: "_SessionState",
        track_source: TrackFileSource,
        metadata: dict,
        track_ts: int,
    ) -> None:
        """Watcher-thread body for the mid-session deferred announce."""
        if not track_source.ready.wait(timeout=_TRACK_DEMUX_WAIT_S):
            print(
                "[upnp] bounded file not ready in time; track not announced",
                flush=True,
            )
            return
        if track_source.failed or track_source.path is None:
            print("[upnp] bounded file failed; track not announced", flush=True)
            return
        # Skip if the session ended or a newer track superseded this
        # one while the demux ran — announcing then would hand the
        # renderer a stale URI.
        with self._session_lock:
            current = self._session
        http_server = getattr(session, "http_server", None)
        if (
            current is not session
            or http_server is None
            or getattr(http_server, "track_source", None) is not track_source
        ):
            return
        try:
            self._notify_track_change(metadata, session, track_ts=track_ts)
        except Exception as exc:
            print(
                f"[upnp] deferred track change notification failed: {exc!r}",
                flush=True,
            )

    def prepare_next_passthrough(
        self, source_urls, prefetched=None, track_id=None
    ) -> None:
        """Demux the NEXT track's FLAC to a buffered temp file while the
        current one plays, so the renderer's track change can promote it
        instantly — gapless with no demux pause.

        Called by the player when it preloads the following track (it
        already knows that track's source URLs via the PCM preload). If a
        different source was preloaded previously, it is replaced. A stale
        preload that never gets promoted is closed on the next
        start_passthrough / disconnect.
        """
        with self._session_lock:
            session = self._session
        if session is None:
            return
        http_server = getattr(session, "http_server", None)
        if http_server is None or not getattr(http_server, "dlna", False):
            return
        if not session.passthrough_active:
            return
        if not isinstance(source_urls, (list, tuple)):
            return
        _src = tuple(source_urls)
        _tid = str(track_id) if track_id is not None else None
        with session.passthrough_lock:
            if session.next_track_source is not None and (
                (_tid is not None and session.next_track_id == _tid)
                or session.next_source_urls == _src
            ):
                return
            old = session.next_track_source
            session.next_track_source = None
            session.next_source_urls = None
            session.next_track_id = None
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        # Single-flight TIDAL: don't open the NEXT track while the
        # CURRENT bounded file's reader is still alive. Only one TIDAL
        # track may be open at a time (renderer vendor note on #354).
        # The current reader is released the moment its full demux
        # finishes (TrackFileSource._release_source) and the player
        # decodes from that local file, so this waits on the download,
        # not on the whole track.
        cur = getattr(http_server, "track_source", None)
        if cur is not None and not cur.source_released.wait(
            timeout=_NEXT_SOURCE_RELEASE_WAIT_S
        ):
            print(
                "[upnp] prepare_next_passthrough: current TIDAL source "
                "still open; not starting next",
                flush=True,
            )
            return
        try:
            from app.audio.segment_reader import SegmentReader
            import time as _time
            reader = SegmentReader(list(_src), prefetched=prefetched or {})
            # Unique ts so the pre-staged file is addressable under its
            # own ?ts= while the current track still occupies the stream
            # URL. On promotion this same id becomes the current ts, so
            # the renderer's in-flight requests stay valid.
            ts = TrackFileSource(
                source=reader,
                track_id=int(_time.monotonic() * 1_000_000),
            )
        except Exception as exc:
            print(
                f"[upnp] prepare_next_passthrough failed: {exc!r}",
                flush=True,
            )
            return
        with session.passthrough_lock:
            session.next_track_source = ts
            session.next_source_urls = _src
            session.next_track_id = _tid
            # A fresh next is staged: the previous track's consumption
            # evidence is no longer needed and must not match a later
            # reuse of the same track id.
            session.renderer_consumed_track_id = None
        ts.start()

    def set_next_track(
        self, source, prefetched=None, metadata: Optional[dict] = None,
        track_id=None,
    ) -> bool:
        """Pre-stage the next track on the renderer so it advances on
        its own at the natural end (DLNA-native gapless).

        Returns True when the pre-stage was accepted. False when the
        renderer doesn't advertise SetNextAVTransportURI or the file /
        metadata couldn't be built — the caller then keeps the existing
        gapped advance.

        The bounded file is shared with the gapped path: it is built
        here and promoted by start_passthrough when the next track
        actually starts, whichever trigger fired. The URI is only sent
        once the file is servable, so the renderer never prefetches a
        half-demuxed file and times out on it (same rule as fix A).
        """
        with self._session_lock:
            session = self._session
        if session is None or session.av is None:
            print("[upnp] set_next_track: no session/av", flush=True)
            return False
        # Only the gapless opt-in uses the SetNext pre-stage. Without
        # it the session advances at the boundary with a plain
        # SetAVTransportURI (small gap), so no second stream is opened.
        if not session.gapless:
            return False
        http_server = getattr(session, "http_server", None)
        if http_server is None or not getattr(http_server, "dlna", False):
            print("[upnp] set_next_track: no dlna http server", flush=True)
            return False
        if not session.passthrough_active:
            print("[upnp] set_next_track: passthrough inactive", flush=True)
            return False
        if not isinstance(source, (list, tuple)) or not source:
            print("[upnp] set_next_track: no source urls", flush=True)
            return False
        # Build (or refresh) the next track's bounded file. Pass the
        # TIDAL track id so promotion at the boundary can match on it
        # (signed segment URLs differ per resolve).
        self.prepare_next_passthrough(source, prefetched, track_id=track_id)
        with session.passthrough_lock:
            ts_next = session.next_track_source
        if ts_next is None:
            print("[upnp] set_next_track: no bounded next source", flush=True)
            return False
        if not session.av.supports_next_uri():
            log.debug(
                "upnp: renderer has no SetNextAVTransportURI; gapped advance"
            )
            return False
        if metadata is None and self._metadata_provider is not None:
            try:
                metadata = self._metadata_provider()
            except Exception as exc:
                print(f"[upnp] metadata provider raised: {exc!r}", flush=True)
        if not metadata:
            print("[upnp] set_next_track: no metadata", flush=True)
            return False
        # Serve the pre-staged file under its own ts while the current
        # track is still the active source.
        http_server.next_track_source = ts_next
        _sep = "&" if "?" in session.stream_url else "?"
        uri = f"{session.stream_url}{_sep}ts={ts_next.track_id}"
        didl = self._build_track_didl(metadata, session, uri)
        if not session.current_announced.is_set():
            # The current track has not been announced yet (preload can
            # beat the connect-time announce). Sending NextURI now would
            # make a renderer with no current treat it as current; wait
            # on a watcher thread until the current is live.
            threading.Thread(
                target=self._send_next_when_announced,
                args=(session, ts_next, uri, didl, metadata),
                name="upnp-next-announced",
                daemon=True,
            ).start()
            return True
        if ts_next.ready.is_set():
            return self._send_next_uri(session, ts_next, uri, didl, metadata)
        threading.Thread(
            target=self._send_next_when_ready,
            args=(session, ts_next, uri, didl, metadata),
            name="upnp-next-ready",
            daemon=True,
        ).start()
        return True

    def _send_next_uri(
        self,
        session: "_SessionState",
        ts_next: TrackFileSource,
        uri: str,
        didl: str,
        metadata: dict,
    ) -> bool:
        """Send SetNextAVTransportURI for a servable pre-staged file.

        No-op when the pre-stage was invalidated or superseded while the
        demux ran — the renderer must not receive a URI we've dropped.
        Held back until the current track is near its end (see
        ``_next_uri_lead_reached``) so the renderer doesn't hold two
        stream connections open for minutes on end.
        """
        with session.passthrough_lock:
            if session.next_track_source is not ts_next:
                return False
        # Stage as soon as the file is ready. UAPP 7.1.2.8 fixed the crash
        # that made holding a pre-staged "next" connection while the
        # current track still plays unsafe (7.1.1.4 crashed in
        # HTTPStreamProvider::cleanUp). Staging early also removes the
        # old ~30 s lead gate, whose deferred send could stall and let the
        # watchdog fall back to a deadline advance (a gap).
        try:
            session.av.set_next_av_transport_uri(uri, didl)
        except Exception as exc:
            # The renderer's control endpoint can go away mid-session
            # (UAPP re-registers its UPnP device at some track
            # boundaries, moving the control port). Don't give up and
            # leave the renderer with no `next`: retry on a watcher
            # thread, re-binding to the freshly discovered device, until
            # the pre-stage is superseded or the retry window closes.
            print(
                f"[upnp] set_next_av_transport_uri failed, retrying: "
                f"{exc!r}",
                flush=True,
            )
            with session.passthrough_lock:
                if session.next_uri_retry_pending:
                    return False  # a retry for this pre-stage is in flight
                session.next_uri_retry_pending = True
            threading.Thread(
                target=self._send_next_uri_retry,
                args=(session, ts_next, uri, didl, metadata),
                name="upnp-next-retry",
                daemon=True,
            ).start()
            return False
        with session.passthrough_lock:
            if session.next_track_source is ts_next:
                session.renderer_next_uri = uri
                session.renderer_next_track_id = session.next_track_id
        print(
            f"[upnp] nextURI staged: {metadata.get('title', '?')} "
            f"url={uri}",
            flush=True,
        )
        return True

    def _send_next_uri_retry(
        self,
        session: "_SessionState",
        ts_next: TrackFileSource,
        uri: str,
        didl: str,
        metadata: dict,
    ) -> None:
        """Retry a failed SetNextAVTransportURI while the renderer's
        control endpoint comes back.

        Bounded by ``_NEXT_URI_RETRY_MAX_S`` and abandoned the moment the
        pre-stage is superseded (the next track starts, the user skips,
        the session closes). Re-binds the session to the renderer at most
        once every ``_RENDERER_REBIND_MIN_INTERVAL_S`` so a prolonged
        outage doesn't turn into a discovery storm."""
        deadline = time.monotonic() + _NEXT_URI_RETRY_MAX_S
        try:
            self._send_next_uri_retry_loop(
                session, ts_next, uri, didl, metadata, deadline
            )
        finally:
            with session.passthrough_lock:
                session.next_uri_retry_pending = False

    def _send_next_uri_retry_loop(
        self,
        session: "_SessionState",
        ts_next: TrackFileSource,
        uri: str,
        didl: str,
        metadata: dict,
        deadline: float,
    ) -> None:
        last_rebind = 0.0
        while time.monotonic() < deadline:
            time.sleep(_NEXT_URI_RETRY_INTERVAL_S)
            with self._session_lock:
                if self._session is not session:
                    return
            with session.passthrough_lock:
                if session.next_track_source is not ts_next:
                    return  # superseded or already promoted to current
            now = time.monotonic()
            if now - last_rebind >= _RENDERER_REBIND_MIN_INTERVAL_S:
                last_rebind = now
                self._rebind_session_renderer(session)
            try:
                session.av.set_next_av_transport_uri(uri, didl)
            except Exception:
                continue
            with session.passthrough_lock:
                if session.next_track_source is ts_next:
                    session.renderer_next_uri = uri
                    session.renderer_next_track_id = session.next_track_id
            print(
                f"[upnp] nextURI staged on retry: "
                f"{metadata.get('title', '?')} url={uri}",
                flush=True,
            )
            return
        print(
            "[upnp] renderer control endpoint unreachable; next not "
            "pre-staged",
            flush=True,
        )

    def _rebind_session_renderer(self, session: "_SessionState") -> bool:
        """Re-point the session's controllers after the renderer's control
        endpoint has gone away.

        A renderer can re-register its UPnP device mid-session, which
        moves its control URL; the session keeps the URL it connected to,
        so every subsequent SOAP call fails until we re-discover. Match by
        UDN — the renderer's identity that is stable across a
        re-registration — and rebuild the AVTransport / RenderingControl
        controllers from the freshly fetched description.

        Returns True when the session was re-bound. False when the device
        was not found, the control URL was unchanged (a transient outage,
        not a move — the caller's retry handles that), or the fetch
        failed."""
        target = session.device
        if not self._rebind_lock.acquire(blocking=False):
            return False  # a re-bind is already in flight
        try:
            try:
                devices = self.refresh(timeout=4.0)
            except Exception as exc:
                log.debug("upnp: re-bind discovery failed: %r", exc)
                return False
            match = next((d for d in devices if d.id == target.id), None)
            if match is None or match.location == target.location:
                return False
            try:
                fresh = fetch_device(match.location)
            except Exception as exc:
                log.debug("upnp: re-bind fetch failed: %r", exc)
                return False
            av = AVTransportController.from_device(fresh)
            if av is None:
                return False
            rc = RenderingControlController.from_device(fresh)
            with self._session_lock:
                if self._session is not session:
                    return False
                session.device = match
                session.openhome_device = fresh
                session.av = av
                session.rc = rc
            print(
                f"[upnp] renderer control endpoint re-bound to "
                f"{match.location}",
                flush=True,
            )
            return True
        finally:
            self._rebind_lock.release()

    def _next_uri_lead_reached(self, session: "_SessionState") -> bool:
        """True when the current track is close enough to its end to
        pre-stage the next one.

        Prefers the renderer's own remaining time when its clock is
        active: the local decode clock does not advance in DLNA
        passthrough, so a local-position gate would never open and the
        next URI would never be sent. Falls back to the desktop decoder's
        position plus the current track's metadata duration, and returns
        True (stage immediately) when neither is usable."""
        progress = self.renderer_progress_s()
        if progress is not None:
            position_s, duration_s = progress
            return (duration_s - position_s) <= _NEXT_URI_LEAD_S
        provider = getattr(self, "_position_provider", None)
        meta_provider = getattr(self, "_metadata_provider", None)
        if provider is None or meta_provider is None:
            return True
        try:
            position_s = provider()
            meta = meta_provider()
        except Exception:
            return True
        if position_s is None or not meta:
            return True
        try:
            duration_s = float(meta.get("duration_s") or 0.0)
        except (TypeError, ValueError):
            return True
        if duration_s <= 0:
            return True
        return (duration_s - float(position_s)) <= _NEXT_URI_LEAD_S

    def _send_next_when_due(
        self,
        session: "_SessionState",
        ts_next: TrackFileSource,
        uri: str,
        didl: str,
        metadata: dict,
    ) -> None:
        """Watcher-thread body: wait until the current track is near its
        end, then send the pre-staged NextURI."""
        while True:
            time.sleep(_NEXT_URI_POLL_S)
            with self._session_lock:
                if self._session is not session:
                    return
            with session.passthrough_lock:
                if session.next_track_source is not ts_next:
                    return
            if self._next_uri_lead_reached(session):
                break
        self._send_next_uri(session, ts_next, uri, didl, metadata)

    def _send_next_when_announced(
        self,
        session: "_SessionState",
        ts_next: TrackFileSource,
        uri: str,
        didl: str,
        metadata: dict,
    ) -> None:
        """Watcher-thread body: wait until the current track has been
        announced, then proceed to stage the next one."""
        if not session.current_announced.wait(timeout=_TRACK_DEMUX_WAIT_S):
            print(
                "[upnp] current track not announced in time; "
                "next not pre-staged",
                flush=True,
            )
            return
        with self._session_lock:
            if self._session is not session:
                return
        self._send_next_when_ready(session, ts_next, uri, didl, metadata)

    def _send_next_when_ready(
        self,
        session: "_SessionState",
        ts_next: TrackFileSource,
        uri: str,
        didl: str,
        metadata: dict,
    ) -> None:
        """Watcher-thread body: send the next URI once the file is ready."""
        if not ts_next.ready.wait(timeout=_TRACK_DEMUX_WAIT_S):
            print(
                "[upnp] next track not ready in time; not pre-staged",
                flush=True,
            )
            return
        if ts_next.failed or ts_next.path is None:
            print(
                "[upnp] next track file failed; not pre-staged",
                flush=True,
            )
            return
        with self._session_lock:
            current = self._session
        if current is not session:
            return
        self._send_next_uri(session, ts_next, uri, didl, metadata)

    def invalidate_next_track(self) -> None:
        """Drop a pre-staged next track.

        Called on any explicit change (seek, stop, user track change):
        the renderer's staged NextURI no longer matches what the user
        asked for, so the file must stop being served and be deleted.
        There is no AVTransport action to clear NextURI; the next
        SetAVTransportURI (or Stop) overrides it on the renderer.
        """
        with self._session_lock:
            session = self._session
        if session is None:
            return
        http_server = getattr(session, "http_server", None)
        with session.passthrough_lock:
            ts_next = session.next_track_source
            session.next_track_source = None
            session.next_source_urls = None
            had_uri = session.renderer_next_uri is not None
            session.renderer_next_uri = None
        if http_server is not None:
            if getattr(http_server, "next_track_source", None) is ts_next:
                http_server.next_track_source = None
        if ts_next is not None:
            try:
                ts_next.close()
            except Exception:
                pass
        if ts_next is not None or had_uri:
            print("[upnp] pre-staged next track invalidated", flush=True)

    def _build_track_didl(
        self, metadata: dict, session: "_SessionState", uri: str
    ) -> str:
        """Build the DIDL-Lite for `metadata`, pointing albumArtURI at
        our own cover proxy so the renderer can reach the image."""
        cover_url = ""
        cover_id = metadata.get("cover_id")
        if cover_id:
            _base = session.stream_url.rsplit(_STREAM_PATH, 1)[0]
            cover_url = f"{_base}/cover/{cover_id}"
        track_meta = TrackMetadata(
            title=metadata.get("title", ""),
            artist=metadata.get("artist", ""),
            album=metadata.get("album", ""),
            duration_s=metadata.get("duration_s", 0),
            cover_url=cover_url,
            track_uri=uri,
            mime_type="audio/flac",
        )
        return build_didl_lite(track_meta)

    # ---- track-change notification ----------------------------------

    def _notify_track_change(
        self, metadata: dict, session: _SessionState, track_ts: Optional[int] = None,
    ) -> None:
        """Send SetAVTransportURI + Play to the renderer so it updates
        its now-playing display and initiates a fresh HTTP GET for the
        new track's FLAC stream.

        Raises on failure (an OpenHomeSOAPError if the device rejected
        the action, or the underlying transport error otherwise). The
        caller owns the policy, because whether a failed notify is fatal
        depends on the context, not the exception: connect() treats a
        rejected initial notify as fatal and tears the session down (a
        Rygel renderer returning UPnP 716 must not report a false
        "connected"); a mid-session track change treats it as
        best-effort and keeps the existing stream flowing.

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
            # Point albumArtURI at the cover we proxy on THIS server
            # (LAN-reachable 0.0.0.0 stream listener). The renderer
            # can't reach Tidal's image CDN directly, so a remote
            # cover_url never resolved to a picture before.
            didl = self._build_track_didl(metadata, session, _uri)
            session.av.set_av_transport_uri(_uri, didl)
            session.av.play()
            with session.passthrough_lock:
                # Sending a new current URI supersedes any pending
                # NextURI on the renderer; our bookkeeping follows.
                session.renderer_next_uri = None
                # The current track is live on the renderer: a next
                # track may now be safely pre-staged.
                session.current_announced.set()
            print(
                f"[upnp] track change notified: {metadata.get('title', '?')} "
                f"url={_uri}",
                flush=True,
            )
        except Exception as exc:
            # Surface the SOAP fault detail the device sent (the request
            # it refused + its raw fault body), which the numeric UPnP
            # code alone doesn't explain, then let the caller decide.
            detail = ""
            if isinstance(exc, OpenHomeSOAPError) and exc.request_envelope:
                detail = (
                    f"\n  request: {exc.request_envelope}"
                    f"\n  response: {exc.response_body}"
                )
            print(
                f"[upnp] track change notification failed: {exc!r}{detail}",
                flush=True,
            )
            raise

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
        with session.passthrough_lock:
            encoder = session.passthrough_encoder
        if encoder is not None:
            encoder.signal_source_done()

    def stop_passthrough(self) -> None:
        """Stop passthrough and revert to PCM re-encode mode."""
        with self._session_lock:
            session = self._session
        if session is None:
            return
        with session.passthrough_lock:
            encoder = session.passthrough_encoder
            session.passthrough_encoder = None
            session.passthrough_active = False
            session._passthrough_source_urls = None
            session._passthrough_done_event = None
            next_source = session.next_track_source
            session.next_track_source = None
            session.next_source_urls = None
            session.renderer_next_uri = None
            # Bump the generation so the watchdog for the track that was
            # playing exits on its next tick.
            session.renderer_watch_gen += 1
            session.renderer_ended_fired = False
        self._set_renderer_clock_active(False)
        if encoder is not None:
            try:
                encoder.close()
            except Exception:
                pass
        # Clear any bounded per-track file so subsequent requests fall
        # back to the live RingBuffer (PCM re-encode) path, and delete
        # its temp file.
        http_server = getattr(session, "http_server", None)
        if http_server is not None:
            http_server.next_track_source = None
        if next_source is not None:
            try:
                next_source.close()
            except Exception:
                pass
        track_source = getattr(http_server, "track_source", None) if http_server else None
        if track_source is not None:
            http_server.track_source = None
            try:
                track_source.close()
            except Exception:
                pass
        print("[upnp] passthrough OFF", flush=True)

    # ---- session lifecycle -----------------------------------------

    def connect(self, device_id: str, gapless: bool = False) -> UpnpDevice:
        """Open a session against the given device. Tears down any
        existing session first. Returns the connected UpnpDevice on
        success; raises ValueError / RuntimeError on failure
        (unknown device, descriptor fetch failure, AVTransport
        rejection, HTTP server bind failure).

        `gapless` opts this session into the bounded per-track path with
        SetNextAVTransportURI pre-staging: the renderer is handed one
        track at a time and advances at the natural boundary via a
        pre-staged next URI. Defaults to off — the plain per-track path
        with a second SetAVTransportURI at the boundary (a small gap) is
        the conservative mode for renderers that don't answer position
        probes.

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

        # DLNA-native gapless hinges on the renderer advertising
        # SetNextAVTransportURI. Probe once per connect so the answer is
        # visible in the console: without it, the only advance mechanism
        # is a second SetAVTransportURI (gapped).
        try:
            print(
                "[upnp] renderer advertises SetNextAVTransportURI: "
                f"{av.supports_next_uri()}",
                flush=True,
            )
        except Exception as exc:
            print(f"[upnp] SCPD probe failed: {exc!r}", flush=True)

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
            gapless=gapless,
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
        # A source-provider failure only costs us the header pre-roll;
        # fall back to the placeholder SetAVTransportURI below rather
        # than failing the connect over it.
        urls = None
        if self._source_provider is not None:
            try:
                _u = self._source_provider()
                if _u and isinstance(_u, list):
                    urls = _u
            except (ValueError, RuntimeError, OSError) as exc:
                log.debug("upnp: source provider raised: %r", exc)

        try:
            if urls is not None:
                # Passthrough path: the initial SetAVTransportURI + Play
                # happens inside start_passthrough -> _notify_track_change,
                # which raises on failure. A rejected URI (e.g. a Rygel
                # renderer returning UPnP 716) therefore propagates here
                # and tears down, instead of a false "connected" with no
                # audio.
                self.start_passthrough(
                    urls, wait_for_ready=True, start_s=self._resume_offset_s()
                )
            else:
                av.set_av_transport_uri(session.stream_url, didl)
                av.play()
                session.media_loaded = True
        except Exception as exc:
            # Full teardown: disconnect() stops the passthrough encoder
            # start_passthrough may have started, closes the buffer and
            # HTTP server, Stops the device, and un-silences local
            # audio. Then surface the failure so the UI shows an error
            # rather than a dead "connected" session.
            self.disconnect()
            raise RuntimeError(
                f"AVTransport handshake to {device.name} failed: {exc}"
            ) from exc

        # The user may have switched output mid-track, in which case the
        # next track was already preloaded before this session existed
        # (and the frontend's preload memo will not re-fire for it). Stage
        # it on the renderer now (SetNextAVTransportURI) when gapless is
        # on: without a `next`, the renderer finishes the current track
        # and stops (the first boundary after a mid-track connect).
        if (
            urls is not None
            and gapless
            and self._next_source_provider is not None
        ):
            try:
                preloaded_next = self._next_source_provider()
            except Exception as exc:
                preloaded_next = None
                log.debug("upnp: next-source provider raised: %r", exc)
            if preloaded_next:
                next_urls, next_meta = preloaded_next
                try:
                    self.set_next_track(next_urls, metadata=next_meta)
                except Exception as exc:
                    print(
                        f"[upnp] connect: stage preloaded next failed: {exc!r}",
                        flush=True,
                    )

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

        # Stop passthrough encoder first. The session is already
        # detached (self._session = None above), so no new push_pcm /
        # start_passthrough can reach it; still take passthrough_lock to
        # settle with any call already in flight, and close() the
        # detached encoder outside the lock.
        with session.passthrough_lock:
            pt_encoder = session.passthrough_encoder
            session.passthrough_encoder = None
            session.passthrough_active = False
            session._passthrough_source_urls = None
            session._passthrough_done_event = None
            next_source = session.next_track_source
            session.next_track_source = None
            session.next_source_urls = None
            session.renderer_next_uri = None
            # Stop the renderer-clock watchdog: the session is detached,
            # so its polling loop exits on the next tick.
            session.renderer_watch_gen += 1
            session.renderer_ended_fired = False
        self._set_renderer_clock_active(False)
        if pt_encoder is not None:
            try:
                pt_encoder.close()
            except Exception as exc:
                print(
                    f"[upnp] passthrough: encoder close error: {exc!r}",
                    flush=True,
                )
        if next_source is not None:
            try:
                next_source.close()
            except Exception as exc:
                log.debug("next-track source close failed: %r", exc)
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
        try:
            ts = getattr(session.http_server, "track_source", None)
            if ts is not None:
                ts.close()
            nxt = getattr(session.http_server, "next_track_source", None)
            if nxt is not None:
                session.http_server.next_track_source = None
                if nxt is not next_source:
                    nxt.close()
        except Exception as exc:
            log.debug("track-source close failed: %r", exc)
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
        # Decide whether passthrough owns the buffer right now, atomically
        # against start_passthrough. Without the lock this cleanup could
        # clobber a just-started next track's stream.
        with session.passthrough_lock:
            if session.passthrough_active:
                done = session._passthrough_done_event
                if done is not None and done.is_set():
                    session.passthrough_active = False
                    session._passthrough_done_event = None
                    session._passthrough_source_urls = None
                    session.passthrough_encoder = None
                    session.buffer.source_done()
                    return
                return
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
