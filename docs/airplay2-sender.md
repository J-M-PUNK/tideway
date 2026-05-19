# AirPlay 2 audio sender

This doc covers the native AirPlay 2 audio sender Tideway is
building: why it exists, why it is a large effort rather than a
library call, the architecture decisions, the staged plan, and the
protocol references. It is the working scope document for the
`feature/airplay2` branch.

## Why this exists

Tideway briefly carried an AirPlay 1 / RAOP sender (via pyatv).
Hardware testing showed RAOP cannot reach the devices people
actually own here: modern smart TVs advertise only the AirPlay 2
service with no legacy RAOP, and the macOS AirPlay Receiver gates
RAOP behind HomeKit transient pairing that pyatv does not
implement (`PairingRequirement.Unsupported`, `stream_file` fails
with `AuthenticationError: not authenticated`). RAOP was shelved
on `feature/airplay-raop`. The only path that reaches a modern
AirPlay 2 receiver is a real AirPlay 2 audio sender.

## The hard truth

There is no embeddable Python AirPlay 2 sender, and pyatv will not
become one (its RAOP-only audio is a documented design decision,
issue postlund/pyatv#1059). The only mature open-source AirPlay 2
*sender* that interoperates with third-party TVs is owntone-server
(C, GPLv2), and it is a full server, not a library. Authentication
is HomeKit transient pairing, which is open and not gated by an
Apple per-vendor certificate, so an independent sender is
technically possible. It is just large: roughly the scope of the
Tidal Connect receiver work, multi-month, with ongoing maintenance
as Apple shifts the protocol.

## Architecture decisions

- **Pure-Python protocol.** Every primitive (SRP6a, Curve25519,
  Ed25519, ChaCha20-Poly1305, HKDF, SHA-512) exists in
  `cryptography` (already a dependency) plus `srptools` (a pyatv
  dependency, already present). pynacl is not needed. Binding the C
  `pair_ap` library would force a per-platform native build, the
  exact bundling pain avoided everywhere else in this project.
  `pair_ap` and owntone's `airplay.c` are reference oracles, not
  shipped code.
- **Reuse pyatv's HAP stack for pairing.** pyatv already ships a
  working HomeKit pair-setup, pair-verify, and encrypted-session
  implementation (`pyatv.auth.hap_srp`, `hap_pairing`,
  `hap_session`) that it uses to pair with Apple devices for
  MRP/Companion/AirPlay. Stage 2 reuses this rather than
  reimplementing SRP and the key schedule from scratch. This
  removes the authentication risk that would otherwise make Stage 2
  a wall. The novel work is Stages 3 to 5 (the AirPlay 2 RTSP
  variant, buffered-audio packetization, ALAC, per-packet
  encryption, and timing), which pyatv does not implement.
- **pyatv for discovery only.** `pyatv.scan` already finds AirPlay
  2 devices with address, port, and TXT features. Reuse it; hand
  roll only the AirPlay 2 session protocol on top.
- **A local debuggable receiver is mandatory tooling.**
  `openairplay/airplay2-receiver` (Python, verbose) runs on
  localhost as the development oracle. Every handshake is validated
  against a receiver whose internal state is readable before it is
  confirmed against a black-box TV. See `scripts/`.
- **Buffered audio mode**, the mode music apps use (ALAC, longer
  buffer, NTP/PTP anchor), not the realtime/mirroring mode.
- **Long-lived staged branch.** Not a release item. Delivered in
  verifiable stages; only bundled into a release once Stage 5
  plays audio on real hardware.

## Staged plan

Each stage ends in something independently verifiable, because the
whole thing is opaque otherwise.

0. Branch, this doc, the airplay2-receiver test rig, module
   scaffold. (current)
1. AirPlay 2 discovery plus decode of the TXT features and status
   flags for the target TV and the test receiver.
2. HomeKit transient pair-setup (PIN 3939) plus pair-verify, derive
   session keys. Verified against the receiver's debug log.
3. Encrypted RTSP control channel: ANNOUNCE, SETUP, RECORD
   accepted.
4. Timing channel: NTP buffered-mode anchor first, PTP if the
   receiver demands it.
5. ALAC-encode the live PCM (PyAV), AirPlay 2 packetization,
   per-packet encryption. Receiver decodes and plays, then the TV.
6. Control and lifecycle (volume, flush on track change, teardown,
   reconnect), wire into the manager and the Sound Output picker,
   fail-safe isolation so it can never take down local playback.
7. PyInstaller bundling and CI smoke.

Stages 2 and 4 are where this can stall hardest. Authentication
and timing are the classic AirPlay 2 walls.

## Re-scoping after Stage 0/1 investigation

pyatv ships far more of the AirPlay 2 control side than expected:

- `pyatv/protocols/airplay/ap2_session.py` (`AP2Session`) already
  does the encrypted AirPlay 2 connect, RTSP SETUP, event/data
  channels, and keep-alive. pyatv uses it for remote-control
  tunnelling, not audio, but the encrypted RTSP plumbing is
  reusable.
- `pyatv/protocols/airplay/auth/hap_transient.py` implements
  transient HomeKit pairing; `pyatv.auth.hap_*` covers
  pair-setup/verify and the session key schedule.
- `pyatv/protocols/airplay/utils.py` has the canonical
  `AirPlayFlags` table plus `parse_features`,
  `get_pairing_requirement`, `is_password_required`. Stage 1 reuses
  these directly.

### Decisive finding (Stage 3 investigation)

pyatv contains a complete, working AirPlay 2 audio sender that it
never exposes for our use case:

- `pyatv/protocols/raop/protocols/airplayv2.py` (`AirPlayV2`) does
  verify_connection, the base SETUP (timing + event channel), the
  buffered-audio stream SETUP (the real body: `audioFormat`, `ct`,
  `spf`, `sr`, `shk`, `type`), feedback, and `send_audio_packet`
  with ChaCha20 8-byte-nonce per-packet encryption.
- `pyatv/protocols/raop/stream_client.py` (`StreamClient`) drives
  it: NTP `TimingServer`, control client, the audio send loop,
  statistics.
- `pyatv/protocols/raop/__init__.py setup()` shows the assembly:
  `http_connect(addr, port)` → `RtspSession` → `AirPlayV2(context,
  rtsp)` → `StreamClient`, with `context.credentials` set.

`pyatv.stream_file` failed for us only because pyatv's RAOP
*discovery and credential* layer assumes a `_raop._tcp` service
and legacy RAOP auth. The streaming engine underneath is intact,
and Stage 2 produced exactly the HAP credentials it needs. So the
remaining stages are no longer "implement the protocol":

- **Stage 3**: assemble `AirPlayV2` against the device's AirPlay
  service (port 7000) with the Stage 2 HAP credentials in
  `StreamContext.credentials`; confirm the receiver accepts
  verify + the buffered-audio SETUP. Pure pyatv reuse.
- **Stage 4**: NTP timing is pyatv's `TimingServer`, already
  driven by `StreamClient`. Reuse.
- **Stage 5**: the only genuinely novel integration left, and it
  is integration not protocol: feed Tideway's live float32 PCM
  into pyatv's audio loop via an `AudioSource` adapter (pyatv's
  RAOP audio source is file/finite oriented; we need a live,
  endless source). ALAC/PCM packetization and encryption are
  already done by `AirPlayV2`/`StreamClient`.

