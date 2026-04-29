"""Chromecast sender.

Discovers Cast devices on the LAN and routes Tideway's audio to a
selected device by handing the Cast Default Media Receiver an HTTP
URL serving Tideway's encoded stream.

The module owns three responsibilities behind a process-wide
singleton (`cast_manager`):

  Discovery — keep an up-to-date list of Cast devices on the LAN.
              pychromecast's `CastBrowser` does the mDNS work; we
              translate its callbacks into a thread-safe dict.

  Session   — a single `CastSession` represents the live connection
              to one chosen device. Owns the FLAC encoder, the
              ring buffer, and the LAN-reachable HTTP server that
              the Cast device pulls audio from. Issues `play_media`
              against the URL once everything's wired.

  PCM tap   — `push_pcm()` is called from PCMPlayer's audio
              callback when a session is active. The PCM goes into
              the FLAC encoder which fills the ring buffer; the
              HTTP server hands those bytes to the Cast device.

The module degrades gracefully when pychromecast is missing — if
the wheel doesn't import, the manager constructs but every public
method is a no-op. The picker just shows empty.

There is exactly one Cast session at a time. Switching to a new
device tears down the old session before connecting the new one.
Cast doesn't have a multi-room concept the way Spotify Connect
does, so a single-session model maps cleanly to what users expect.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from app.audio.http_stream import (
    FlacStreamEncoder,
    RingBuffer,
    StreamHTTPServer,
    primary_lan_ip,
    start_stream_http_server,
)

log = logging.getLogger(__name__)

# pychromecast is an optional dep — if the wheel is unavailable
# the rest of the audio stack still works. The Cast picker just
# shows as empty in the UI.
try:
    import pychromecast
    from pychromecast import Chromecast
    from pychromecast.discovery import CastBrowser, SimpleCastListener
    import zeroconf

    _CAST_AVAILABLE = True
except Exception as _exc:  # pragma: no cover - environment dependent
    log.warning("pychromecast unavailable: %s", _exc)
    pychromecast = None  # type: ignore
    Chromecast = None  # type: ignore
    CastBrowser = None  # type: ignore
    SimpleCastListener = None  # type: ignore
    zeroconf = None  # type: ignore
    _CAST_AVAILABLE = False


# ---------------------------------------------------------------------
# Device record
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class CastDevice:
    """Concrete Cast target the user can pick from the now-playing
    devices menu.

    `id` is the Cast UUID — stable across reboots, comes from the
    device firmware. We use it as the persistence key for "remember
    last device." `friendly_name` is what the user sees ("Living
    Room speaker", "Kitchen Nest Mini"). The rest is metadata that
    helps disambiguate (two Nest Minis next to each other) and is
    logged for support purposes.
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


# ---------------------------------------------------------------------
# CastSession — the live connection to one device
# ---------------------------------------------------------------------

# Configuration the session passes through to the FLAC encoder. PCMPlayer
# emits audio at the source's native rate / dtype; the encoder receives
# whatever shape arrives. The Cast Default Media Receiver supports FLAC
# at 44.1k / 48k natively and most receivers up-convert hi-res down on
# their own DAC, so we encode at the source rate and let the device
# decide its ceiling.
@dataclass
class _SessionState:
    device: CastDevice
    cast: object  # pychromecast.Chromecast, kept opaque so type-check
                  # doesn't complain when the optional dep is missing
    buffer: RingBuffer = field(default_factory=RingBuffer)
    http_server: Optional[StreamHTTPServer] = None
    encoder: Optional[FlacStreamEncoder] = None
    encoder_lock: threading.Lock = field(default_factory=threading.Lock)
    # Last sample-rate / channel / dtype we built the encoder for.
    # When PCMPlayer's source rate changes (track change, resample
    # mode flip), the encoder has to be rebuilt to match the new
    # input. We compare on every push and rebuild when these drift.
    encoder_rate: int = 0
    encoder_channels: int = 0
    encoder_dtype: str = ""
    # Bytes of FLAC successfully fed to the buffer since the session
    # opened. Surfaces in the diagnostic endpoint so a stalled
    # session shows up as "0 bytes encoded" instead of looking
    # silently broken.
    bytes_encoded: int = 0
    # Set once `play_media` has been issued. Before that we're
    # encoding into the buffer but the receiver hasn't been told
    # to fetch it yet — useful state to surface for diagnostics.
    media_loaded: bool = False
    # The media stream URL we handed the device. Logged for support.
    stream_url: str = ""
    # Latest receiver-side MediaStatus. Populated by the status
    # listener registered in connect(). Critical for diagnosing
    # "media_loaded but the TV's not playing" — values like
    # ("IDLE", "ERROR") tell us the receiver tried our URL and
    # rejected it (typically a format-support gap on vendor
    # receivers like Hisense / TCL). All Optional so the snapshot
    # still works before the first status arrives.
    receiver_state: Optional[str] = None
    receiver_idle_reason: Optional[str] = None
    receiver_status_at: float = 0.0


