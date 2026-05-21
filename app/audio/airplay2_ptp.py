"""Minimal IEEE 1588v2 (PTPv2) clock module for AirPlay 2.

Empirical finding (2026-05-20): the doc had the gPTP topology
backwards. Modern AirPlay 2 receivers that advertise `SupportsPTP`
(server version >= 355, like the Hisense `srcvers 377.40.00`) do NOT
expect the sender to be the gPTP grandmaster. They themselves ARE the
grandmaster — broadcasting ANNOUNCE / SYNC / FOLLOW_UP on multicast
224.0.1.129:319-320 at the Apple-profile cadence (1 s / 125 ms /
125 ms). The sender slaves to the receiver's clock, projects its
audio-packet RTP timestamps onto the receiver's clock frame, and
labels the periodic RTCP TIME_ANNOUNCE with the receiver's clock_id
and a wall-clock time AS THE RECEIVER SEES IT.

This is what owntone does when paired with nqptp: nqptp listens for
ANNOUNCEs on the LAN, picks the AirPlay receiver as the grandmaster,
and feeds owntone the running offset. owntone then uses that offset
in its outgoing time-announce packets so the rtptime/wall-clock
mapping is consistent with what the receiver itself believes.

This module is the small subset of nqptp's behaviour we need:

  - `PtpSlave` listens on 319 + 320, joined to the AirPlay multicast
    group, captures ANNOUNCE / SYNC / FOLLOW_UP from the receiver's
    grandmaster.
  - It tracks the running offset between the local realtime clock and
    the master's clock from each FOLLOW_UP's preciseOriginTimestamp.
  - It exposes `master_clock_id` and `master_now_ns()` to the audio
    path so RTCP TIME_ANNOUNCE packets can carry the receiver's
    clock_id and a time that's valid on the receiver's clock.
  - No DELAY_REQ exchange. The buffered-audio latency window
    (latencyMin=11025, latencyMax=88200 samples per the RTSP SETUP)
    tolerates the few hundred microseconds of path delay we'd
    otherwise compensate for.
  - No transmission of our own ANNOUNCE / SYNC. We don't compete in
    BMCA — the receiver wins by default, which is what we want.

The original "PtpGrandmaster" build before this finding stays in the
file under that name (kept for the historical record + because it's
the right primitive if we ever target a receiver that wants the
opposite topology). The active component is `PtpSlave`.

Scope is intentionally tight. We do NOT implement:

  - The full Best Master Clock Algorithm (BMCA). We announce ourselves
    as a "good" grandmaster (priority1=128, priority2=128, clockClass
    248, accuracy 0xFE, variance 0xFFFF) — better than nqptp's default
    248/248 — and rely on the receiver being the only competitor on the
    AirPlay session's gPTP domain.
  - Path delay correction (E2E or P2P mechanism). We answer DELAY_REQ
    with DELAY_RESP using whatever monotonic time we have at receipt;
    the receiver's calculated delay will be wall-clock-noisy but the
    AirPlay buffered-audio path tolerates a wide latency window
    (latencyMin=11025 / latencyMax=88200 from the RTSP SETUP body).
  - Real SO_TIMESTAMP / hardware timestamping. originTimestamps come
    from `time.clock_gettime(CLOCK_REALTIME)` projected onto the PTP
    epoch (Unix epoch + the 37-second TAI/UTC offset Apple uses).
  - A PI servo, frequency adjustment, sub-microsecond accuracy, or any
    of the things that make a production PTP daemon respectable.

What this DOES do:

  - Binds two UDP sockets on the chosen interface: 319 (event,
    SYNC + DELAY_REQ) and 320 (general, ANNOUNCE + FOLLOW_UP +
    DELAY_RESP), each joined to the AirPlay PTP multicast group
    224.0.1.129.
  - Sends an ANNOUNCE every 1 second declaring our clock as the
    grandmaster. Apple's PTP profile uses `logAnnounceInterval = 0`.
  - Sends SYNC + FOLLOW_UP pairs every 125 ms (logSyncInterval = -3),
    the cadence airplay2-receiver and shairport-sync slaves expect.
    Two-step mode: SYNC carries a zero originTimestamp, FOLLOW_UP
    carries the precise origin timestamp captured just after SYNC
    egress.
  - Replies to DELAY_REQ with DELAY_RESP, echoing the slave's
    sourcePortIdentity in `requestingPortIdentity`. nqptp's slave path
    in `nqptp-message-handlers.c` is the format reference.

nqptp under `.airplay2-test/nqptp/` is the byte-layout oracle. Headers
mirror `nqptp-ptp-definitions.h`; the constants in
`send_awaken_announcement` (`nqptp.c:450-478`) are the source of the
Apple-profile values we put in our ANNOUNCE.

Threading: a single daemon thread runs an asyncio loop with the three
periodic tasks. `start()` blocks until the loop is up and sockets are
bound (so probe_play_tone can rely on PTP being live before SETUP);
`stop()` cancels everything and joins the thread. Failure inside the
loop never propagates to the audio path — the loop logs and continues,
because half-working PTP is still better than no PTP for the
diagnostic.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import socket
import struct
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)
_audio_log = logging.getLogger("tideway.audio")


def _say(msg: str, *, exc: bool = False) -> None:
    line = f"[airplay2-ptp] {msg}"
    if exc:
        line += "\n" + traceback.format_exc()
    print(line, flush=True)
    try:
        _audio_log.info(line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Wire layouts (from nqptp-ptp-definitions.h)
# ---------------------------------------------------------------------------

# Multicast address for all 1588 messages on the default UDP transport.
# 224.0.1.129 is the "PTP-primary" address from RFC 7384 / IEEE 1588
# annex D. AirPlay 2 uses the same address — there's no Apple-specific
# multicast.
PTP_MULTICAST = "224.0.1.129"
PTP_EVENT_PORT = 319    # SYNC + DELAY_REQ
PTP_GENERAL_PORT = 320  # ANNOUNCE + FOLLOW_UP + DELAY_RESP

# Message-type IDs (Table 19, IEEE 1588v2). transportSpecific = 1 for the
# 802.1AS / Apple profile, which is why every byte 0 is 0x10 + msgType.
_MSG_SYNC = 0x10 | 0
_MSG_DELAY_REQ = 0x10 | 1
_MSG_FOLLOW_UP = 0x10 | 8
_MSG_DELAY_RESP = 0x10 | 9
_MSG_ANNOUNCE = 0x10 | 11

# Common message header lengths.
_HEADER_LEN = 34
_SYNC_LEN = _HEADER_LEN + 10
_DELAY_REQ_LEN = _HEADER_LEN + 10
_FOLLOW_UP_LEN = _HEADER_LEN + 10
_DELAY_RESP_LEN = _HEADER_LEN + 10 + 10
_ANNOUNCE_LEN = _HEADER_LEN + 30

# Per the Apple Vendor PTP profile: log-2 of seconds.
#   logAnnounceInterval = 0  → 1 s
#   logSyncInterval = -3     → 0.125 s
_ANNOUNCE_INTERVAL_SEC = 1.0
_SYNC_INTERVAL_SEC = 0.125

# PTP epoch is the same as Unix (1970-01-01) but the clock is TAI, not
# UTC. TAI - UTC is +37 seconds (since 2017). We carry the offset in
# the ANNOUNCE's currentUtcOffset field so the slave can derive UTC
# back if it needs to; our SYNC/FOLLOW_UP timestamps are kept in UTC
# (matches Unix time directly) and the receiver simply uses them as a
# monotonic source.
_UTC_OFFSET_SECONDS = 37

# Apple-style sourcePortID. shairport-sync/nqptp use 32776 (0x8008) for
# the awakening ANNOUNCE; we use 1 (the per-spec default) for our own
# periodic stream because we present as a single-port clock and want
# slaves to address us at clockIdentity:portNumber=1.
_SOURCE_PORT_ID = 1

# Apple's "good grandmaster" clock quality. Same encoding nqptp uses
# when generating its own ANNOUNCE for the awakening case
# (`my_clock_quality = 0xf8fe436a`, big-endian). Bits:
#   clockClass = 0xF8 (248) - default for an arbitrary timekeeper
#   clockAccuracy = 0xFE (~unknown, but better-than-1ms)
#   offsetScaledLogVariance = 0x436A (16-bit, Apple-derived)
_CLOCK_QUALITY = 0xF8FE436A

# Lower priority1 = wins BMCA. nqptp's awaken sends `priority1-1` to
# look "slightly better" than the clock it's poking; we hard-code 128
# so we'll beat the default 248 grandmaster of any other competitor on
# the AirPlay session's domain without being aggressively low (which
# can confuse slaves expecting a default 248 master).
_PRIORITY1 = 128
_PRIORITY2 = 128

# Flags field (16 bits, big-endian). nqptp's awaken uses 0x0408. Two
# bits matter for our case:
#   twoStepFlag (bit 9) = 1 — we send FOLLOW_UP after each SYNC.
#   ptpTimescale (bit 3) = 1 — timestamps are PTP timescale (TAI).
# Together that's 0x0408. Matches nqptp.
_FLAGS_TWO_STEP_TAI = 0x0408

# `Internal Oscillator` per the timeSource table.
_TIME_SOURCE_INTERNAL_OSC = 160

# controlField values (deprecated in 1588v2 but receivers still parse
# them; matching nqptp's defaults keeps us safe).
_CONTROL_SYNC = 0x00
_CONTROL_DELAY_REQ = 0x01
_CONTROL_FOLLOW_UP = 0x02
_CONTROL_DELAY_RESP = 0x03
_CONTROL_OTHER = 0x05


def _ptp_timestamp(now_ns: Optional[int] = None) -> bytes:
    """Pack the current realtime clock as a 10-byte PTP timestamp.

    Layout: 6-byte seconds-since-epoch (big-endian) + 4-byte nanoseconds.
    We use the system realtime clock (CLOCK_REALTIME / time.time_ns())
    rather than CLOCK_MONOTONIC because the receiver's slave expects
    wall-clock-aligned timestamps to drive its AirPlay anchor math.
    """
    if now_ns is None:
        now_ns = time.time_ns()
    seconds, ns = divmod(now_ns, 1_000_000_000)
    return seconds.to_bytes(6, "big") + ns.to_bytes(4, "big")


def _pack_header(
    *,
    message_id: int,
    message_length: int,
    clock_id: bytes,
    sequence_id: int,
    control_field: int,
    log_period: int,
    source_port_id: int = _SOURCE_PORT_ID,
    flags: int = _FLAGS_TWO_STEP_TAI,
    correction_field: int = 0,
) -> bytes:
    """Build the 34-byte common message header.

    Field order (struct ptp_common_message_header in nqptp's header):
      uint8  transportSpecificAndMessageID
      uint8  reservedAndVersionPTP (0x02)
      uint16 messageLength
      uint8  domainNumber (0)
      uint8  reserved_b (0)
      uint16 flags
      uint64 correctionField
      uint32 reserved_l (0)
      uint8  clockIdentity[8]
      uint16 sourcePortID
      uint16 sequenceId
      uint8  controlField
      uint8  logMessagePeriod (signed; pack as unsigned with & 0xFF)
    """
    return struct.pack(
        ">BBHBBHQI8sHHBB",
        message_id,
        0x02,
        message_length,
        0,  # domainNumber
        0,  # reserved_b
        flags,
        correction_field,
        0,  # reserved_l
        clock_id,
        source_port_id,
        sequence_id,
        control_field,
        log_period & 0xFF,
    )


def build_announce(clock_id: bytes, seq: int) -> bytes:
    """ANNOUNCE message — declares us as the grandmaster.

    Sent every 1s on the general port (320). Values mirror nqptp's
    awakening ANNOUNCE except we use priority1=priority2=128 (better
    than the default 248) so any default-priority competitor on the
    domain loses BMCA cleanly without being so aggressive that slaves
    second-guess us.
    """
    header = _pack_header(
        message_id=_MSG_ANNOUNCE,
        message_length=_ANNOUNCE_LEN,
        clock_id=clock_id,
        sequence_id=seq,
        control_field=_CONTROL_OTHER,
        log_period=0,  # logAnnounceInterval = 0 → every 1 s
    )
    # Announce body:
    #   uint8[10] originTimestamp (zero by convention for ANNOUNCE)
    #   uint16 currentUtcOffset
    #   uint8  reserved (0)
    #   uint8  grandmasterPriority1
    #   uint32 grandmasterClockQuality
    #   uint8  grandmasterPriority2
    #   uint8[8] grandmasterIdentity
    #   uint16 stepsRemoved
    #   uint8  timeSource
    body = struct.pack(
        ">10sHBBIB8sHB",
        b"\x00" * 10,
        _UTC_OFFSET_SECONDS,
        0,  # reserved
        _PRIORITY1,
        _CLOCK_QUALITY,
        _PRIORITY2,
        clock_id,
        0,  # stepsRemoved — we ARE the grandmaster, no hops
        _TIME_SOURCE_INTERNAL_OSC,
    )
    return header + body


def build_sync(clock_id: bytes, seq: int) -> bytes:
    """SYNC message — empty origin timestamp, two-step. The real time
    goes in the paired FOLLOW_UP. Sent on the event port (319).
    """
    header = _pack_header(
        message_id=_MSG_SYNC,
        message_length=_SYNC_LEN,
        clock_id=clock_id,
        sequence_id=seq,
        control_field=_CONTROL_SYNC,
        log_period=-3,  # logSyncInterval = -3 → every 125 ms
    )
    return header + b"\x00" * 10  # originTimestamp = 0 in two-step mode


def build_follow_up(clock_id: bytes, seq: int, sync_egress_ns: int) -> bytes:
    """FOLLOW_UP for the SYNC with matching sequence_id. Carries the
    precise egress timestamp of the SYNC (captured just after sendto).
    Sent on the general port (320).
    """
    header = _pack_header(
        message_id=_MSG_FOLLOW_UP,
        message_length=_FOLLOW_UP_LEN,
        clock_id=clock_id,
        sequence_id=seq,
        control_field=_CONTROL_FOLLOW_UP,
        log_period=-3,
    )
    return header + _ptp_timestamp(sync_egress_ns)


def build_delay_resp(
    clock_id: bytes,
    seq: int,
    receive_ns: int,
    requesting_clock_id: bytes,
    requesting_port_id: int,
) -> bytes:
    """DELAY_RESP — echoes the DELAY_REQ's sourcePortIdentity in
    `requestingPortIdentity` and timestamps the moment we received
    the DELAY_REQ. Sent on the general port (320).
    """
    header = _pack_header(
        message_id=_MSG_DELAY_RESP,
        message_length=_DELAY_RESP_LEN,
        clock_id=clock_id,
        sequence_id=seq,
        control_field=_CONTROL_DELAY_RESP,
        log_period=-3,
    )
    body = (
        _ptp_timestamp(receive_ns)
        + requesting_clock_id
        + requesting_port_id.to_bytes(2, "big")
    )
    return header + body


@dataclass
class _ParsedDelayReq:
    sequence_id: int
    requesting_clock_id: bytes
    requesting_port_id: int


def parse_delay_req(data: bytes) -> Optional[_ParsedDelayReq]:
    """Pull the bits we need to build the DELAY_RESP out of an incoming
    DELAY_REQ. Returns None for anything that isn't a parseable
    DELAY_REQ (wrong message type, wrong version, too short) so the
    listener loop can drop noise without trying."""
    if len(data) < _DELAY_REQ_LEN:
        return None
    msg_id = data[0]
    if (msg_id & 0x0F) != 1:  # Delay_Req = 1
        return None
    version = data[1] & 0x0F
    if version != 2:
        return None
    requesting_clock_id = data[20:28]
    requesting_port_id = struct.unpack(">H", data[28:30])[0]
    sequence_id = struct.unpack(">H", data[30:32])[0]
    return _ParsedDelayReq(
        sequence_id=sequence_id,
        requesting_clock_id=requesting_clock_id,
        requesting_port_id=requesting_port_id,
    )


# ---------------------------------------------------------------------------
# The grandmaster
# ---------------------------------------------------------------------------


def _generate_clock_id() -> bytes:
    """Produce an 8-byte clock identifier. IEEE 1588 says this should
    be a EUI-64 derived from the MAC address (insert 0xFFFE in the
    middle). We use 8 random bytes instead — slaves don't check the
    EUI-64 structure, only the uniqueness, and our airplay2 module
    already generates the integer clock_id with the same shape."""
    return secrets.token_bytes(8)


class PtpGrandmaster:
    """Minimal PTPv2 grandmaster bound to one interface.

    Lifecycle:

        gm = PtpGrandmaster(clock_id=clock_id_bytes, interface_ip="192.168.1.202")
        gm.start()           # blocks until sockets are bound
        try:
            ...              # run RTSP SETUP + probe_play_tone here
        finally:
            gm.stop()

    The asyncio loop runs on a daemon thread so the caller never has to
    deal with the loop directly. Failures inside the loop are logged
    but never re-raised — a degraded grandmaster is more useful than
    no grandmaster for the diagnostic.
    """

    def __init__(self, clock_id: bytes, interface_ip: str) -> None:
        if len(clock_id) != 8:
            raise ValueError("clock_id must be 8 bytes")
        self.clock_id = clock_id
        self.interface_ip = interface_ip
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stop_evt: Optional[asyncio.Event] = None
        self._event_sock: Optional[socket.socket] = None
        self._general_sock: Optional[socket.socket] = None
        self._announce_seq = 0
        self._sync_seq = 0
        # Wall-clock anchor for trace lines so a hardware test produces
        # a useful timeline without needing a separate logger.
        self._started_at_ns = 0

    # ---- public ----

    def start(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            return
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="airplay2-ptp", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("grandmaster failed to start within timeout")

    def stop(self, timeout: float = 3.0) -> None:
        loop = self._loop
        stop_evt = self._stop_evt
        if loop is None or stop_evt is None:
            return
        loop.call_soon_threadsafe(stop_evt.set)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def __enter__(self) -> "PtpGrandmaster":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ---- internals ----

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception:
            _say("grandmaster loop crashed", exc=True)
        finally:
            try:
                loop.close()
            finally:
                self._loop = None

    async def _main(self) -> None:
        self._stop_evt = asyncio.Event()
        try:
            self._event_sock = self._make_socket(PTP_EVENT_PORT)
            self._general_sock = self._make_socket(PTP_GENERAL_PORT)
        except Exception as exc:
            _say(f"socket setup failed: {exc!r}")
            self._ready.set()  # unblock start() so it can raise
            return

        self._started_at_ns = time.time_ns()
        _say(
            f"grandmaster up on {self.interface_ip}; clock_id="
            f"{self.clock_id.hex()}"
        )
        self._ready.set()

        tasks = [
            asyncio.create_task(self._announce_loop(), name="ptp-announce"),
            asyncio.create_task(self._sync_loop(), name="ptp-sync"),
            asyncio.create_task(
                self._delay_req_loop(), name="ptp-delay-req"
            ),
            asyncio.create_task(
                self._general_recv_loop(), name="ptp-general-recv"
            ),
        ]
        try:
            await self._stop_evt.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for s in (self._event_sock, self._general_sock):
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass
            _say("grandmaster down")

    def _make_socket(self, port: int) -> socket.socket:
        """Build a UDP socket bound to the multicast group on the
        chosen interface, with the bits set so we receive multicast
        AND send from the same socket.

        macOS lets us bind to 0.0.0.0 and the IP_ADD_MEMBERSHIP join
        determines which interface delivers traffic. The
        IP_MULTICAST_IF on the same socket controls our outbound
        interface so the receiver sees packets sourced from the
        AirPlay session's IP, not whichever interface the OS happens
        to pick first.
        """
        s = socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP
        )
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (OSError, AttributeError):
            pass
        s.bind(("0.0.0.0", port))
        mreq = struct.pack(
            "4s4s",
            socket.inet_aton(PTP_MULTICAST),
            socket.inet_aton(self.interface_ip),
        )
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        s.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_MULTICAST_IF,
            socket.inet_aton(self.interface_ip),
        )
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        # Do NOT loop multicast back to ourselves — saves a tight
        # parse loop on outgoing announce/sync packets we just sent.
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        s.setblocking(False)
        return s

    async def _announce_loop(self) -> None:
        while not self._stop_evt.is_set():  # type: ignore[union-attr]
            seq = self._announce_seq & 0xFFFF
            self._announce_seq += 1
            try:
                pkt = build_announce(self.clock_id, seq)
                if self._general_sock is not None:
                    self._general_sock.sendto(
                        pkt, (PTP_MULTICAST, PTP_GENERAL_PORT)
                    )
            except Exception as exc:
                _say(f"announce send failed: {exc!r}")
            await asyncio.sleep(_ANNOUNCE_INTERVAL_SEC)

    async def _sync_loop(self) -> None:
        # Two-step: send SYNC with zero ts, capture egress, send
        # FOLLOW_UP with the captured time. Keep them tightly paired
        # to minimise the (sync_egress - follow_up_egress) gap so the
        # slave's path-delay math doesn't have to compensate for our
        # ~tens-of-microseconds of host-side processing.
        while not self._stop_evt.is_set():  # type: ignore[union-attr]
            seq = self._sync_seq & 0xFFFF
            self._sync_seq += 1
            try:
                sync_pkt = build_sync(self.clock_id, seq)
                if self._event_sock is not None:
                    self._event_sock.sendto(
                        sync_pkt, (PTP_MULTICAST, PTP_EVENT_PORT)
                    )
                egress_ns = time.time_ns()
                fup_pkt = build_follow_up(self.clock_id, seq, egress_ns)
                if self._general_sock is not None:
                    self._general_sock.sendto(
                        fup_pkt, (PTP_MULTICAST, PTP_GENERAL_PORT)
                    )
            except Exception as exc:
                _say(f"sync/follow_up send failed: {exc!r}")
            await asyncio.sleep(_SYNC_INTERVAL_SEC)

    async def _delay_req_loop(self) -> None:
        # DELAY_REQ messages come in on the EVENT socket (319) as
        # multicast or unicast. We answer each one with a DELAY_RESP
        # on the GENERAL socket (320) addressed to the slave's
        # sourcePortIdentity. The slave's IP comes from the recvfrom
        # tuple — multicast destination, unicast source.
        await self._recv_loop(self._event_sock, "event", handle_delay_req=True)

    async def _general_recv_loop(self) -> None:
        """Listen on UDP 320. Used for diagnostic: we want to see any
        ANNOUNCE from a competing grandmaster on the network (which
        could be losing BMCA against us, leaving the slave unsure
        who to lock to), or any unexpected traffic the receiver
        sends our way."""
        await self._recv_loop(self._general_sock, "general")

    async def _recv_loop(
        self, sock: Optional[socket.socket], label: str,
        handle_delay_req: bool = False,
    ) -> None:
        if sock is None:
            return
        loop = asyncio.get_event_loop()
        # `sock_recvfrom` gives us the sender address so we can tell
        # apart "the TV is talking to us" from "another box on the LAN
        # is also broadcasting PTP".
        while not self._stop_evt.is_set():  # type: ignore[union-attr]
            try:
                data, addr = await loop.sock_recvfrom(sock, 2048)  # type: ignore[arg-type]
            except (asyncio.CancelledError, OSError):
                return
            except Exception as exc:
                _say(f"{label} recv failed: {exc!r}")
                continue
            recv_ns = time.time_ns()
            self._trace_inbound(data, addr, label)
            if not handle_delay_req:
                continue
            req = parse_delay_req(data)
            if req is None:
                continue
            try:
                resp = build_delay_resp(
                    self.clock_id,
                    seq=req.sequence_id,
                    receive_ns=recv_ns,
                    requesting_clock_id=req.requesting_clock_id,
                    requesting_port_id=req.requesting_port_id,
                )
                if self._general_sock is not None:
                    self._general_sock.sendto(
                        resp, (PTP_MULTICAST, PTP_GENERAL_PORT)
                    )
                _say(
                    f"DELAY_RESP -> {addr[0]} seq={req.sequence_id} "
                    f"clock_id={req.requesting_clock_id.hex()}"
                )
            except Exception as exc:
                _say(f"delay_resp send failed: {exc!r}")

    def _trace_inbound(self, data: bytes, addr, label: str) -> None:
        """One line per inbound PTP packet so the test trace makes the
        receiver-side behaviour observable. Filters our own packets
        (multicast loopback should already drop them, but defence in
        depth) and decodes the message type for readability."""
        if not data:
            return
        msg_type = data[0] & 0x0F
        msg_names = {
            0: "SYNC",
            1: "DELAY_REQ",
            8: "FOLLOW_UP",
            9: "DELAY_RESP",
            11: "ANNOUNCE",
        }
        name = msg_names.get(msg_type, f"type=0x{msg_type:x}")
        # Don't trace our own announces — only useful when there's
        # a competing grandmaster or the receiver is talking back.
        if len(data) >= 28:
            sender_clock = data[20:28]
            if sender_clock == self.clock_id:
                return
        seq = (
            int.from_bytes(data[30:32], "big") if len(data) >= 32 else None
        )
        _say(
            f"{label} <- {addr[0]}:{addr[1]} {name} "
            f"seq={seq} bytes={len(data)} clock={data[20:28].hex() if len(data)>=28 else '?'}"
        )


# ---------------------------------------------------------------------------
# PtpSlave — the actually-active path
# ---------------------------------------------------------------------------


@dataclass
class _MasterState:
    """What we've learned about the receiver's PTP grandmaster.

    `clock_id` is the 8-byte EUI-64 the master broadcasts in every
    ANNOUNCE / SYNC / FOLLOW_UP header. `offset_ns` is the running
    estimate of `master_time - local_realtime` (positive = master is
    ahead of us). `last_follow_up_at_ns` is local-clock wall time of
    when we last updated `offset_ns`; useful for logging and for a
    "is the offset stale" sanity check by callers."""

    clock_id: bytes
    offset_ns: int
    last_follow_up_at_ns: int
    follow_up_count: int


def _parse_sync_seq(data: bytes) -> Optional[int]:
    if len(data) < 32 or (data[0] & 0x0F) != 0:
        return None
    return int.from_bytes(data[30:32], "big")


def _parse_follow_up_origin_ts(data: bytes) -> Optional[int]:
    """Pull preciseOriginTimestamp out of a FOLLOW_UP, return ns
    since epoch. Returns None for anything that isn't a parseable
    FOLLOW_UP."""
    if len(data) < _FOLLOW_UP_LEN or (data[0] & 0x0F) != 8:
        return None
    ts = data[_HEADER_LEN : _HEADER_LEN + 10]
    seconds = int.from_bytes(ts[0:6], "big")
    nanos = int.from_bytes(ts[6:10], "big")
    return seconds * 1_000_000_000 + nanos


class PtpSlave:
    """Passive PTP slave for AirPlay 2.

    Listens for the receiver's gPTP grandmaster ANNOUNCEs and pairs
    them with the FOLLOW_UPs to extract a master-clock offset. Does
    not transmit anything — no DELAY_REQ, no ANNOUNCE — so we never
    compete in BMCA and the receiver stays grandmaster.

    The grandmaster's clock_id is auto-detected on first ANNOUNCE.
    If multiple grandmasters are on the LAN (unusual on a home net)
    we pick the highest-priority one per a minimal BMCA stand-in:
    smaller priority1 wins, then smaller clock_id. For now that's
    "pick the first one and stick with it" since the AirPlay session
    on the target subnet is overwhelmingly going to have exactly one
    relevant grandmaster.

    Threading mirrors PtpGrandmaster: daemon thread + asyncio loop,
    `start()` blocks until sockets are bound (and an initial ANNOUNCE
    has been seen if `wait_for_lock_sec > 0`), `stop()` joins.
    """

    def __init__(self, interface_ip: str) -> None:
        self.interface_ip = interface_ip
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._sockets_ready = threading.Event()
        self._first_offset_event = threading.Event()
        self._stop_evt: Optional[asyncio.Event] = None
        self._event_sock: Optional[socket.socket] = None
        self._general_sock: Optional[socket.socket] = None
        # Sequence -> (egress_capture_ns) for pairing SYNC ↔ FOLLOW_UP
        # by sequence id. Spec doesn't require local capture (since we
        # use the FOLLOW_UP's own preciseOriginTimestamp), but tracking
        # the pair lets us notice if SYNCs are missing entirely.
        self._sync_seen: dict[int, int] = {}
        self._state_lock = threading.Lock()
        self._state: Optional[_MasterState] = None

    # ---- public ----

    def start(
        self, sockets_timeout: float = 3.0, lock_timeout: float = 4.0
    ) -> None:
        if self._thread is not None:
            return
        self._sockets_ready.clear()
        self._first_offset_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="airplay2-ptp-slave", daemon=True
        )
        self._thread.start()
        if not self._sockets_ready.wait(sockets_timeout):
            raise RuntimeError("PTP slave sockets did not bind in time")
        if lock_timeout > 0:
            if not self._first_offset_event.wait(lock_timeout):
                _say(
                    "warning: no FOLLOW_UP from a master within "
                    f"{lock_timeout:.1f}s — proceeding without clock offset"
                )

    def stop(self, timeout: float = 3.0) -> None:
        loop = self._loop
        stop_evt = self._stop_evt
        if loop is None or stop_evt is None:
            return
        loop.call_soon_threadsafe(stop_evt.set)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def __enter__(self) -> "PtpSlave":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def wait_for_lock(self, timeout: float) -> bool:
        """Block until the slave has seen at least one FOLLOW_UP from
        the master (so `offset_ns` reflects real master clock time,
        not the ANNOUNCE-only zero). Returns True on success, False on
        timeout. Used by callers that need a valid master-clock
        timestamp before the first outgoing RTCP TIME_ANNOUNCE — the
        AirPlay receiver appears to anchor its rtptime/clock map off
        the FIRST sync packet, so getting that one right matters more
        than getting later ones right."""
        return self._first_offset_event.wait(timeout)

    def master_clock_id(self) -> Optional[bytes]:
        """The 8-byte clock_id of the receiver's grandmaster, or None
        if we haven't heard an ANNOUNCE yet."""
        with self._state_lock:
            return self._state.clock_id if self._state else None

    def master_now_ns(self) -> Optional[int]:
        """Current time on the master's clock, in nanoseconds. None if
        we haven't locked yet. Callers use this for the `cur_ns` field
        of the RTCP TIME_ANNOUNCE packet so the receiver's mapping is
        consistent with its own clock."""
        with self._state_lock:
            if self._state is None:
                return None
            return time.time_ns() + self._state.offset_ns

    def snapshot(self) -> Optional[dict]:
        """Diagnostic snapshot for the dev script."""
        with self._state_lock:
            if self._state is None:
                return None
            return {
                "clock_id": self._state.clock_id.hex(),
                "offset_ns": self._state.offset_ns,
                "follow_up_count": self._state.follow_up_count,
                "age_ms": (
                    (time.time_ns() - self._state.last_follow_up_at_ns) // 1_000_000
                ),
            }

    # ---- internals ----

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception:
            _say("ptp slave loop crashed", exc=True)
        finally:
            try:
                loop.close()
            finally:
                self._loop = None

    async def _main(self) -> None:
        self._stop_evt = asyncio.Event()
        try:
            self._event_sock = self._make_socket(PTP_EVENT_PORT)
            self._general_sock = self._make_socket(PTP_GENERAL_PORT)
        except Exception as exc:
            _say(f"slave socket setup failed: {exc!r}")
            self._sockets_ready.set()
            return
        self._sockets_ready.set()
        _say(f"PTP slave up on {self.interface_ip}; listening for master")
        tasks = [
            asyncio.create_task(self._recv("event", self._event_sock)),
            asyncio.create_task(self._recv("general", self._general_sock)),
        ]
        try:
            await self._stop_evt.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for s in (self._event_sock, self._general_sock):
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass
            _say("PTP slave down")

    def _make_socket(self, port: int) -> socket.socket:
        s = socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP
        )
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (OSError, AttributeError):
            pass
        s.bind(("0.0.0.0", port))
        mreq = struct.pack(
            "4s4s",
            socket.inet_aton(PTP_MULTICAST),
            socket.inet_aton(self.interface_ip),
        )
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        s.setblocking(False)
        return s

    async def _recv(self, label: str, sock: Optional[socket.socket]) -> None:
        if sock is None:
            return
        loop = asyncio.get_event_loop()
        while not self._stop_evt.is_set():  # type: ignore[union-attr]
            try:
                data, addr = await loop.sock_recvfrom(sock, 2048)  # type: ignore[arg-type]
            except (asyncio.CancelledError, OSError):
                return
            except Exception as exc:
                _say(f"slave {label} recv failed: {exc!r}")
                continue
            self._handle(data, addr, label)

    def _handle(self, data: bytes, addr, label: str) -> None:
        if not data:
            return
        msg_type = data[0] & 0x0F
        if msg_type == 11:  # ANNOUNCE
            self._on_announce(data, addr)
        elif msg_type == 0:  # SYNC
            seq = _parse_sync_seq(data)
            if seq is not None:
                # Record local arrival time for diagnostics; not used
                # in offset math since we trust the FOLLOW_UP's own
                # preciseOriginTimestamp.
                self._sync_seen[seq] = time.time_ns()
                # Trim the seen dict so it doesn't grow indefinitely.
                if len(self._sync_seen) > 64:
                    for k in list(self._sync_seen.keys())[:-32]:
                        self._sync_seen.pop(k, None)
        elif msg_type == 8:  # FOLLOW_UP
            self._on_follow_up(data, addr)

    def _on_announce(self, data: bytes, addr) -> None:
        if len(data) < 28:
            return
        master_clock = data[20:28]
        with self._state_lock:
            if self._state is None:
                self._state = _MasterState(
                    clock_id=master_clock,
                    offset_ns=0,
                    last_follow_up_at_ns=0,
                    follow_up_count=0,
                )
                _say(
                    f"PTP slave locked grandmaster {master_clock.hex()} "
                    f"@ {addr[0]}"
                )

    def _on_follow_up(self, data: bytes, addr) -> None:
        master_time_ns = _parse_follow_up_origin_ts(data)
        if master_time_ns is None:
            return
        local_arrival_ns = time.time_ns()
        with self._state_lock:
            if self._state is None:
                # Heard FOLLOW_UP before ANNOUNCE — use this sender's
                # clock_id as the master. Some grandmasters phase
                # FOLLOW_UPs ahead of the next ANNOUNCE in their
                # cadence.
                if len(data) >= 28:
                    self._state = _MasterState(
                        clock_id=data[20:28],
                        offset_ns=0,
                        last_follow_up_at_ns=0,
                        follow_up_count=0,
                    )
                else:
                    return
            sender_clock = data[20:28]
            if sender_clock != self._state.clock_id:
                # FOLLOW_UP from a non-master; ignore (could be a
                # second grandmaster on a multi-LAN setup, or our
                # own loopback before we set LOOP=0).
                return
            # Naive offset: master_time - local_arrival_time. Ignores
            # path delay (we'd need DELAY_REQ/RESP for the symmetric
            # estimate) but the AirPlay buffered window swallows the
            # one-way LAN latency.
            offset_ns = master_time_ns - local_arrival_ns
            self._state.offset_ns = offset_ns
            self._state.last_follow_up_at_ns = local_arrival_ns
            self._state.follow_up_count += 1
        if self._state.follow_up_count == 1:
            _say(
                f"first FOLLOW_UP from master "
                f"{self._state.clock_id.hex()}: offset={offset_ns}ns"
            )
            self._first_offset_event.set()


__all__ = [
    "PtpGrandmaster",
    "PtpSlave",
    "build_announce",
    "build_sync",
    "build_follow_up",
    "build_delay_resp",
    "parse_delay_req",
]