Initial read was that this collapsed the multi-month estimate.
The Stage 3 hardware test corrected that, and the correction
matters:

**What genuinely collapsed (validated on the real TV):**

- Authentication. HAP pair-setup + pair-verify with the Stage 2
  credentials succeeds against the Hisense. Real, hard, done.
- Encrypted RTSP transport and the general/base SETUP. The TV
  accepts these via pyatv's reused machinery.

**What did NOT collapse (the real wall, confirmed):**

pyatv's `AirPlayV2` is the **NTP + realtime** variant (it
hardcodes `timingProtocol: "NTP"`, stream `type` 0x60). The
canonical receiver source is explicit: server version >= 355
means the device operates in **PTP + buffered** mode; <= 355 is
NTP + realtime. The Hisense advertises `srcvers 377.40.00`, so it
is PTP + buffered, like essentially every modern third-party
AirPlay 2 TV and speaker. It accepts the general SETUP (lenient)
but never answers a realtime/NTP stream SETUP. pyatv does not
implement the PTP + buffered path at all.

So the genuinely novel, undocumented work is back and real:

- A PTP (IEEE 1588) clock responder.
- `SETPEERS` (PTP peer list exchange) and `SETRATEANCHORTIME`.
- The buffered-audio stream SETUP (`type` 103, ALAC/PCM, `shk`).
- Buffered-audio packet pacing against the PTP anchor.

owntone implements all of this in C; `airplay2-receiver` is the
readable receiver-side spec oracle. No Python sender implements
the PTP buffered path.

### Correction after reading owntone's canonical sender