# ---------------------------------------------------------------------
# CastManager — process-wide owner
# ---------------------------------------------------------------------

class CastManager:
    """Process-wide owner of Cast discovery + the active session.

    Construct once at server boot. Discovery starts on
    `start_discovery()` and runs continuously. `connect(device_id)`
    opens a session against a discovered device. `push_pcm()` feeds
    PCM samples into the active session's encoder; called from
    PCMPlayer's audio callback. `is_active()` is the cheap probe
    that callback uses to decide whether to even reach for the lock.

    There is at most one session. `connect()` to a different device
    tears the existing session down first.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: dict[str, CastDevice] = {}
        self._browser: Optional["CastBrowser"] = None
        self._zconf: Optional["zeroconf.Zeroconf"] = None
        self._last_event_at: float = 0.0

        # Active session and its protector. Held under a separate
        # lock from the discovery dict so a slow connect() doesn't
        # block list_devices() callers.
        self._session_lock = threading.Lock()
        self._session: Optional[_SessionState] = None

        # External "session changed" listeners. SSE bus subscribes
        # so the frontend's NowPlaying can show "casting to X" /
        # "stopped casting" without polling.
        self._listeners: list[Callable[[Optional[CastDevice]], None]] = []

        # Local-output silencer hook. Called with True when a
        # session opens, False when it closes. server.py wires this
        # to the global PCMPlayer's set_external_output_active at
        # startup. Held as a callback rather than a hard import to
        # avoid the circular import of cast.py <-> player.py.
        self._local_silencer: Optional[Callable[[bool], None]] = None

    # ---- discovery lifecycle ---------------------------------------

    def start_discovery(self) -> None:
        """Begin browsing for Cast devices on the LAN. Idempotent."""
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
                # through with no browser; list_devices() returns []
                # and the picker is empty, which is the right UX for
                # "no Cast devices reachable."
                print(f"[cast] discovery failed to start: {exc!r}",
                      flush=True)
                self._zconf = None
                self._browser = None

    def stop_discovery(self) -> None:
        """Tear down discovery and any active session. Called from
        the FastAPI shutdown hook."""
        # Disconnect first; the session uses zeroconf indirectly
        # via pychromecast's connection logic, and we want a clean
        # session shutdown before zeroconf tears down its sockets.
        try:
            self.disconnect()
        except Exception as exc:
            log.debug("cast disconnect during shutdown failed: %r", exc)
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

    # ---- discovery surface -----------------------------------------

    def list_devices(self) -> list[CastDevice]:
        """Snapshot of currently-known devices, audio-only first."""
        with self._lock:
            devices = list(self._devices.values())
        devices.sort(
            key=lambda d: (0 if is_audio_only(d) else 1,
                           d.friendly_name.lower())
        )
        return devices

    def get_device(self, device_id: str) -> Optional[CastDevice]:
        with self._lock:
            return self._devices.get(device_id)

    def status(self) -> dict[str, object]:
        """Diagnostic snapshot. Surfaces discovery state and
        whether a session is open. Used by /api/cast/devices and
        by the dev console for debugging."""
        with self._lock:
            disc = {
                "available": _CAST_AVAILABLE,
                "running": self._browser is not None,
                "device_count": len(self._devices),
                "last_event_age_s": (
                    None if self._last_event_at == 0.0
                    else round(time.monotonic() - self._last_event_at, 1)
                ),
            }
        with self._session_lock:
            sess = self._session
            disc["connected_id"] = sess.device.id if sess else None
            disc["connected_name"] = sess.device.friendly_name if sess else None
            disc["bytes_encoded"] = sess.bytes_encoded if sess else 0
            disc["media_loaded"] = bool(sess and sess.media_loaded)
            disc["receiver_state"] = sess.receiver_state if sess else None
            disc["receiver_idle_reason"] = (
                sess.receiver_idle_reason if sess else None
            )
            disc["receiver_status_age_s"] = (
                round(time.monotonic() - sess.receiver_status_at, 1)
                if sess and sess.receiver_status_at > 0.0
                else None
            )
        # Pull cached state directly from pychromecast on top of the
        # listener-push values. Listener fires only on transitions, so
        # if the receiver settled into a state before we registered (or
        # never sends another update) the listener fields stay None
        # forever; the cached values give us the receiver's current
        # truth on every poll. Best-effort — pychromecast's accessors
        # can race with disconnect / reconnect cycles, and the whole
        # block is diagnostic.
        if sess is not None:
            try:
                cast_obj = sess.cast
                mc = getattr(cast_obj, "media_controller", None)
                ms = getattr(mc, "status", None) if mc is not None else None
                if ms is not None:
                    disc["mc_player_state"] = (
                        getattr(ms, "player_state", None) or None
                    )
                    disc["mc_idle_reason"] = (
                        getattr(ms, "idle_reason", None) or None
                    )
                    disc["mc_content_type"] = (
                        getattr(ms, "content_type", None) or None
                    )
                    disc["mc_content_id"] = (
                        getattr(ms, "content_id", None) or None
                    )
                cs = getattr(cast_obj, "status", None)
                if cs is not None:
                    disc["app_id"] = (
                        getattr(cs, "app_id", None) or None
                    )
                    disc["app_display_name"] = (
                        getattr(cs, "display_name", None) or None
                    )
                    disc["is_active_input"] = getattr(
                        cs, "is_active_input", None
                    )
                    disc["is_stand_by"] = getattr(cs, "is_stand_by", None)
            except Exception as exc:
                log.debug("cast: receiver-status pull failed: %r", exc)
        return disc

    # ---- session lifecycle -----------------------------------------

    def connect(self, device_id: str) -> CastDevice:
        """Open a session against the given device. Tears down any
        existing session first. Returns the connected CastDevice
        on success; raises ValueError / RuntimeError on failure
        (unknown device, connect timeout, play_media rejection).

        This blocks for the duration of the Cast handshake — the
        FastAPI handler that calls it returns to the user once the
        device is ready and the stream URL has been issued. Typical
        latency on the LAN is well under a second; we cap at 10s.
        """
        if not _CAST_AVAILABLE:
            raise RuntimeError("pychromecast not available")
        device = self.get_device(device_id)
        if device is None:
            raise ValueError(f"unknown cast device: {device_id}")

        # Tear down anything already open. Safe to call with no
        # session active (no-ops). Held outside the session lock
        # because disconnect() takes the same lock and we'd self-
        # deadlock.
        self.disconnect()

        # pychromecast's get_chromecast_from_cast_info is the modern
        # entry point — takes the CastInfo we already have from the
        # browser and skips re-discovery. Falls back to a UUID-based
        # lookup if the API shifted in a later version.
        cast_obj: object
        cast_info = None
        try:
            with self._lock:
                if self._browser is not None:
                    cast_info = self._browser.devices.get(device.id) or \
                                self._browser.devices.get(_uuid_or_str(device.id))
            if cast_info is None:
                raise RuntimeError(
                    f"cast info missing for {device.friendly_name}; "
                    "device may have just gone offline"
                )
            cast_obj = pychromecast.get_chromecast_from_cast_info(
                cast_info, self._zconf
            )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"failed to construct Chromecast for "
                f"{device.friendly_name}: {exc}"
            ) from exc

        # `wait` blocks until the device's status is read and the
        # default media controller is available. Without this, a
        # subsequent play_media races the controller's readiness
        # and intermittently drops the command.
        try:
            cast_obj.wait(timeout=10.0)
        except Exception as exc:
            try:
                cast_obj.disconnect()
            except Exception:
                pass
            raise RuntimeError(
                f"timed out waiting for {device.friendly_name} "
                f"to become ready: {exc}"
            ) from exc

        # Set up the streaming pipeline. Buffer + HTTP server must
        # exist before we issue play_media so the device's first
        # GET hits a serving listener.
        session = _SessionState(device=device, cast=cast_obj)
        try:
            session.http_server = start_stream_http_server(
                session.buffer,
                stream_path="/cast/stream",
                content_type="audio/flac",
            )
            host_port = session.http_server.server_address[1]
            session.stream_url = (
                f"http://{primary_lan_ip()}:{host_port}/cast/stream"
            )
        except Exception as exc:
            try:
                cast_obj.disconnect()
            except Exception:
                pass
            raise RuntimeError(
                f"failed to start http stream server: {exc}"
            ) from exc

        # Hand the URL to the device. The Default Media Receiver
        # is implicit; pychromecast launches it via play_media.
        # `metadata` lights up the device's now-playing display
        # (matters for Cast TVs / hubs with screens; ignored on
        # speakers).
        try:
            mc = cast_obj.media_controller
            mc.play_media(
                session.stream_url,
                "audio/flac",
                title="Tideway",
                stream_type="LIVE",
            )
            mc.block_until_active(timeout=10.0)
            session.media_loaded = True
            # Subscribe to MediaStatus so the receiver's player_state
            # / idle_reason transitions show up in /api/cast/devices
            # and on stdout. Without this we have no visibility into
            # why a receiver that claims "active" then doesn't play —
            # vendor Cast receivers (Hisense, TCL, Vizio) routinely
            # accept play_media, transition to IDLE/ERROR a second
            # later because they can't decode our format, and the
            # sender side never finds out.
            mc.register_status_listener(
                _MediaStatusListener(self, session)
            )
        except Exception as exc:
            try:
                if session.http_server is not None:
                    session.http_server.shutdown()
                    session.http_server.server_close()
            except Exception:
                pass
            try:
                cast_obj.disconnect()
            except Exception:
                pass
            raise RuntimeError(
                f"play_media to {device.friendly_name} failed: {exc}"
            ) from exc

        with self._session_lock:
            self._session = session

        # Mute local audio output. PCMPlayer's audio callback writes
        # silence while active; the PCM tap above STILL feeds the
        # Cast encoder (the tap happens before the silencer in the
        # callback ordering), so the device gets full audio while
        # local stays quiet. Best-effort — if no silencer was wired
        # we just skip rather than block the cast lifecycle.
        if self._local_silencer is not None:
            try:
                self._local_silencer(True)
            except Exception as exc:
                log.debug("cast: local silencer raised: %r", exc)

        print(f"[cast] connected: {device.friendly_name} "
              f"@ {device.host} streaming from {session.stream_url}",
              flush=True)
        self._notify_listeners(device)
        return device

    def disconnect(self) -> None:
        """Tear down any active session. Idempotent."""
        with self._session_lock:
            session = self._session
            self._session = None
        if session is None:
            return
        # Order matters here. Encoder first (drains pending FLAC
        # bytes), then buffer close (unblocks the HTTP serve loop's
        # read), then HTTP server shutdown (the serve loop notices
        # closed buffer and exits cleanly), then Cast disconnect.
        try:
            with session.encoder_lock:
                if session.encoder is not None:
                    try:
                        tail = session.encoder.close()
                        if tail:
                            session.buffer.write(tail)
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
            mc = getattr(session.cast, "media_controller", None)
            if mc is not None:
                try:
                    mc.stop()
                except Exception:
                    pass
            session.cast.disconnect()
        except Exception as exc:
            log.debug("cast disconnect failed: %r", exc)

        # Restore local audio output. The user's volume / mute
        # settings were preserved across the cast cycle so they
        # don't have to re-set them now. Best-effort.
        if self._local_silencer is not None:
            try:
                self._local_silencer(False)
            except Exception as exc:
                log.debug("cast: local silencer raised on close: %r", exc)

        print(f"[cast] disconnected: {session.device.friendly_name}",
              flush=True)
        self._notify_listeners(None)

    # ---- PCM tap (called from PCMPlayer's audio callback) -----------

    def is_active(self) -> bool:
        """Cheap probe used by PCMPlayer's audio callback to decide
        whether to reach for the session. Stays lock-free for the
        common case (no session) — the callback runs at sub-
        millisecond cadence and even a contended lock takes too
        long.

        Holds the GIL but doesn't acquire any explicit lock. The
        `_session is not None` read is a single Python pointer
        compare which is atomic. False positives (briefly seeing a
        session that's about to close) are caught by the actual
        lock+null check inside `push_pcm`."""
        return self._session is not None

    def push_pcm(self, pcm: np.ndarray, sample_rate: int, dtype: str) -> None:
        """Feed a chunk of PCM into the active session's encoder.

        `pcm` is a 2-D ndarray (frames, channels). `dtype` may be
        "int16", "int32", or "float32"; float32 is converted to int32
        here because FLAC is integer-only and Windows WASAPI shared
        mode delivers the audio callback's PCM as float32 (the
        device-mixer format). Without that conversion every Windows
        shared-mode user would silently get a zero-byte stream.
        `sample_rate` and `dtype` describe the current source so we
        can rebuild the encoder when the source changes (e.g., a
        track-change to a different rate). When no session is
        active the call is a quick no-op.

        Called from PCMPlayer's audio callback, which is the
        realtime thread, so it has to be cheap. The encode +
        ring-buffer write happens inline; profiling on a Mac mini
        shows ~1ms per 4096-frame chunk for stereo 24/96, well
        within budget.
        """
        if pcm.size == 0:
            return
        with self._session_lock:
            session = self._session
        if session is None:
            return
        if dtype == "float32":
            # WASAPI shared-mode samples are normalized floats in
            # [-1.0, 1.0]. Scale to int32 full range; clip to handle
            # intersample peaks > 1.0 that would otherwise wrap.
            # Math runs in float64 because int32-max (2147483647)
            # is not representable in float32 — clipping to it in
            # float32 actually rounds up to 2147483648 and overflows
            # to int32-min on the cast.
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
                # Rebuilding the encoder mid-stream produces a
                # discontinuity at the boundary; the Cast device
                # will glitch briefly. Rare in practice — happens
                # on track-change to a different sample rate, which
                # would discontinuity-glitch the local audio engine
                # too because PCMPlayer reopens its OutputStream
                # for cross-rate transitions.
                if session.encoder is not None:
                    try:
                        tail = session.encoder.close()
                        if tail:
                            session.buffer.write(tail)
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
                    print(f"[cast] encoder build failed: {exc!r}",
                          flush=True)
                    return
            # Ensure 2-D (frames, channels) for the encoder.
            if pcm.ndim == 1:
                pcm = pcm.reshape(-1, 1)
            try:
                encoded = session.encoder.encode(pcm)
            except Exception as exc:
                # Encoder errors mid-stream are a session-killer.
                # Rather than try to recover here on the realtime
                # thread, log and let the session run dry; the
                # frontend will see media_loaded but bytes_encoded
                # plateau, which is an obvious diagnostic signal.
                log.debug("flac encode failed: %r", exc)
                return
        if encoded:
            session.buffer.write(encoded)
            # Atomic int update; no lock needed for a counter.
            session.bytes_encoded += len(encoded)

    # ---- listener bus ----------------------------------------------

    def set_local_silencer(
        self, callback: Optional[Callable[[bool], None]]
    ) -> None:
        """Wire the audio engine's local-output silencer. The
        callback gets `True` whenever a Cast session opens and
        `False` when it closes, so the audio engine can mute the
        local sounddevice output while the Cast device is the
        active sink. Pass None to unwire (used in tests)."""
        self._local_silencer = callback

    def add_listener(
        self,
        callback: Callable[[Optional[CastDevice]], None],
    ) -> Callable[[], None]:
        """Subscribe to session-change events. Called whenever
        connect / disconnect runs to completion. Returns an
        unsubscribe callable. Used by server.py to push state
        changes onto the SSE bus."""
        with self._session_lock:
            self._listeners.append(callback)

        def _unsub() -> None:
            with self._session_lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)
        return _unsub

    def _notify_listeners(self, device: Optional[CastDevice]) -> None:
        with self._session_lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(device)
            except Exception as exc:
                log.debug("cast listener raised: %r", exc)

    # ---- pychromecast discovery callbacks ---------------------------

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
        # If the removed device is the one we're connected to, the
        # session's about to die. Disconnect proactively so we don't
        # keep encoding into a black hole. The device coming back
        # online (Wi-Fi blip) will surface in discovery again and
        # the user can reselect.
        with self._session_lock:
            sess = self._session
        if sess is not None and sess.device.id == sid:
            try:
                self.disconnect()
            except Exception:
                pass

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


class _MediaStatusListener:
    """pychromecast MediaStatus subscriber.

    pychromecast invokes `new_media_status(status)` on every receiver
    state transition. We capture player_state and idle_reason on the
    associated session so /api/cast/devices can surface them, and
    print transitions to stdout for live debugging. The receiver
    pushes status messages on its own protocol thread so we mutate
    via the manager's session lock to stay consistent with status()
    readers.

    Held for the lifetime of the session — pychromecast doesn't take
    a strong ref, so the listener has to outlive the
    register_status_listener call. CastSession holds the controller,
    which holds this listener via pychromecast's internal list."""

    def __init__(
        self, manager: "CastManager", session: "_SessionState"
    ) -> None:
        self._manager = manager
        self._session = session

    def new_media_status(self, status) -> None:  # pychromecast API
        player_state = getattr(status, "player_state", None) or None
        idle_reason = getattr(status, "idle_reason", None) or None
        prev_state = self._session.receiver_state
        prev_reason = self._session.receiver_idle_reason
        # Update under the session lock so /api/cast/devices reads
        # are consistent. We only ever mutate this session's fields,
        # so contention is negligible.
        with self._manager._session_lock:
            self._session.receiver_state = player_state
            self._session.receiver_idle_reason = idle_reason
            self._session.receiver_status_at = time.monotonic()
        # High-signal print for the dev console. The combination
        # (state, idle_reason) is what the receiver uses to mean
        # "couldn't play": e.g. ('IDLE', 'ERROR') after a play_media
        # is the format-rejection fingerprint we're chasing.
        if (player_state, idle_reason) != (prev_state, prev_reason):
            print(
                f"[cast] receiver status: "
                f"player_state={player_state!r} "
                f"idle_reason={idle_reason!r}",
                flush=True,
            )


def _uuid_or_str(s: str):
    """pychromecast's `browser.devices` dict is keyed by UUID
    objects. Our CastDevice carries the str representation. Try the
    str first (some versions accept it), then fall back to a UUID
    parse. Keeps us version-tolerant without binding hard to a
    specific pychromecast major."""
    try:
        import uuid as _uuid
        return _uuid.UUID(s)
    except Exception:
        return s


# Module-level singleton. server.py calls `cast_manager.start_discovery()`
# at boot and `cast_manager.stop_discovery()` on shutdown.
cast_manager = CastManager()
