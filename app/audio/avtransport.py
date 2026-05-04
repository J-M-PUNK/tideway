"""UPnP AVTransport + RenderingControl SOAP wrappers.

The DLNA MediaRenderer profile that WiiM, Bluesound, Cambridge,
LG / Samsung TVs, and almost every cheap network streamer
implements is a small set of SOAP actions over two UPnP services:

  AVTransport:1
    SetAVTransportURI: point the device at our HTTP stream URL
    Play / Pause / Stop: transport control
    Seek: relative time offset within the stream
    GetTransportInfo: current state (PLAYING, PAUSED_PLAYBACK, ...)
    GetPositionInfo: RelTime, TrackDuration

  RenderingControl:1 (optional, not all renderers expose):
    SetVolume / GetVolume / SetMute / GetMute

The SOAP machinery lives in `app/audio/openhome.py` despite the
file name. That module's `invoke()`, `_build_soap_envelope`, and
`OpenHomeService` predate this work but are generic UPnP-SOAP
helpers. They don't depend on anything OpenHome-specific. We
import them directly here rather than duplicate the envelope
encoder, and don't rename the openhome module because the existing
controllers (Playlist, Volume, Time, Info) ARE OpenHome-specific
and that's the right name for them.

This file is the AVTransport-flavored equivalent: thin typed
wrappers around `invoke()` so the manager can call
`controller.play()` instead of building SOAP args inline. Argument
serialization (InstanceID="0", duration formatting, etc.) is
encapsulated here so the manager stays focused on session
lifecycle and audio plumbing.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.audio.openhome import (
    OpenHomeDevice,
    OpenHomeService,
    invoke,
)

log = logging.getLogger(__name__)


# UPnP-A/V service-type URNs. Versions vary across devices (some
# older Sonos hardware advertises AVTransport:1, newer devices may
# expose :2 or :3 as well). We match the prefix and accept any
# version. The actions we use exist in every published version.
_AVTRANSPORT_PREFIX = "urn:schemas-upnp-org:service:AVTransport:"
_RENDERING_CONTROL_PREFIX = "urn:schemas-upnp-org:service:RenderingControl:"

# AVTransport's SetAVTransportURI / Play / Pause / Stop all take an
# InstanceID. The spec lets a single device own multiple transport
# instances (think a multi-zone amp), but every consumer renderer
# we'll see treats InstanceID="0" as the only instance. Hardcoding
# "0" everywhere is fine. If we ever target multi-zone gear it
# becomes a per-session attribute.
_INSTANCE_ID = "0"


def _find_service(
    device: OpenHomeDevice, prefix: str,
) -> Optional[OpenHomeService]:
    """Return the first service on `device` whose service_type
    starts with `prefix`. Lets us match any version of the service
    URN without forcing the caller to know which the device
    advertises."""
    for svc in device.services:
        if svc.service_type.startswith(prefix):
            return svc
    return None


# ---------------------------------------------------------------------
# AVTransport
# ---------------------------------------------------------------------


class AVTransportController:
    """Thin wrapper over the AVTransport service. One instance per
    active DLNA session; manager builds it after `fetch_device()`.

    Methods raise the same `OpenHomeSOAPError` / `RuntimeError` that
    `invoke()` raises. The manager treats both as "device rejected
    the action" and tears down the session.
    """

    def __init__(self, service: OpenHomeService) -> None:
        self._service = service

    @classmethod
    def from_device(
        cls, device: OpenHomeDevice,
    ) -> Optional["AVTransportController"]:
        """Build a controller from a parsed device, or return None
        if the device doesn't advertise AVTransport. In that case
        we can't drive playback against it through this protocol,
        and the manager should reject the connect."""
        svc = _find_service(device, _AVTRANSPORT_PREFIX)
        if svc is None:
            return None
        return cls(svc)

    @property
    def service_type(self) -> str:
        return self._service.service_type

    def set_av_transport_uri(self, uri: str, didl_metadata: str) -> None:
        """Point the device at a stream URL.

        `uri` is the full http://host:port/path our embedded stream
        server exposes. `didl_metadata` is a DIDL-Lite XML document
        describing the track (built by `openhome.build_didl_lite`);
        many devices will refuse SetAVTransportURI without one, or
        accept it but show "Unknown Track" on their display.

        After this call the device is in TRANSITIONING or STOPPED;
        a subsequent `play()` is what actually starts the pull.
        """
        invoke(
            self._service,
            "SetAVTransportURI",
            {
                "InstanceID": _INSTANCE_ID,
                "CurrentURI": uri,
                "CurrentURIMetaData": didl_metadata,
            },
        )

    def play(self, speed: str = "1") -> None:
        """Begin (or resume) playback. Speed="1" is normal forward;
        the spec allows "2", "1/2", etc. but no consumer renderer
        actually honors them, so we don't expose the parameter."""
        invoke(
            self._service,
            "Play",
            {"InstanceID": _INSTANCE_ID, "Speed": speed},
        )

    def pause(self) -> None:
        """Pause playback. Some renderers reject Pause from the
        STOPPED state with UPnP error 701 (Transition Not
        Available); the manager catches that and turns it into a
        no-op. Pause from PLAYING moves to PAUSED_PLAYBACK."""
        invoke(self._service, "Pause", {"InstanceID": _INSTANCE_ID})

    def stop(self) -> None:
        """Stop playback. Tears down the active stream resource on
        most renderers; the device drops its HTTP connection to our
        embedded server, which is how the encoder feeds drain
        cleanly when a session ends."""
        invoke(self._service, "Stop", {"InstanceID": _INSTANCE_ID})

    def seek(self, position_s: int) -> None:
        """Seek to an absolute offset within the current track,
        formatted as REL_TIME (HH:MM:SS).

        AVTransport's seek modes vary by device; REL_TIME is the
        only one universally supported across the renderers we
        care about. ABS_TIME is technically what we want (offset
        from start of stream) but devices often map both modes to
        the same internal clock, and REL_TIME is more widely
        accepted in practice."""
        h = max(0, position_s) // 3600
        m = (max(0, position_s) % 3600) // 60
        s = max(0, position_s) % 60
        invoke(
            self._service,
            "Seek",
            {
                "InstanceID": _INSTANCE_ID,
                "Unit": "REL_TIME",
                "Target": f"{h:02d}:{m:02d}:{s:02d}",
            },
        )

    def get_transport_info(self) -> dict[str, str]:
        """Return the current transport state as a dict with at
        least `CurrentTransportState` (PLAYING / PAUSED_PLAYBACK /
        STOPPED / TRANSITIONING / NO_MEDIA_PRESENT). Other fields
        the device may include (CurrentTransportStatus,
        CurrentSpeed) pass through."""
        return invoke(
            self._service,
            "GetTransportInfo",
            {"InstanceID": _INSTANCE_ID},
        )

    def get_position_info(self) -> dict[str, str]:
        """Return current position as a dict. Useful keys:
        `RelTime` (HH:MM:SS), `TrackDuration` (HH:MM:SS), `Track`
        (queue index, usually "1"). Devices that don't track
        position internally return "00:00:00" or "NOT_IMPLEMENTED";
        the manager treats both as "position unknown."""
        return invoke(
            self._service,
            "GetPositionInfo",
            {"InstanceID": _INSTANCE_ID},
        )