The PTP estimate was too pessimistic. owntone's `airplay.c` shows:

- owntone supports NTP and PTP. When no PTP daemon (nqptp) is
  present it falls back to **in-process NTP** and still streams
  buffered audio to third-party TVs. PTP/nqptp is NOT required.
- `payload_make_setpeers` is `if (!use_ptp) return 1; // Skip` —
  **SETPEERS is PTP-only**; the NTP path omits it.
- The canonical NTP start sequence is: `SETUP(session, NTP)` →
  `RECORD` (empty body) → `SETUP(stream)` → `SET_PARAMETER`. The
  general SETUP body is minimal: `deviceID`, `sessionUUID`,
  `timingPort`, `timingProtocol:"NTP"`.
- The stream SETUP body: `audioFormat 0x40000` (ALAC/44100/16/2),
  `ct:2` (ALAC), `type:0x60`, `spf:352`, `latencyMin:11025`,
  `shk:<32-byte key>`, `controlPort`, `streamConnectionID`.
- Audio encryption is ChaCha20-Poly1305, 32-byte key, 8-byte
  nonce, AAD = RTP header[4:12] — exactly what pyatv's
  `AirPlayV2.send_audio_packet` already implements.

pyatv's `AirPlayV2` does session SETUP then the stream SETUP with
**no RECORD in between**, and uses realtime PCM. The Hisense
accepting verify + general SETUP but never answering the stream
SETUP is consistent with the missing RECORD. So the likely fix is
a sequencing change (`SETUP(session,NTP)` → `RECORD` →
`SETUP(stream)` with owntone's ALAC body), not a PTP stack.

Net: auth + transport done; the remaining work is most likely
replicating owntone's NTP buffered sequence on top of pyatv's
primitives, then feeding live PCM (Stage 5). Validated
empirically against the Hisense before this is claimed — the
last "collapse" claim was made too early; this one is a
hypothesis until the TV answers the stream SETUP.

### Stage 3b result: NTP sequence disproven on the Hisense

Implemented owntone's exact NTP order on pyatv primitives:
`SETUP(session,NTP)` → `RECORD` → `SETUP(stream)` with owntone's
ALAC body (`audioFormat 0x40000`, `ct:2`, `type:0x60`, `spf:352`,
`shk` 32 bytes, `streamConnectionID`). Result against the
Hisense: session SETUP, event channel, and RECORD all succeed;
the **stream SETUP still times out**. The missing-RECORD
hypothesis is disproven on this device.

Conclusion: the Hisense (`srcvers 377`, PTP+buffered) does not
honor owntone's NTP fallback. The remaining path is the **full
PTP session SETUP**: `timingProtocol:"PTP"` with
`timingPeerInfo`/`timingPeerList`, the SETPEERS step, a
`TIME_ANNOUNCE_PTP` RTCP announcer (type 215, 28 bytes, sender as
PTP grandmaster — format known from airplay2-receiver
control.py), and `SETRATEANCHORTIME` with `networkTimeSecs/Frac/
TimelineID/rtpTime/rate`. This is the genuine multi-week
reverse-engineering effort, now empirically confirmed as required
for this device, not avoidable via the NTP shortcut.

The `probe_setup` / `probe_setup_seq` harnesses and this log are
the reusable artifacts; the NTP path stays in the tree as a
documented dead end for this class of receiver.

### PTP spike (step A): GATE PASSED on the Hisense

`probe_setup_ptp` replicates owntone's PTP path framing without a
running PTP clock: verify -> `SETUP(session, timingProtocol PTP,
timingPeerInfo/timingPeerList)` -> event channel -> `RECORD` ->
`SETPEERS` (array of [receiver ip, local ip]) -> `SETUP(stream)`
with owntone's ALAC body (`audioFormat 0x40000`, `ct:2`,
`type:0x67`/103 buffered, `shk` 32 bytes, `streamConnectionID`).

Result against the Hisense (`srcvers 377`):

    PTP session SETUP accepted; eventPort=44619
    RECORD accepted
    SETPEERS accepted
    BUFFERED STREAM SETUP ACCEPTED: dataPort=44077 controlPort=47256

The receiver answered the buffered stream SETUP and allocated
audio ports **with no PTP clock running**. PTP framing alone is
sufficient for the negotiation. The protocol-handshake wall that
blocked every prior attempt is solved on the real target. This is
the project go/no-go gate and it is GO.

Caveat (not repeating the earlier over-claim): SETUP acceptance is
not audio. Playback still needs the timing and audio path below.

