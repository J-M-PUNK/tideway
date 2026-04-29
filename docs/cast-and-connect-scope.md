# Chromecast + Tidal Connect — implementation scope

Two output integrations, both "Tideway sends audio to a device on the
LAN," but with different architectures. Cast streams Tideway's
decoded audio to the device. Connect tells the device to fetch
straight from Tidal and Tideway is just a remote control.

This doc captures what would actually have to be built. Estimates
are honest "focused work" estimates, not optimistic ones.

## Existing reusable code

`app/audio/airplay.py` (860 lines, untested) already implements the
"serve Tideway's PCM over HTTP" half of the Cast story:

- `RingBuffer` — feeds the latest PCM frames into a circular buffer
  the HTTP server reads from.
- `FlacStreamEncoder` — PyAV pipeline that encodes PCM to FLAC on
  the fly so we ship lossless to receivers.
- `_StreamHTTPServer` / `_StreamRequestHandler` — bound to a LAN IP,
  serves the encoded stream as `audio/flac` with a Content-Type the
  Cast Default Media Receiver and AirPlay both accept.
- `_primary_lan_ip()` — picks the right interface so the URL we
  hand the device is reachable.

The Cast project reuses these directly. The AirPlay-specific bits
(pyatv pairing, RAOP control) stay in `airplay.py` and aren't
touched.

`app/audio/upnp.py` (215 lines) is the closest precedent for what
Tidal Connect needs — OpenHome / SOAP control of a LAN device. The
discovery and SOAP-call patterns are similar enough that the Connect
implementation can crib structure from it.

## Scope split — Cast first, Connect second

These are two separate releases. Cast is well-bounded, Connect has a
real reverse-engineering risk window. Sequencing them lets Cast ship
on a known timeline while Connect's protocol scoping happens in
parallel without blocking the smaller win.

---

# Part 1 — Chromecast (sender)

## What we're building

Tideway plays a track. User opens a "Devices" picker in the now-
playing bar. List shows Cast targets discovered on the LAN. User
picks one. Audio routes to that device instead of (or in addition
to) the local sounddevice output. Volume / play / pause / seek in
Tideway's UI control the Cast device. Cast's own remote (Google
Home app, casts from other apps) can interrupt; Tideway sees the
state change and reflects it.

Disable: pick "This device" in the picker, audio returns to local.

## Tidal-app parity check

The official Tidal desktop app has a Cast picker. So this is
matching parity, not differentiation. Bar to clear: at minimum
discover the same devices, maintain control over the session
through the same lifecycle events.

## Library

`pychromecast` — mature, actively maintained, MIT-licensed, the
canonical Python Cast controller. Used by Home Assistant, Mopidy,
Music Assistant. Discovery via mDNS, control via a Cast Application
Framework abstraction, supports the Default Media Receiver app for
audio URLs.

## Architecture

The audio engine grows a sink abstraction. Today `PCMPlayer` writes
to a single sounddevice OutputStream. After this change, it writes
to one or more registered sinks:

- `LocalSink` — the existing sounddevice output.
- `CastSink` — feeds the FLAC encoder ringbuffer that the HTTP
  server reads from. The Cast device fetches the URL we hand it.

`CastSink` is "active" when the user has selected a Cast device.
While active, `LocalSink` is muted by default. A future "play on
local AND Cast" multi-room toggle is possible but not part of this
scope.

`CastSession` (new) wraps a single device's lifecycle:

- Discover via `pychromecast.get_chromecasts()`.
- On select: spin up the HTTP server (reusing `airplay.py`'s code),
  start the encoder, issue `mc.play_media(url, "audio/flac")` to
  the device's media controller.
- Subscribe to the controller's status updates so phone /
  third-party app interruptions surface as state changes in
  Tideway's now-playing UI.
- On track change: `play_media` again with the new URL. Pause /
  resume / seek / volume map to the matching `MediaController`
  methods.
- On disconnect (network drop, device powered off): fall back to
  local. Surface a toast.

## What the device actually receives

The Default Media Receiver supports HTTP `audio/flac`,
`audio/mpeg`, `audio/aac`, `audio/wav`. Hi-res FLAC is supported up
to 24/96 on most Cast-built-in speakers; some downsample. We ship
FLAC at the source's native rate (same as Tideway's local output)
and the receiver decides what its DAC handles. Same trade-off as
AirPlay sender — speaker dictates the ceiling.

## Tasks

