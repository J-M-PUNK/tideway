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
import json
import logging
import os
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

# pyatv ships the canonical AirPlay feature-flag table and the
# pairing/password classification logic. Reuse it rather than
# hand-decoding a 64-bit bitfield (the "reuse pyatv" decision in
# docs/airplay2-sender.md). These are pyatv internals, so the
# import is guarded separately: a pyatv layout change degrades
# discovery to conservative "not streamable" rather than crashing
# the module. Verified against pyatv 0.17.0.
try:  # pragma: no cover - environment dependent
    from pyatv.protocols.airplay.utils import (  # type: ignore
        AirPlayFlags,
        get_pairing_requirement,
        is_password_required,
        parse_features,
    )

    _FEATURES_ERROR: Optional[str] = None
except Exception as exc:  # pragma: no cover
    AirPlayFlags = None  # type: ignore
    get_pairing_requirement = None  # type: ignore
    is_password_required = None  # type: ignore
    parse_features = None  # type: ignore
    _FEATURES_ERROR = f"pyatv airplay utils unavailable: {exc!r}"

# pyatv's HAP pair-setup / pair-verify and HTTP transport. The
# canonical recipe is pyatv.protocols.airplay.pairing; Stage 2
# follows it (http_connect -> pair_setup(HAP) -> start/finish ->
# str(HapCredentials)) and Stage 3 will pair_verify the stored
# credentials to derive the encrypted-channel keys. Guarded
# separately for the same reason as the feature table.
try:  # pragma: no cover - environment dependent
    from pyatv.auth.hap_pairing import (  # type: ignore
        NO_CREDENTIALS,
        TRANSIENT_CREDENTIALS,
        parse_credentials,
    )
    from pyatv.protocols.airplay.auth import (  # type: ignore
        AuthenticationType,
        pair_setup,
        verify_connection,
    )
    from pyatv.support.http import http_connect  # type: ignore

    _PAIR_ERROR: Optional[str] = None
except Exception as exc:  # pragma: no cover
    NO_CREDENTIALS = None  # type: ignore
    TRANSIENT_CREDENTIALS = None  # type: ignore
    parse_credentials = None  # type: ignore
    AuthenticationType = None  # type: ignore
    pair_setup = None  # type: ignore
    verify_connection = None  # type: ignore
    http_connect = None  # type: ignore
    _PAIR_ERROR = f"pyatv HAP pairing unavailable: {exc!r}"

# pyatv's RAOP module already implements a complete AirPlay 2
# buffered-audio sender (AirPlayV2: verify, base SETUP, audio
# SETUP, ChaCha20 per-packet encryption; StreamClient: NTP timing
# server + audio loop). pyatv only fails to reach our devices
# because its RAOP discovery/credential layer assumes a _raop._tcp
# service. We drive the streaming engine directly against the
# AirPlay service with the Stage 2 HAP credentials. See
# docs/airplay2-sender.md. Guarded separately; pyatv internals.
try:  # pragma: no cover - environment dependent
    from pyatv.protocols.raop.protocols import (  # type: ignore
        StreamContext,
    )
    from pyatv.protocols.raop.protocols.airplayv2 import (  # type: ignore
        EVENTS_READ_INFO,
        EVENTS_SALT,
        EVENTS_WRITE_INFO,
        AirPlayV2,
    )
    from pyatv.support.chacha20 import (  # type: ignore
        Chacha20Cipher8byteNonce,
    )
    from pyatv.support.http import (  # type: ignore
        decode_bplist_from_body,
    )
    from pyatv.support.rtsp import RtspSession  # type: ignore

    _STREAM_ERROR: Optional[str] = None
except Exception as exc:  # pragma: no cover
    StreamContext = None  # type: ignore
    AirPlayV2 = None  # type: ignore
    RtspSession = None  # type: ignore
    decode_bplist_from_body = None  # type: ignore
    Chacha20Cipher8byteNonce = None  # type: ignore
    EVENTS_SALT = EVENTS_READ_INFO = EVENTS_WRITE_INFO = None  # type: ignore
    _STREAM_ERROR = f"pyatv RAOP stream engine unavailable: {exc!r}"

from app.paths import user_data_dir


def _creds_path():
    return user_data_dir() / "airplay2_credentials.json"