## Complete wire spec (reverse-engineered, owntone-canonical)

Every byte layout is now known. Sources: owntone `rtp_common.c` /
`airplay.c` (canonical sender), validated against the Hisense for
auth + SETUP. No remaining wire-format unknowns.

**Timing is NOT a SETRATEANCHORTIME RTSP request.** owntone never
sends one. The anchor is delivered as an RTCP "time announce" /
sync packet sent periodically to the receiver's `controlPort`
(the one returned by the stream SETUP). PTP form, 28 bytes
(`RTCP_SYNC_PACKET_PTP_LEN`, matches the receiver's
`TIME_ANNOUNCE_PTP plen==28`):

    data[0]    = type      # 0x90 boot sync (M=1, stream start), 0x80 periodic
    data[1]    = 0xd7      # RTCP PT 215, "time announce"
    data[2:4]  = 00 06     # length in dwords
    data[4:8]  = be32(cur_stamp.pos)        # RTP pos this stamp refers to
    data[8:16] = be64(cur_ns)               # our monotonic time (ns) at pos
    data[16:20]= be32(pos - 11025)          # earliest rtptime to start playing
    data[20:28]= be64(ptp_clock_id)         # our self-assigned clock id

`cur_stamp` maps "RTP position `pos` == our monotonic time
`cur_ns`". We pick our own monotonic clock and `ptp_clock_id`.

**Audio packet** to the `dataPort` (RTP, 12-byte header):

    header[0]   = 0x80                 # RTP v2
    header[1]   = type                 # payload type (marker on first)
    header[2:4] = be16(seqnum)
    header[4:8] = be32(rtptime/pos)
    header[8:12]= be32(ssrc)           # 0 for a PTP session
    payload     = ChaCha20-Poly1305(ALAC_frame, key=shk,
                    aad=header[4:12], 8-byte nonce from seqnum)
    packet      = header + ciphertext + nonce[-8:]

This is exactly what pyatv's `AirPlayV2.send_audio_packet`
already implements (`Chacha20Cipher8byteNonce`, aad
`rtp_header[4:12]`). Audio is ALAC, 352 samples/frame, 44100/16/2;
PyAV (libav) has an ALAC encoder.

**The one remaining empirical question:** does the Hisense accept
us as its own grandmaster off these RTCP sync packets alone (no
real gPTP exchange on 319/320), or does it run a gPTP slave that
must lock to a real IEEE 1588 grandmaster (nqptp territory) before
it will render? owntone uses nqptp for real gPTP; the RTCP sync
packet is the rtptime/clock map either way. The Stage 4 spike
answers this: send the RTCP sync packets + audio with our own
monotonic clock as grandmaster, no gPTP, and listen for sound.
If silent, implement the pure-Python IEEE 1588 grandmaster with
nqptp (now cloned under .airplay2-test/) as the line reference.

Net: the protocol is fully cracked. What remains is engineering
(UDP control + data sockets, the periodic sync-packet sender, an
ALAC encoder, the RTP packetizer reusing pyatv's cipher, a pacing
loop), then the empirical grandmaster-trust gate, then the live
PCM feed and lifecycle.

Stage 1 finding: the target Hisense TV advertises
`SupportsAirPlayAudio + SupportsBufferedAudio + SupportsPTP` and
mandatory pairing, but does NOT advertise the CoreUtils/transient
pairing flag that the macOS receivers do. Stage 2 must therefore
support classic HAP pair-setup/verify (PIN shown on the TV), not
assume the no-PIN transient path.

## Protocol references

- AirPlay 2 internals, authentication: https://emanuelecozzi.net/docs/airplay2/
- Unofficial AirPlay spec, HomeKit pairing: https://openairplay.github.io/airplay-spec/
- openairplay/airplay2-receiver (protocol reference + test receiver): https://github.com/openairplay/airplay2-receiver
- owntone-server AirPlay 2 sender (canonical sender logic): https://github.com/owntone/owntone-server/blob/master/src/outputs/airplay.c
- ejurgensen/pair_ap (HomeKit pairing reference): https://github.com/ejurgensen/pair_ap
- pyatv RAOP-only rationale: https://github.com/postlund/pyatv/issues/1059
- shairport-sync (receiver reference): https://github.com/mikebrady/shairport-sync

## Test receiver rig

`scripts/airplay2_test_receiver.sh` clones and runs
openairplay/airplay2-receiver into a gitignored local directory. It
is dev-only and never bundled. Develop each stage against this
receiver with its verbose logging on, then confirm against the
real TV.