| | Item | Effort |
|---|---|---|
| 1 | `pychromecast` discovery wired to a `CastDeviceList` model in the audio engine. | 0.5d |
| 2 | `CastSession` class — connect, play_media, track change, status subscribe. | 1.5d |
| 3 | `CastSink` adapter — bridge between the existing PCM ringbuffer and the existing HTTP server. Mostly glue; the components exist. | 1d |
| 4 | Player-engine integration — `PCMPlayer` learns about sinks; selecting a Cast device mutes Local and routes to Cast. | 1.5d |
| 5 | UI — devices picker in `NowPlaying.tsx`, status indicator when casting, "stop casting" affordance. | 1.5d |
| 6 | State sync — when the Cast device gets paused / resumed by another controller, Tideway's state reflects it. | 1d |
| 7 | Edge cases — disconnect handling, network change, port collision, IPv6 hosts. | 1d |
| 8 | Testing matrix — Cast Audio (discontinued but still in homes), Nest Mini, Nest Hub, Sony / LG Cast-built-in TVs, Chromecast Ultra. Need physical access or borrowed hardware. | 2d |
| 9 | Settings persistence — last-used Cast device, auto-reconnect on launch (opt-in). | 0.5d |
| 10 | Docs — README section + a one-line release note. | 0.5d |

**Total: ~10–11 days focused work.**

## Risks

- **Network reachability.** A Cast device on a guest VLAN can see
  Tideway via mDNS but can't fetch the HTTP URL. We surface the
  HTTP URL the device is being asked to fetch in the diagnostic
  log so this is debuggable, not silent.
- **Codec ceiling on cheap Cast targets.** Some receivers reject
  high-rate FLAC. Fall back to 16/44.1 if the device's media
  controller status reports a load error and we'll attribute it
  in the toast.
- **Multiple controllers.** The user's phone can target the same
  Cast device. We don't try to "own" the session — if a phone
  takes over, we surface the state change and stop sending updates
  until the user re-selects in our picker. Same model as Spotify,
  Apple Music etc.

## Won't do in this pass

- Multi-room (cast to a Cast group of speakers) — pychromecast
  supports it but we're scoping single-device first.
- Cast volume sync with system volume.
- Sender from the mini-player window. Picker lives only in the
  main window's now-playing for v1.

---

# Part 2 — Tidal Connect (controller)

## What we're building

Same UX as Cast — picker in the now-playing bar, list of Tidal
Connect devices on the LAN, pick one, audio plays on that device.
Difference is in what happens under the hood: we don't stream
Tideway's audio to the device. We tell the device which Tidal
track to play, and the device fetches the audio directly from
Tidal's CDN using its own paired session.

The Tidal desktop app does exactly this. We're matching that, in a
narrowly defined "control plane only" capacity.

## Why this is harder than Cast

Cast is an open ecosystem with a documented protocol and a mature
Python library. Tidal Connect is closed. There's no SDK, no spec,
no library that implements the controller side. We'd need to
reverse-engineer the protocol enough to act as a controller.

Two unknowns the design doc on `feature/cast-airplay` already
flagged:

1. **What discovery and control protocol does Tidal Connect actually
   speak on the LAN?** Believed to be OpenHome (UPnP-derived) over
   mDNS, but we haven't packet-captured it to confirm. Without
   that, this whole project is speculation.
2. **How does the device authenticate to Tidal's CDN?** Two
   hypotheses:
   - **Hypothesis A:** Tideway hands the device a stream URL with
     auth tokens already embedded (signed URL, time-limited). Same
     URL Tideway's player uses for local playback. Device fetches
     it like any other HTTPS URL.
   - **Hypothesis B:** The device has its own paired Tidal
     session and Tideway just hands it a track ID. Device
     authenticates independently using its own credentials.

Hypothesis A is the "good case" — we have those URLs already from
tidalapi, the code change is mostly piping them out via OpenHome
SOAP. Hypothesis B is where we'd hit a wall: pairing a third-party
Mac app as a Tidal Connect "device session" probably requires
partner credentials we don't have.

The first phase of this project is one job: **answer that question
with a packet capture.** If it's A, we proceed. If it's B, we
likely stop — the only Path B implementation that doesn't hit the
ban-risk problem from the existing design doc is one we can't
reach.

## Tidal-app parity check

The Tidal desktop app does have Connect support — we'd be matching
parity. Bar to clear is the same as Cast: discover the same
devices, maintain a control session through the same lifecycle.

## Architecture

`PCMPlayer` is bypassed entirely when Connect is active. The
"Connect" sink is structurally different from `LocalSink` /
`CastSink` because no PCM is generated — Tideway's role is just
control:

- `ConnectSession` (new) — discovery, pairing handshake (TBD by
  protocol scoping), SOAP control commands.
- When Connect is active: `PCMPlayer.pause()` and stop the
  decoder. The local audio engine is idle. Now-playing UI reads
  state from the Connect device's status feedback instead of from
  PCMPlayer.
- Track change: issue `Insert` (or whatever the protocol uses)
  with the new track's stream URL.
