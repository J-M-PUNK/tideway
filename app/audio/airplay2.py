"""Native AirPlay 2 audio sender.

Streams the player's live PCM to an AirPlay 2 receiver (modern
smart TV, HomePod, AirPlay 2 speaker) that exposes only the
`_airplay._tcp` service and no legacy RAOP. This is the path RAOP
could not reach. See docs/airplay2-sender.md for the full scope,
the staged plan, and the protocol references.

Status: SCAFFOLD. Stage 0 only. is_available() returns False and
connect() raises until the protocol stages land. Nothing imports
this into the player or server yet; wiring happens in Stage 6 so a
half-built sender can never affect local playback.

Architecture mirrors the proven-safe shape from the shelved RAOP
manager: a single asyncio loop on a daemon thread, a realtime-safe
push_pcm that never blocks or raises on the audio callback, and
high-signal diagnostics that reach both the dev console and
audio.log so a hardware test produces actionable signal.

Pipeline once complete:

    player float32 PCM
      -> push_pcm (realtime-safe handoff)
      -> ALAC encoder thread                      [Stage 5]
      -> AirPlay 2 audio packetizer + per-packet encryption [Stage 5]
      -> UDP/TCP audio channel to the receiver    [Stage 3/5]

    control:  encrypted RTSP (ANNOUNCE/SETUP/RECORD/...) [Stage 3]
    auth:     HomeKit transient pair-setup + pair-verify  [Stage 2]
              (reuses pyatv.auth.hap_*; see the doc)
    timing:   NTP buffered-mode anchor, PTP if required   [Stage 4]
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import traceback
from dataclasses import dataclass
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# audio.log is bound to the "tideway.audio" logger in player.py.
# Reuse it so AirPlay 2's high-signal lines survive in the rotating
# file for post-hoc reports, and print() so they show in the dev
# console (the logging module isn't wired to stdout in dev). Same
# lesson the RAOP work learned the hard way: an unobservable
# protocol path is impossible to iterate on.
_audio_log = logging.getLogger("tideway.audio")


def _say(msg: str, *, exc: bool = False) -> None:
    line = f"[airplay2] {msg}"
    if exc:
        line += "\n" + traceback.format_exc()
    print(line, flush=True)
    try:
        _audio_log.info(line)
    except Exception:
        # A logging-to-file hiccup must never propagate into the
        # sender path. The print already carried the signal.
        pass


# pyatv is used for discovery only (mDNS scan + parsed TXT
# records). The session protocol is hand-rolled on top. cryptography
# + srptools cover the crypto; pyatv.auth.hap_* covers pairing.
# Missing any of these -> is_available() is False, no crash.
try:  # pragma: no cover - environment dependent
    import pyatv  # type: ignore
    from pyatv.const import Protocol  # type: ignore

    _DEPS_ERROR: Optional[str] = None
except Exception as exc:  # pragma: no cover
    pyatv = None  # type: ignore
    Protocol = None  # type: ignore
    _DEPS_ERROR = f"pyatv import failed: {exc!r}"


def _crypto_ready() -> Optional[str]:
    """None if every crypto/auth dependency is importable, else a
    human-readable reason string."""
    try:
        import cryptography  # noqa: F401
        import srptools  # noqa: F401
        import pyatv.auth.hap_pairing  # noqa: F401
        import pyatv.auth.hap_session  # noqa: F401
        import pyatv.auth.hap_srp  # noqa: F401
    except Exception as exc:  # pragma: no cover
        return f"crypto/auth deps missing: {exc!r}"
    return None


# Flip to True only when Stages 2-5 are real. Keeps the manager
# honest: it advertises unavailable while it cannot actually
# stream, so nothing wires a non-working output into the picker.
_IMPLEMENTED = False


@dataclass
class AirPlay2Device:
    """An AirPlay 2 candidate from discovery (Stage 1 fills in the
    decoded TXT features/flags)."""

    id: str
    name: str
    address: str
    port: int
    # Stage 1: decoded `features`/`flags` bitfields, model, whether
    # the receiver wants transient pairing, supported audio formats.


@dataclass
class _Session:
    device_id: str
    sample_rate: int
    channels: int
    # pcm_queue: realtime-safe handoff from push_pcm to the ALAC
    # encoder thread (Stage 5). Bounded; full -> drop, never block.
    pcm_queue: "queue.Queue[Optional[bytes]]"
    encoder_thread: Optional[threading.Thread] = None
    # Stage 2-5 fill in: hap session keys, RTSP transport, audio
    # socket, timing peer, sequence/rtptime anchors.


class AirPlay2Manager:
    """Singleton-ish sender. Public surface intentionally matches
    the RAOP manager so Stage 6 can slot it into the Sound Output
    picker with the same connect/disconnect/push_pcm contract."""

    _instance: Optional["AirPlay2Manager"] = None

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._session: Optional[_Session] = None

    # -- lifecycle ---------------------------------------------------

    @classmethod
    def instance(cls) -> "AirPlay2Manager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def is_available() -> bool:
        """True only when the sender can actually stream. Stage 0
        scaffold: always False (deps may be present, but the
        protocol is not implemented), so nothing wires a dead
        output into the picker."""
        if not _IMPLEMENTED:
            return False
        return pyatv is not None and _crypto_ready() is None

    @staticmethod
    def unavailable_reason() -> Optional[str]:
        if not _IMPLEMENTED:
            return "AirPlay 2 sender not implemented yet (in development)"
        if pyatv is None:
            return _DEPS_ERROR
        return _crypto_ready()

    def _start_loop_thread(self) -> None:
        if self._thread is not None:
            return

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._loop_ready.set()
            loop.run_forever()

        self._thread = threading.Thread(
            target=_run, name="airplay2-loop", daemon=True
        )
        self._thread.start()
        self._loop_ready.wait(timeout=5.0)

    def _run_coro(self, coro, timeout: float = 30.0):
        self._start_loop_thread()
        assert self._loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # -- discovery (Stage 1) ----------------------------------------

    def discover(self, timeout: float = 5.0) -> list[AirPlay2Device]:
        """Scan for AirPlay 2 receivers. Stage 1 decodes the TXT
        features/flags to tell a streamable AirPlay 2 audio receiver
        from a video-only or RAOP-only device."""
        if pyatv is None:
            return []
        return self._run_coro(self._discover(timeout), timeout=timeout + 5)

    async def _discover(self, timeout: float) -> list[AirPlay2Device]:
        loop = asyncio.get_event_loop()
        results = await pyatv.scan(loop, timeout=timeout)
        out: list[AirPlay2Device] = []
        for conf in results:
            airplay_svc = None
            for svc in conf.services:
                if svc.protocol == Protocol.AirPlay:
                    airplay_svc = svc
                    break
            if airplay_svc is None:
                continue
            out.append(
                AirPlay2Device(
                    id=conf.identifier or str(conf.address),
                    name=conf.name,
                    address=str(conf.address),
                    port=airplay_svc.port,
                )
            )
            # Stage 1 TODO: decode airplay_svc.properties — the
            # `features`/`flags` bitfields, model (`am`), protocol
            # version (`vs`), supported encryption (`et`) — and
            # classify: streamable AirPlay 2 audio vs video-only vs
            # RAOP-only. Only the streamable ones should surface.
        return out

    # -- connect / stream (Stages 2-5) ------------------------------

    def connect(
        self, device_id: str, sample_rate: int, channels: int, dtype: str
    ) -> None:
        """Pair if needed, open the encrypted control session, set
        up the audio + timing channels, and start streaming the
        player's PCM. Not implemented past Stage 0."""
        raise NotImplementedError(
            "AirPlay 2 sender: connect() lands across Stages 2-5. "
            "See docs/airplay2-sender.md."
        )

    async def _pair(self, device: AirPlay2Device):
        # Stage 2: HomeKit transient pair-setup (PIN 3939) +
        # pair-verify via pyatv.auth.hap_*; derive the session keys.
        raise NotImplementedError("Stage 2: HomeKit pairing")

    async def _open_rtsp(self, device: AirPlay2Device):
        # Stage 3: encrypted RTSP control channel — ANNOUNCE, SETUP
        # (buffered audio), SETPEERS, SETRATEANCHORTIME, RECORD.
        raise NotImplementedError("Stage 3: encrypted RTSP control")

    async def _start_timing(self, session: _Session):
        # Stage 4: NTP buffered-mode anchor; PTP if the receiver
        # demands it.
        raise NotImplementedError("Stage 4: timing channel")

    def _encoder_worker(self, session: _Session) -> None:
        # Stage 5: drain pcm_queue, ALAC-encode (PyAV), packetize
        # per AirPlay 2, per-packet encrypt, push on the audio
        # channel at the anchored rate. float32 -> int16/ALAC
        # conversion lives here (the RAOP path proved the player
        # emits float32 and the encoder is integer-only).
        raise NotImplementedError("Stage 5: ALAC audio path")

    # -- realtime-safe PCM tap --------------------------------------

    def push_pcm(self, pcm: np.ndarray) -> None:
        """Called from the audio callback. Must never block, never
        raise, never allocate unboundedly. Identical contract to the
        RAOP tap: a brief lock, a non-blocking enqueue, drop on
        full. A dead/slow sender must not stutter local playback."""
        sess = self._session
        if sess is None:
            return
        try:
            sess.pcm_queue.put_nowait(bytes(pcm))
        except queue.Full:
            # Receiver/encoder fell behind. Dropping AirPlay frames
            # is correct here; local audio continuity wins.
            pass
        except Exception:
            # Absolutely nothing from the sender path is allowed to
            # surface on the audio thread.
            pass

    # -- teardown ----------------------------------------------------

    def disconnect(self) -> None:
        with self._lock:
            sess = self._session
            self._session = None
        if sess is None:
            return
        # Stage 6: signal the encoder, tear down audio/timing/RTSP,
        # close the hap session, in an order that doesn't race.
        if sess.encoder_thread is not None:
            sess.pcm_queue.put(None)


def manager() -> AirPlay2Manager:
    return AirPlay2Manager.instance()