def _load_creds() -> dict:
    """device_id -> serialized HapCredentials string. Missing or
    unreadable store is treated as empty; pairing just runs again."""
    try:
        with open(_creds_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        _say(f"credentials read failed: {exc}")
        return {}


def _save_cred(device_id: str, creds_str: str) -> None:
    store = _load_creds()
    store[device_id] = creds_str
    path = _creds_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Credentials are long-lived pairing secrets; keep them
        # owner-only, same posture the RAOP path used.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh)
    except OSError as exc:
        _say(f"credentials write failed: {exc}")


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
    """An AirPlay 2 candidate from discovery, with the TXT
    features/flags decoded (Stage 1)."""

    id: str
    name: str
    address: str
    port: int
    model: str
    # Decoded from the AirPlay TXT `features` bitfield via pyatv's
    # canonical AirPlayFlags table.
    supports_airplay_audio: bool
    supports_buffered_audio: bool
    supports_ptp: bool
    # Whether the receiver advertises a CoreUtils/transient pairing
    # path (the no-PIN HomeKit pairing the sender will use).
    supports_transient_pairing: bool
    # pyatv's pairing verdict for this service: "NotNeeded",
    # "Mandatory", "Unsupported", or "Disabled". "Unsupported"
    # means pyatv (and so this sender) cannot pair with it, e.g.
    # macOS "Current User" access control (act=2).
    pairing: str
    password_required: bool
    raw_features: int
    # Whether this sender can plausibly stream to it. reason is
    # populated only when streamable is False.
    streamable: bool
    reason: str = ""


def _classify_device(
    device_id: str, name: str, address: str, svc
) -> AirPlay2Device:
    """Decode an AirPlay service's TXT into capability flags and
    decide whether this sender can plausibly stream to it.

    Uses pyatv's canonical AirPlayFlags table and pairing/password
    logic. If pyatv's airplay utils aren't importable (version
    drift), the device is still surfaced but conservatively marked
    not streamable rather than guessed at."""
    props = dict(svc.properties or {})
    model = props.get("model", "")
    port = svc.port

    if parse_features is None:
        return AirPlay2Device(
            id=device_id,
            name=name,
            address=address,
            port=port,
            model=model,
            supports_airplay_audio=False,
            supports_buffered_audio=False,
            supports_ptp=False,
            supports_transient_pairing=False,
            pairing="Unknown",
            password_required=False,
            raw_features=0,
            streamable=False,
            reason=_FEATURES_ERROR or "feature decode unavailable",
        )

    feat_str = props.get("features", "")
    try:
        feats = parse_features(feat_str) if feat_str else AirPlayFlags(0)
    except ValueError:
        feats = AirPlayFlags(0)

    audio = AirPlayFlags.SupportsAirPlayAudio in feats
    buffered = AirPlayFlags.SupportsBufferedAudio in feats
    ptp = AirPlayFlags.SupportsPTP in feats
    transient = (
        AirPlayFlags.SupportsCoreUtilsPairingAndEncryption in feats
        or AirPlayFlags.SupportsUnifiedPairSetupandMFi in feats
    )
    pairing = get_pairing_requirement(svc).name
    pw_required = is_password_required(svc)

    # Streamable means: it does AirPlay audio, in the buffered mode
    # the sender targets, and there's a pairing path the sender can
    # actually perform. "Unsupported" pairing is macOS Current-User
    # access control (act=2) which the HAP transient path can't do;
    # password auth isn't implemented.
    if not audio:
        streamable, reason = False, "no AirPlay audio (video-only receiver)"
    elif not buffered:
        streamable, reason = (
            False,
            "no buffered-audio support (realtime/legacy only)",
        )
    elif pairing == "Unsupported":
        streamable, reason = (
            False,
            "pairing unsupported (macOS Current User access control)",
        )
    elif pw_required:
        streamable, reason = False, "password-protected (not implemented)"
    else:
        streamable, reason = True, ""

    return AirPlay2Device(
        id=device_id,
        name=name,
        address=address,
        port=port,
        model=model,
        supports_airplay_audio=audio,
        supports_buffered_audio=buffered,
        supports_ptp=ptp,
        supports_transient_pairing=transient,
        pairing=pairing,
        password_required=pw_required,
        raw_features=int(feats),
        streamable=streamable,
        reason=reason,
    )


@dataclass
class _PendingPair:
    """In-flight HAP pair-setup: PIN displayed, awaiting the code.
    Holds the open HTTP connection and pyatv pair-setup procedure
    between pair_begin and pair_finish."""

    device_id: str
    http: object
    procedure: object


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
        self._pending_pair: Optional[_PendingPair] = None

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
                _classify_device(
                    device_id=conf.identifier or str(conf.address),
                    name=conf.name,
                    address=str(conf.address),
                    svc=airplay_svc,
                )
            )
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

    # -- pairing (Stage 2) ------------------------------------------

    @staticmethod
    def is_paired(device_id: str) -> bool:
        return device_id in _load_creds()

    async def _resolve(self, device_id: str) -> Optional[AirPlay2Device]:
        for d in await self._discover(5.0):
            if d.id == device_id:
                return d
        return None

    def pair_begin(self, device_id: str) -> None:
        """Open a connection and start HAP pair-setup. The receiver
        displays a PIN; call pair_finish with it. For receivers that
        advertise transient pairing (the Macs) no pair flow is
        needed; _verify uses the transient path directly."""
        if pair_setup is None:
            raise RuntimeError(_PAIR_ERROR or "pyatv HAP pairing unavailable")
        self._run_coro(self._pair_begin(device_id), timeout=20.0)

    async def _pair_begin(self, device_id: str) -> None:
        await self._pair_cancel()
        device = await self._resolve(device_id)
        if device is None:
            raise RuntimeError(f"device {device_id} not found on network")
        http = await http_connect(device.address, device.port)
        try:
            proc = pair_setup(AuthenticationType.HAP, http)
            await proc.start_pairing()  # receiver displays the PIN
        except Exception:
            http.close()
            raise
        self._pending_pair = _PendingPair(
            device_id=device_id, http=http, procedure=proc
        )
        _say(f"pair-setup started for {device_id}; enter the PIN on the device")

    def pair_finish(self, pin: str) -> None:
        """Submit the PIN shown on the receiver; persist credentials."""
        if self._pending_pair is None:
            raise RuntimeError("no pairing in progress")
        self._run_coro(self._pair_finish(pin), timeout=20.0)

    async def _pair_finish(self, pin: str) -> None:
        pending = self._pending_pair
        if pending is None:
            raise RuntimeError("no pairing in progress")
        try:
            # pyatv's own handler zero-pads to 4 and passes a str
            # despite the int annotation; mirror that exactly.
            creds = await pending.procedure.finish_pairing(
                "", str(pin).strip().zfill(4), "Tideway"
            )
        finally:
            try:
                pending.http.close()
            except Exception:
                pass
            self._pending_pair = None
        _save_cred(pending.device_id, str(creds))
        _say(f"paired with {pending.device_id}")

    def pair_cancel(self) -> None:
        if self._pending_pair is None:
            return
        self._run_coro(self._pair_cancel(), timeout=5.0)

    async def _pair_cancel(self) -> None:
        pending = self._pending_pair
        self._pending_pair = None
        if pending is not None:
            try:
                pending.http.close()
            except Exception:
                pass

    async def _verify(self, device: AirPlay2Device):
        """Open a verified, encrypted AirPlay 2 session: stored HAP
        credentials if paired, else the transient path when the
        receiver advertises it. Returns (http, verifier) for Stage 3
        to derive the channel keys and open the audio RTSP session.
        Stage 2 deliverable: this round-trip succeeds."""
        http = await http_connect(device.address, device.port)
        try:
            stored = _load_creds().get(device.id)
            if stored:
                creds = parse_credentials(stored)
            elif device.supports_transient_pairing:
                creds = TRANSIENT_CREDENTIALS
            else:
                raise RuntimeError(
                    f"{device.id} needs pairing; run the pair flow first"
                )
            verifier = await verify_connection(creds, http)
        except Exception:
            http.close()
            raise
        return http, verifier

    # -- session SETUP (Stage 3) ------------------------------------

    def probe_setup(self, device_id: str) -> dict:
        """Stage 3 acceptance probe: assemble pyatv's AirPlayV2
        against the device's AirPlay service with our stored HAP
        credentials and confirm the receiver accepts pair-verify and
        the buffered-audio SETUP. Non-disruptive: negotiates only,
        no audio plays. Returns the negotiated ports."""
        if AirPlayV2 is None:
            raise RuntimeError(_STREAM_ERROR or "pyatv stream engine missing")
        return self._run_coro(self._probe_setup(device_id), timeout=30.0)

    async def _probe_setup(self, device_id: str) -> dict:
        device = await self._resolve(device_id)
        if device is None:
            raise RuntimeError(f"device {device_id} not found on network")
        stored = _load_creds().get(device.id)
        if not stored:
            raise RuntimeError(
                f"{device.id} not paired; run scripts/airplay2_pair.py first"
            )

        loop = asyncio.get_event_loop()
        http = await http_connect(device.address, device.port)
        # The SETUP bodies advertise our timing and control ports.
        # The receiver records them now and only connects during
        # RECORD/streaming (Stage 5), but bind real ephemeral UDP
        # sockets so the ports are genuine and ours.
        timing_t, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("0.0.0.0", 0)
        )
        control_t, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("0.0.0.0", 0)
        )
        timing_port = timing_t.get_extra_info("socket").getsockname()[1]
        control_port = control_t.get_extra_info("socket").getsockname()[1]

        context = StreamContext()
        context.credentials = parse_credentials(stored)
        rtsp = RtspSession(http)
        proto = AirPlayV2(context, rtsp)
        try:
            _say(f"opening AirPlay 2 session to {device.name}")
            await proto.setup(timing_port, control_port)
            result = {
                "data_port": context.server_port,
                "control_port": context.control_port,
                "verified": True,
            }
            _say(
                f"SETUP accepted by {device.name}: "
                f"dataPort={context.server_port} "
                f"controlPort={context.control_port}"
            )
            return result
        except Exception as exc:
            _say(f"SETUP failed for {device.name}: {exc}", exc=True)
            raise
        finally:
            try:
                proto.teardown()
            except Exception:
                pass
            for t in (timing_t, control_t):
                try:
                    t.close()
                except Exception:
                    pass
            http.close()

    def probe_setup_seq(self, device_id: str) -> dict:
        """Stage 3b probe: same as probe_setup but using owntone's
        canonical NTP sequence — session SETUP (NTP) -> RECORD ->
        stream SETUP with owntone's ALAC body. pyatv's AirPlayV2
        omits the RECORD before the stream SETUP and uses realtime
        PCM, which the Hisense never answers. This isolates whether
        the missing RECORD + ALAC body is the fix. Non-disruptive."""
        if AirPlayV2 is None:
            raise RuntimeError(_STREAM_ERROR or "pyatv stream engine missing")
        return self._run_coro(self._probe_setup_seq(device_id), timeout=30.0)

    async def _probe_setup_seq(self, device_id: str) -> dict:
        device = await self._resolve(device_id)
        if device is None:
            raise RuntimeError(f"device {device_id} not found on network")
        stored = _load_creds().get(device.id)
        if not stored:
            raise RuntimeError(
                f"{device.id} not paired; run scripts/airplay2_pair.py first"
            )

        # owntone's proven order: SETUP(session,NTP) -> RECORD ->
        # SETUP(stream, ALAC). pyatv's AirPlayV2 does session then
        # stream with no RECORD; subclass to inject it and swap the
        # stream body for owntone's.
        class _OwntoneSeqV2(AirPlayV2):  # type: ignore
            async def setup(self, timing_port: int, control_port: int) -> None:
                await self._setup_base(timing_port)
                # Empty RECORD, exactly as owntone/iOS send it.
                await self.rtsp.record()
                out_key, _ = self._verifier.encryption_keys(
                    EVENTS_SALT, EVENTS_WRITE_INFO, EVENTS_READ_INFO
                )
                shk = out_key[0:32]
                resp = await self.rtsp.setup(
                    body={
                        "streams": [
                            {
                                "audioFormat": 0x40000,  # ALAC/44100/16/2
                                "audioMode": "default",
                                "controlPort": control_port,
                                "ct": 2,  # ALAC
                                "isMedia": True,
                                "latencyMax": 88200,
                                "latencyMin": 11025,
                                "shk": shk,
                                "spf": 352,
                                "sr": 44100,
                                "type": 0x60,
                                "supportsDynamicStreamID": False,
                                "streamConnectionID": self.rtsp.session_id,
                            }
                        ]
                    }
                )
                r = decode_bplist_from_body(resp)
                stream = r["streams"][0]
                self.context.control_port = stream["controlPort"]
                self.context.server_port = stream["dataPort"]
                self._cipher = Chacha20Cipher8byteNonce(shk, shk)

        loop = asyncio.get_event_loop()
        http = await http_connect(device.address, device.port)
        timing_t, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("0.0.0.0", 0)
        )
        control_t, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("0.0.0.0", 0)
        )
        timing_port = timing_t.get_extra_info("socket").getsockname()[1]
        control_port = control_t.get_extra_info("socket").getsockname()[1]

        context = StreamContext()
        context.credentials = parse_credentials(stored)
        rtsp = RtspSession(http)
        proto = _OwntoneSeqV2(context, rtsp)
        try:
            _say(f"opening AirPlay 2 session (owntone seq) to {device.name}")
            await proto.setup(timing_port, control_port)
            _say(
                f"buffered SETUP accepted by {device.name}: "
                f"dataPort={context.server_port} "
                f"controlPort={context.control_port}"
            )
            return {
                "data_port": context.server_port,
                "control_port": context.control_port,
                "verified": True,
            }
        except Exception as exc:
            _say(f"buffered SETUP failed for {device.name}: {exc}", exc=True)
            raise
        finally:
            try:
                proto.teardown()
            except Exception:
                pass
            for t in (timing_t, control_t):
                try:
                    t.close()
                except Exception:
                    pass
            http.close()

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
