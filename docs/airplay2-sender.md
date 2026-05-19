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

Net effect: Stages 2 and 3 are largely pyatv reuse plus adaptation
(issue the SETUP with an audio-stream descriptor rather than a
control descriptor), not from-scratch protocol work. The genuinely
novel, undocumented effort concentrates in **Stage 5**: ALAC
encode, the AirPlay 2 buffered-audio packet format, per-packet
ChaCha20-Poly1305 with the SETUP-derived key, and pacing against
the timing anchor. This is still substantial but materially
smaller than "implement the whole protocol."

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