# ---------------------------------------------------------------------
# RenderingControl (volume / mute)
# ---------------------------------------------------------------------


class RenderingControlController:
    """Optional wrapper around RenderingControl. Many renderers
    expose this; some don't, and a few only honor SetMute (no
    volume). Manager builds an instance lazily, or None if the
    device doesn't expose the service. In the None case
    volume/mute become no-ops at the session level.
    """

    def __init__(self, service: OpenHomeService) -> None:
        self._service = service

    @classmethod
    def from_device(
        cls, device: OpenHomeDevice,
    ) -> Optional["RenderingControlController"]:
        svc = _find_service(device, _RENDERING_CONTROL_PREFIX)
        if svc is None:
            return None
        return cls(svc)

    @property
    def service_type(self) -> str:
        return self._service.service_type

    def set_volume(self, level_percent: int) -> None:
        """Set master-channel volume to a 0-100 percentage. The
        UPnP spec says the value is "in the device's native
        range" but Channel="Master" with a 0-100 value is what
        every consumer renderer accepts; the spec's "absolute
        units" model is mostly theoretical."""
        clamped = max(0, min(100, int(level_percent)))
        invoke(
            self._service,
            "SetVolume",
            {
                "InstanceID": _INSTANCE_ID,
                "Channel": "Master",
                "DesiredVolume": str(clamped),
            },
        )

    def get_volume(self) -> int:
        out = invoke(
            self._service,
            "GetVolume",
            {"InstanceID": _INSTANCE_ID, "Channel": "Master"},
        )
        try:
            return int(out.get("CurrentVolume", "0"))
        except ValueError:
            return 0

    def set_mute(self, muted: bool) -> None:
        invoke(
            self._service,
            "SetMute",
            {
                "InstanceID": _INSTANCE_ID,
                "Channel": "Master",
                "DesiredMute": "1" if muted else "0",
            },
        )

    def get_mute(self) -> bool:
        out = invoke(
            self._service,
            "GetMute",
            {"InstanceID": _INSTANCE_ID, "Channel": "Master"},
        )
        return out.get("CurrentMute", "0") in ("1", "true", "True")
