# UPnP / DLNA renderer output

This doc covers Tideway's UPnP/DLNA MediaRenderer output path:
what it does, what kinds of devices it works with, how to use it
from the picker, and the known gaps.

## What this is

DLNA's `MediaRenderer` profile is the universal audio-output
target across consumer network gear. Almost every streamer that
isn't a certified Tidal Connect device or a Chromecast speaks
this protocol. That includes:

- **WiiM** (Pro, Pro Plus, Ultra, Mini, Amp)
- **Bluesound** (newer Node and Pulse models that haven't been
  pulled into Tidal Connect)
- **Cambridge Audio** streamers and CXN-class units
- **NAD** networked players
- **Most network AVRs** — Yamaha, Denon, Pioneer
- **LG and Samsung TVs** (the audio-input side of their network
  stack)
- **A long tail** of cheaper Hi-Fi network bridges and
  add-on streamers

Tideway encodes its decoded PCM to FLAC inside the app, serves
that stream over an HTTP endpoint reachable from your LAN, and
tells the renderer to fetch from that URL using UPnP SOAP. The
renderer pulls; Tideway keeps encoding. Same shape as the
Chromecast path, different control protocol.

## How it differs from Cast and Tidal Connect

Three output protocols that sound similar at first glance, but
they're architecturally distinct:

- **Chromecast.** Tideway encodes PCM, hands the device a stream
  URL, the device fetches and plays. Tideway is the audio source.
  Used for Google Home speakers, Cast-built-in TVs, Nest Mini,
  Chromecast Audio.
- **Tidal Connect.** Tideway is just a remote control. The device
  has its own paired Tidal session and pulls audio directly from
  Tidal's CDN. Tideway sends a track ID and transport commands.
  Used for Linn, Naim, some older Bluesound. Requires the device
  to be a certified Tidal Connect endpoint.
- **DLNA (this doc).** Tideway encodes PCM, hands the device a
  stream URL via UPnP SOAP, the device fetches and plays. Same
  audio-source shape as Cast, different control protocol.

If a device shows up in two of these (some Bluesound models do),
the picker shows it in both sections; pick the one that gives
you the best behaviour with that hardware. Tidal Connect tends
to give the best result on supported devices because audio comes
from Tidal directly, but DLNA's universality means it'll usually
work even if the others don't.

## Using it from the picker

Open the **Output device picker** (sound icon in the now-playing
bar). The dropdown lists local outputs first, then sections for
Cast, Tidal Connect, and DLNA renderers it found on the LAN.

DLNA discovery is via SSDP (multicast UDP). When you open the
picker, Tideway issues a fresh discovery and waits for results
before populating the list, so you see populated devices when
the dropdown opens rather than an empty list that fills in
moments later. If a device you expect doesn't show up, it's
usually one of:

- Device on a different VLAN than your laptop
- Device's UPnP / DLNA service disabled in its own settings
- Multicast filtering on a managed switch in between
- Device hasn't fully booted yet (some take 30-60s after power-on
  to advertise)

There's an explicit refresh button if you want to retrigger the
scan without closing the picker.

## What pause and skip do

Tideway routes user transport actions through to the device via
AVTransport SOAP:

- Pause / resume from Tideway → SOAP `Pause` / `Play` to the
  device. The device reacts within a second or so rather than
  taking ~8s to drain its own buffer (which is what happens if
  you only pause Tideway's encoder side without telling the
  device).
- Stop → SOAP `Stop`. The device clears its current URI.
- Track change → Tideway hands the device a fresh stream URL via
  `SetAVTransportURI` and a `Play` command.
- Volume controls in Tideway map to the device's
  `RenderingControl.SetVolume` if the device exposes that
  service. Devices without RenderingControl (some bare-bones
  bridges) ignore the volume command and the slider in Tideway
  has no effect; use the device's own volume control in that
  case.

## Gapless track changes

Each track is served as a bounded FLAC file — the real track, with its
real length, not an open-ended stream. At the end of a track the
renderer has to move on to the next file. Tideway picks between two
mechanisms:

- **Renderers that advertise `SetNextAVTransportURI`** get the next
  track pre-staged while the current one still plays. The renderer
  advances on its own at the natural end, so there is no decoder
  re-init and no audible gap.
- **Renderers that don't** get a fresh `SetAVTransportURI` at the
  boundary. Tideway polls the renderer's own playback position
  (`GetPositionInfo`) and drives the switch when the renderer — not the
  desktop decoder, whose muted stream finishes ahead of the renderer's
  buffer — reaches the end. That avoids cutting the previous track's
  tail; the renderer still re-initialises its decoder, so a small gap
  can remain.

The position poll only times the advance. A pause pressed on the
device's own remote is still not reflected in the Tideway UI (see
below).

## Known limits

- **No hardware verification by the maintainer.** The unit tests
  cover SOAP-arg shapes, response parsing, session lifecycle,
  and audio plumbing, but the maintainer doesn't have a WiiM /
  Bluesound / Cambridge unit on the bench. Device-side behaviour
  is unverified. File a GitHub issue if your specific renderer
  misbehaves.
- **Device-initiated transport changes aren't shown.** Tideway polls
  the renderer's position to time gapless track changes, but it doesn't
  reflect a pause, resume, or stop pressed on the device's own remote
  (or its mobile app) in the Tideway UI. Same gap the Cast path has.
- **No multi-room.** Some renderers (Bluesound BluOS, Sonos via
  their DLNA-bridge mode) support synchronised playback across
  multiple speakers. Tideway sends to one renderer at a time.
- **Codec ceiling.** The encoder ships hi-res FLAC at the
  source's native rate. Some cheaper renderers can't decode
  above 24/96 and will refuse the stream or fall back to a lower
  rate. Same trade-off as the Cast path.
- **Networks that block multicast.** Discovery uses SSDP, which
  is multicast UDP. Guest VLANs and locked-down corporate
  networks routinely drop it. Tideway can see a device that's
  visible to it via mDNS but can't reach the HTTP URL Tideway
  hands it, and we surface that URL in the diagnostic log so
  you can debug it from the device's side.
