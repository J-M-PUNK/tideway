"""AirPlay output support.

This module adds a second audio sink alongside the existing
sounddevice output. When AirPlay is active, every PCM chunk the
PCMPlayer produces is also encoded to FLAC on the fly and streamed
to a paired AirPlay receiver via `pyatv`. The local sounddevice
output can either keep playing in parallel or be muted with the
normal volume slider; that's a UX decision the settings page
owns, not this module.

Data flow when AirPlay is active:

    Tidal DASH / local file
            |
            v
         Decoder
            |
            v    (PCM chunks)
       PCMPlayer  -----> sounddevice (muted or not, user's choice)
            |
            |  (tap hook, same PCM)
            v
       AirPlayManager._on_pcm()
            |
            v
       FlacStreamEncoder  (PyAV, format=flac, non-seekable)
            |
            v    (encoded bytes)
       RingBuffer
            |
            v    (served by FastAPI /api/airplay/stream over HTTP)
            |
            v
        pyatv.stream.stream_file(localhost URL)
            |
            v
        AirPlay receiver

Pairing is a separate, interactive flow. Modern receivers need a
one-time HomeKit-style pair handshake before they accept streams.
The `begin_pairing()` / `submit_pin()` / `finish_pairing()` methods
here expose that flow to the frontend as three backend calls. On
success the resulting credential string goes into
`airplay_credentials.json` keyed by device id, and future
connections reuse it without re-pairing.

None of this has been end-to-end tested against real hardware
yet. The code was written to match pyatv's public API, but you
will almost certainly find sharp edges the first time it talks
to a real HomePod / AirPort Express / AirPlay speaker. Logging
is verbose on purpose so the first test session surfaces what's
actually happening at every boundary.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from app.audio.http_stream import (
    FlacStreamEncoder,
    RingBuffer,
    StreamHTTPServer,
    primary_lan_ip,
    start_stream_http_server,
)
from app.paths import user_data_dir

log = logging.getLogger(__name__)

# pyatv is optional at import time. The rest of the app must boot
# fine on machines where pyatv either failed to install or has a
# runtime crash at import. AirPlay just becomes unavailable in that
# case, signalled via `is_available()`.
try:  # pragma: no cover - environment dependent
    import pyatv  # type: ignore
    from pyatv.const import Protocol  # type: ignore

    _IMPORT_ERROR: Optional[str] = None
except Exception as exc:  # pragma: no cover
    pyatv = None  # type: ignore
    Protocol = None  # type: ignore
    _IMPORT_ERROR = str(exc)


CREDENTIALS_FILE = user_data_dir() / "airplay_credentials.json"


@dataclass
class AirPlayDevice:
    """Minimal public representation of a discovered receiver."""
    id: str
    name: str
    address: str
    has_raop: bool
    paired: bool


@dataclass
class _ConnectedSession:
    """Internal: holds the pyatv handle and the PCM streaming pipe
    for an active AirPlay connection.

    `pcm_queue` accumulates raw int16 or float32 PCM chunks from the
    PCMPlayer's tap. The encoder loop (running on the AirPlay
    asyncio thread) drains it, converts to FLAC frames, and writes
    them into `flac_buffer`. A tiny HTTP server bound on
    `http_port` pulls from `flac_buffer` and hands bytes to pyatv.
    """
    device_id: str
    atv: object  # pyatv's ATV handle
    sample_rate: int
    channels: int
    pcm_queue: "queue.Queue[bytes]"
    flac_buffer: "RingBuffer"
    http_server: Optional["StreamHTTPServer"] = None
    http_port: int = 0
    encoder_thread: Optional[threading.Thread] = None



class AirPlayManager:
    """Singleton manager for AirPlay discovery, pairing, and
    streaming. Runs a dedicated asyncio loop on a background thread
    because pyatv is async-native and the rest of the player engine
    is sync."""

    _instance: Optional["AirPlayManager"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._lock = threading.RLock()

        self._discovered: dict[str, AirPlayDevice] = {}
        self._pending_pair: Optional[dict] = None  # {id, pairing}
        self._session: Optional[_ConnectedSession] = None

        self._start_loop_thread()

    @classmethod
    def instance(cls) -> "AirPlayManager":
        # Double-checked locking. The fast path (instance already
        # built) is a lock-free read. First-build contention is
        # serialized so concurrent callers can't both spawn their
        # own asyncio threads.
        if cls._instance is not None:
            return cls._instance
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @staticmethod
    def is_available() -> bool:
        return pyatv is not None

    @staticmethod
    def import_error() -> Optional[str]:
        return _IMPORT_ERROR

    # ------------------------------------------------------------------
    # Loop thread plumbing
    # ------------------------------------------------------------------

    def _start_loop_thread(self) -> None:
        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._loop_ready.set()
            try:
                loop.run_forever()
            finally:
                loop.close()

        self._loop_thread = threading.Thread(
            target=_run, name="airplay-asyncio", daemon=True
        )
        self._loop_thread.start()
        self._loop_ready.wait(timeout=5.0)

    def _run_coro(self, coro, timeout: float = 30.0):
        """Run an async coroutine on the AirPlay thread and wait for
        the result from the caller's (sync) thread."""
        if self._loop is None:
            raise RuntimeError("AirPlay loop not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # ------------------------------------------------------------------
    # Credentials persistence
    # ------------------------------------------------------------------

    def _load_credentials(self) -> dict:
        try:
            if CREDENTIALS_FILE.is_file():
                return json.loads(CREDENTIALS_FILE.read_text())
        except Exception as exc:
            log.warning("airplay credentials read failed: %s", exc)
        return {}

    def _save_credentials(self, store: dict) -> None:
        try:
            CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
            CREDENTIALS_FILE.write_text(json.dumps(store, indent=2))
            # Docstring promises user-only perms (0600). Apply them
            # explicitly; default umask on most shells leaves the
            # file world-readable otherwise. Best-effort on Windows
            # where chmod is a no-op.
            try:
                import stat

                os.chmod(
                    CREDENTIALS_FILE, stat.S_IRUSR | stat.S_IWUSR
                )
            except OSError:
                pass
        except Exception as exc:
            log.warning("airplay credentials write failed: %s", exc)

    def paired_device_ids(self) -> set[str]:
        return set(self._load_credentials().keys())

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self, timeout: float = 5.0) -> list[AirPlayDevice]:
        if not self.is_available():
            return []
        devices = self._run_coro(self._discover(timeout), timeout=timeout + 5)
        with self._lock:
            self._discovered = {d.id: d for d in devices}
        return devices

    async def _discover(self, timeout: float) -> list[AirPlayDevice]:
        loop = asyncio.get_event_loop()
        results = await pyatv.scan(loop, timeout=timeout)
        paired = self.paired_device_ids()
        out: list[AirPlayDevice] = []
        for conf in results or []:
            protocols = {svc.protocol.name for svc in conf.services}
            has_raop = "RAOP" in protocols
            out.append(
                AirPlayDevice(
                    id=conf.identifier,
                    name=conf.name,
                    address=str(conf.address),
                    has_raop=has_raop,
                    paired=conf.identifier in paired,
                )
            )
        return out

    async def _find_conf(self, device_id: str):
        """Re-scan and return the pyatv conf for a device by id.
        Scanning on every call is wasteful but correct. Caching the
        conf is risky because the receiver's IP can change."""
        loop = asyncio.get_event_loop()
        results = await pyatv.scan(loop, timeout=3.0)
        for conf in results or []:
            if conf.identifier == device_id:
                return conf
        return None

    # ------------------------------------------------------------------
    # Pairing
    # ------------------------------------------------------------------

    def begin_pairing(self, device_id: str) -> None:
        if not self.is_available():
            raise RuntimeError("pyatv not available")
        self._run_coro(self._begin_pairing(device_id))

    async def _begin_pairing(self, device_id: str) -> None:
        conf = await self._find_conf(device_id)
        if conf is None:
            raise RuntimeError(f"device {device_id} not found")
        loop = asyncio.get_event_loop()
        pairing = await pyatv.pair(conf, Protocol.RAOP, loop)
        await pairing.begin()
        with self._lock:
            self._pending_pair = {
                "device_id": device_id,
                "pairing": pairing,
                "name": conf.name,
            }

    def submit_pin(self, pin: str) -> None:
        with self._lock:
            pending = self._pending_pair
        if pending is None:
            raise RuntimeError("no pending pairing")
        self._run_coro(self._submit_pin(pin))

    async def _submit_pin(self, pin: str) -> None:
        with self._lock:
            pending = self._pending_pair
        if pending is None:
            return
        pairing = pending["pairing"]
        pairing.pin(pin)
        try:
            await pairing.finish()
        finally:
            try:
                await pairing.close()
            except Exception:
                pass
        if not pairing.has_paired:
            raise RuntimeError("pairing did not complete")
        creds = pairing.service.credentials
        if not creds:
            raise RuntimeError("pairing returned no credentials")
        store = self._load_credentials()
        store[pending["device_id"]] = {
            "name": pending.get("name") or "",
            "credentials": creds,
        }
        self._save_credentials(store)
        with self._lock:
            self._pending_pair = None

    def cancel_pairing(self) -> None:
        with self._lock:
            pending = self._pending_pair
            self._pending_pair = None
        if pending is None:
            return
        try:
            self._run_coro(self._cancel_pairing(pending["pairing"]), timeout=5.0)
        except Exception:
            pass

    async def _cancel_pairing(self, pairing) -> None:
        try:
            await pairing.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Connect + stream
    # ------------------------------------------------------------------

    def connect(self, device_id: str, sample_rate: int, channels: int,
                dtype: str) -> None:
        """Connect to a paired device and prepare the streaming
        pipe. Raises if the device has no saved credentials or if
        pyatv rejects the connection."""
        if not self.is_available():
            raise RuntimeError("pyatv not available")
        self.disconnect()
        self._run_coro(self._connect(device_id, sample_rate, channels, dtype))

    async def _connect(self, device_id: str, sample_rate: int,
                       channels: int, dtype: str) -> None:
        store = self._load_credentials()
        entry = store.get(device_id)
        if not entry:
            raise RuntimeError(
                f"no saved credentials for {device_id}; pair first"
            )
        creds = entry.get("credentials")
        if not creds:
            raise RuntimeError(f"credentials for {device_id} are empty")
        conf = await self._find_conf(device_id)
        if conf is None:
            raise RuntimeError(f"device {device_id} not found on network")
        conf.set_credentials(Protocol.RAOP, creds)
        loop = asyncio.get_event_loop()
        atv = await pyatv.connect(conf, loop)

        session = _ConnectedSession(
            device_id=device_id,
            atv=atv,
            sample_rate=sample_rate,
            channels=channels,
            pcm_queue=queue.Queue(maxsize=64),
            flac_buffer=RingBuffer(),
        )

        # Stand up a tiny LAN-reachable HTTP server just for this
        # session's stream. See StreamHTTPServer in http_stream.py
        # for why this is separate from FastAPI.
        http_server = start_stream_http_server(session.flac_buffer)
        session.http_server = http_server
        session.http_port = http_server.server_address[1]
        log.info("airplay http server bound on port %s", session.http_port)

        # Encoder runs on a dedicated thread, not on the asyncio
        # loop, because queue.Queue.get is blocking and would freeze
        # the loop during the wait. Thread lives until the sentinel
        # None lands on pcm_queue during disconnect.
        session.encoder_thread = threading.Thread(
            target=self._encoder_worker,
            args=(session, dtype),
            name=f"airplay-encoder-{device_id}",
            daemon=True,
        )
        session.encoder_thread.start()

        with self._lock:
            self._session = session

        # Hand pyatv a LAN URL pointing at our dedicated HTTP
        # server. `_drive_stream` awaits the long-running transfer.
        loop.create_task(self._drive_stream(atv, session.http_port))

    async def _drive_stream(self, atv, http_port: int) -> None:
        """Call pyatv's stream_file against our dedicated stream
        HTTP server. Runs for the duration of the connection."""
        try:
            # The URL has to be routable from the AirPlay receiver.
            # For a home network that means the host's LAN IP rather
            # than 127.0.0.1. We pick the first non-loopback IPv4
            # address; good enough for the common "both devices on
            # the same wifi" case. A future refinement is to let the
            # user pick the interface or advertise via mDNS.
            url = f"http://{primary_lan_ip()}:{http_port}/stream"
            log.info("airplay: opening stream against %s", url)
            await atv.stream.stream_file(url)
            log.info("airplay: stream_file returned")
        except Exception as exc:
            log.exception("airplay: stream_file failed: %s", exc)

    def _encoder_worker(self, session: _ConnectedSession, dtype: str) -> None:
        """Drains pcm_queue and writes FLAC bytes into flac_buffer.
        Runs on its own thread so the blocking queue.get doesn't
        stall the asyncio loop that pyatv is using."""
        try:
            encoder = FlacStreamEncoder(
                sample_rate=session.sample_rate,
                channels=session.channels,
                dtype=dtype,
            )
        except Exception as exc:
            log.exception("airplay: encoder init failed: %s", exc)
            session.flac_buffer.close()
            return
        try:
            while True:
                try:
                    raw = session.pcm_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if raw is None:
                    break
                # raw is a (frames, channels) interleaved int16 /
                # int32 / float32 buffer serialized as bytes.
                try:
                    arr = np.frombuffer(raw, dtype=encoder.np_dtype)
                    if session.channels > 0:
                        arr = arr.reshape(-1, session.channels)
                    encoded = encoder.encode(arr)
                    if encoded:
                        session.flac_buffer.write(encoded)
                except Exception as exc:
                    log.warning("airplay: encode chunk failed: %s", exc)
                    # Keep the loop alive; one bad chunk should not
                    # terminate the whole stream.
            try:
                tail = encoder.close()
                if tail:
                    session.flac_buffer.write(tail)
            except Exception:
                pass
        finally:
            session.flac_buffer.close()

    def disconnect(self) -> None:
        with self._lock:
            sess = self._session
            self._session = None
        if sess is None:
            return
        try:
            self._run_coro(self._disconnect(sess), timeout=5.0)
        except Exception as exc:
            log.warning("airplay disconnect hit: %s", exc)

    async def _disconnect(self, sess: _ConnectedSession) -> None:
        # Order matters: signal the encoder first and let it exit
        # cleanly before we close the buffer or the HTTP server.
        # Otherwise the encoder can raise mid-write against a closed
        # buffer, or the HTTP thread tears down the socket while
        # pyatv's stream task is still trying to read.
        try:
            sess.pcm_queue.put_nowait(None)
        except Exception:
            pass
        if sess.encoder_thread is not None:
            try:
                sess.encoder_thread.join(timeout=2.0)
            except Exception:
                pass
        try:
            sess.atv.close()  # type: ignore[attr-defined]
        except Exception:
            pass
        sess.flac_buffer.close()
        if sess.http_server is not None:
            try:
                sess.http_server.shutdown()
                sess.http_server.server_close()
            except Exception:
                pass

    def is_connected(self) -> bool:
        with self._lock:
            return self._session is not None

    def current_device_id(self) -> Optional[str]:
        with self._lock:
            return self._session.device_id if self._session else None

    # ------------------------------------------------------------------
    # PCM tap entrypoint
    # ------------------------------------------------------------------

    def push_pcm(self, pcm: np.ndarray) -> None:
        """Called from the PCMPlayer audio callback on every chunk.
        Copies into the active session's queue if there is one.
        No-op when AirPlay is not connected."""
        with self._lock:
            sess = self._session
        if sess is None:
            return
        # The audio callback runs on a realtime thread; we cannot
        # block it. Use put_nowait and drop if the encoder can't
        # keep up. A drop here would be audible on the AirPlay side
        # but local playback keeps going. Shouldn't happen under
        # normal load; the encoder is CPU-light.
        try:
            sess.pcm_queue.put_nowait(bytes(pcm))
        except queue.Full:
            pass