- User pause / resume / seek in Tideway → SOAP commands to device.
- Device-side state changes (paused on the device, skipped via
  device's own buttons) → SOAP events back to Tideway → reflect in
  UI.

This is a meaningful refactor of how the now-playing UI gets its
state. Today it's wired straight from `PCMPlayer`'s broadcast.
With Connect, the broadcast comes from a Connect device's status.

## Phases

### Phase 1 — protocol scoping (3–5 days)

No code commits to Tideway. Output is one short doc that answers:
which OpenHome service descriptors does a Tidal Connect device
expose, what SOAP commands does the official Tidal desktop app
issue at it, and which of the two auth hypotheses is reality.

Tools: `tcpdump` on a Mac with the Tidal desktop app + a real
Connect device on the LAN. Wireshark for SOAP body inspection.
Optionally `mitmproxy` if the control plane is HTTPS (it probably
is) — only viable if Tidal's desktop app doesn't pin certs, worth
testing once at the start.

**Phase 1 gate:** if hypothesis B is reality, stop the project.
Document why. The work in phase 2 / 3 is wasted if we can't get
auth right.

### Phase 2 — discovery + SOAP plumbing (1 week)

- mDNS browsing for whatever service type Connect uses (TBD by
  Phase 1, likely a Tidal-specific subtype on top of OpenHome).
- HTTP client for the OpenHome service descriptor XML.
- SOAP body builder + HTTPX client for the control commands.
- A `ConnectSession` skeleton with: connect, send Play, send
  Pause, send Seek, subscribe to status events. Doesn't have to
  actually play audio yet — just exchange messages with the device
  and log them.

**Phase 2 milestone:** `ConnectSession.connect("Bluesound Node")`
returns; subsequent SOAP calls don't error; we can observe the
device respond.

### Phase 3 — track handoff (1–2 weeks)

- Generate the stream URL via tidalapi. Same code path the local
  player uses, with whatever metadata the OpenHome `Insert`
  command requires.
- Hand it to the device, watch it fetch and play.
- Track-end handling: device fires an "EndOfTrack" event, our
  controller advances the queue and issues the next `Insert`.
- Seek: round-trip a SOAP `SeekSecond` and verify the device
  honors it.
- Multi-track gapless: if the device supports a "next track" hint
  (most OpenHome implementations do), pre-load via that. Otherwise
  fall back to insert-on-end.

**Risk window**: this is the variable part. If hypothesis A holds,
this phase is mostly mechanical SOAP plumbing and 1 week. If we
discover the device rejects our stream URLs (auth piece is
trickier than expected), we may not be able to finish.

### Phase 4 — UI integration (3–5 days)

- Reuse the picker UI built for Cast.
- Now-playing reads from `ConnectSession`'s status broadcast when
  active.
- Status indicator distinguishes Local / Cast / Connect to the
  user.
- Settings: "Show Tidal Connect devices" opt-in toggle. Default
  off; we surface a one-line note about the third-party-protocol
  caveat.

### Phase 5 — testing (3–5 days)

A real Tidal Connect device is required. Bluesound Node is the
canonical low-cost option ($550); a borrowed Linn or Cambridge
unit also works. We'd buy / borrow at the start of Phase 2 so
we're not waiting on hardware mid-project.

Test matrix:
- Single device, basic playback control.
- Track change.
- Seek.
- Phone Tidal app interrupting mid-session (does our controller
  cleanly hand off?).
- Network partition (Wi-Fi drops then comes back).
- Tidal session expiry mid-playback.

## Total estimate

**3–4 weeks of focused work**, with the Phase 1 gate determining
whether the rest happens at all. Maintenance commitment after
launch: a few days per quarter as Tidal rotates protocol elements
or partner-side device firmware changes break assumptions.

## What we explicitly are NOT building

- **Tideway as a Tidal Connect target** (phone Tidal app picks
  Tideway from its device list). That's the path the existing
  design doc on `feature/cast-airplay` covered as Path B. It has
  a real account-ban risk because we'd be pretending to be a
  partner-licensed device. Out of scope for this project.
- **AirPlay sender or receiver.** Existing `airplay.py`
  scaffolding stays as-is until a future release.
- **Multi-room synchronized playback.** Cast and Connect both have
  protocol features for it; not part of v1.

---

## Sequencing recommendation

Ship Cast first. It's well-defined, library-supported, ~2 weeks of
work, and gets users a real feature on a known schedule.

Run Tidal Connect Phase 1 (protocol scoping) in parallel, on a
side branch. The output is a doc, not code, so it doesn't slow
Cast down. End of Phase 1 we have a yes/no on whether the rest of
Connect is buildable. If yes, we plan its release. If no, we ship
Cast as the answer for "play to LAN devices," document why
Connect didn't happen, and move on.
