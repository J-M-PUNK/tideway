"""OpenHome service descriptors + SOAP control client.

OpenHome (linn-co-uk / av-openhome-org) is the UPnP-derived control
protocol that Tidal Connect targets implement. Every Bluesound,
Linn, NAD, Cambridge, etc. unit on a LAN exposes:

  - A root device description XML at the LOCATION URL we got from
    SSDP. Lists the device's friendly name, manufacturer, model, and
    every service it implements.
  - For each service, a separate SCPD (Service Control Protocol
    Description) XML that lists the SOAP-callable actions, their
    arguments, and the related state variables.

This module turns those XMLs into typed Python objects (slice 1)
AND issues SOAP control calls against them (slice 2). The SOAP
layer is intentionally generic — it knows how to send any OpenHome
action and parse the response, but doesn't know what Playlist or
Volume mean. Slice 3 builds service-specific wrappers on top.

Why one module for both: descriptor parsing is mostly there to feed
the SOAP layer (controlURL + action argument lists), and the SOAP
layer's natural input type is the OpenHomeService dataclass the
descriptor parser produces. Splitting the file would just move
imports around.

Why separate from `tidal_connect.py`: OpenHome is a generic protocol
useful for any UPnP-AV controller. Keeping it independent lets it
be reused if the Tidal-specific track-handoff in slice 4 turns out
to need adjustments and we want plain-OpenHome control as a
fallback.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

log = logging.getLogger(__name__)


# UPnP / OpenHome XML namespaces. Most parsers blow up when the
# namespace isn't declared explicitly because elementtree won't match
# tags that have a prefix. We use the wildcard-namespace trick when
# searching: `{*}elementName` matches the element regardless of prefix.
_NS_DEVICE = "urn:schemas-upnp-org:device-1-0"
_NS_SCPD = "urn:schemas-upnp-org:service-1-0"


@dataclass(frozen=True)
class OpenHomeArgument:
    """One in / out argument of a SOAP action.

    `related_state_variable` is the SCPD-declared state variable the
    argument's type comes from. Slice 2's SOAP client uses this to
    serialize / deserialize argument values, since the action
    declaration only carries a name and direction, not a type.
    """

    name: str
    direction: str  # "in" or "out"
    related_state_variable: str


@dataclass(frozen=True)
class OpenHomeAction:
    """One callable action on a service. e.g. `Playlist.Insert`,
    `Volume.SetVolume`, `Time.Time` (no args, returns track time)."""

    name: str
    arguments: tuple[OpenHomeArgument, ...]

    @property
    def in_arguments(self) -> tuple[OpenHomeArgument, ...]:
        return tuple(a for a in self.arguments if a.direction == "in")

    @property
    def out_arguments(self) -> tuple[OpenHomeArgument, ...]:
        return tuple(a for a in self.arguments if a.direction == "out")


@dataclass(frozen=True)
class OpenHomeService:
    """One OpenHome service exposed by a device.

    `service_type` is the full URN (e.g. `urn:av-openhome-org:service:
    Playlist:1`). `short_name` is the trailing segment ("Playlist") —
    used by the SOAP client and the wrappers in slice 3 to look up
    services by friendly name. `control_url` and `event_sub_url` are
    fully resolved against the device's base URL so callers can hit
    them directly without re-doing the URL math.
    """

    service_type: str
    service_id: str
    short_name: str
    control_url: str
    event_sub_url: str
    scpd_url: str
    actions: tuple[OpenHomeAction, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OpenHomeDevice:
    """Parsed root device description.

    `services` is the list of services we found, with their SCPD
    actions populated when fetched. Slice 2 caches devices and
    indexes services by `short_name` for quick `get_service('Playlist')`
    lookups.
    """

    udn: str
    friendly_name: str
    manufacturer: str
    model_name: str
    model_number: str
    services: tuple[OpenHomeService, ...]

    def get_service(self, short_name: str) -> Optional[OpenHomeService]:
        """Look up a service by its short name (case-insensitive).
        Returns None if the device doesn't expose that service.
        Slice 3's wrappers all start with this lookup so a missing
        service surfaces as a clean None rather than an exception
        deep inside a SOAP call."""
        target = short_name.lower()
        for s in self.services:
            if s.short_name.lower() == target:
                return s
        return None


def parse_device_description(
    xml: str,
    base_url: str,
) -> OpenHomeDevice:
    """Parse a root UPnP device description XML.

    `base_url` is the URL we fetched the XML from. Used to resolve
    relative URLs (controlURL, SCPDURL, eventSubURL) declared inside
    the XML — UPnP doesn't require these to be absolute, and many
    OpenHome devices return paths like `/Playlist/control` that need
    to be joined against the device's host:port.

    Raises ValueError if the XML can't be parsed or doesn't have the
    minimum structure (root element + at least one service). Empty
    services list is fine — some devices answer SSDP without
    declaring any services in their root XML, which is unusual but
    not malformed.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"device description XML invalid: {exc}") from exc

    # `device` element. UPnP root devices wrap the device entry in a
    # top-level <root> with an inner <device>; some embedded devices
    # nest deeper. Walk to find the first <device> regardless.
    device_el = _find(root, "device")
    if device_el is None:
        raise ValueError("device description: no <device> element")

    services: list[OpenHomeService] = []
    service_list = _find(device_el, "serviceList")
    if service_list is not None:
        for svc_el in _findall(service_list, "service"):
            svc = _parse_service_element(svc_el, base_url)
            if svc is not None:
                services.append(svc)

    return OpenHomeDevice(
        udn=_text(_find(device_el, "UDN")),
        friendly_name=_text(_find(device_el, "friendlyName")),
        manufacturer=_text(_find(device_el, "manufacturer")),
        model_name=_text(_find(device_el, "modelName")),
        model_number=_text(_find(device_el, "modelNumber")),
        services=tuple(services),
    )


def parse_scpd(xml: str) -> tuple[OpenHomeAction, ...]:
    """Parse a Service Control Protocol Description XML.

    Returns the action list with arguments populated. Action order
    matches the XML's order, which is conventionally how OpenHome
    spec docs present services — useful when comparing our parsed
    output to the published spec.

    Empty action list is valid (rare, but a service can declare zero
    actions if it's eventing-only). Returns an empty tuple in that
    case rather than raising.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"SCPD XML invalid: {exc}") from exc

    actions: list[OpenHomeAction] = []
    action_list = _find(root, "actionList")
    if action_list is None:
        return ()
    for action_el in _findall(action_list, "action"):
        name = _text(_find(action_el, "name"))
        if not name:
            continue
        arguments: list[OpenHomeArgument] = []
        argument_list = _find(action_el, "argumentList")
        if argument_list is not None:
            for arg_el in _findall(argument_list, "argument"):
                arguments.append(
                    OpenHomeArgument(
                        name=_text(_find(arg_el, "name")),
                        direction=_text(_find(arg_el, "direction")),
                        related_state_variable=_text(
                            _find(arg_el, "relatedStateVariable")
                        ),
                    )
                )
        actions.append(
            OpenHomeAction(name=name, arguments=tuple(arguments))
        )
    return tuple(actions)


def fetch_device(
    location: str,
    timeout: float = 10.0,
    *,
    fetch_scpds: bool = True,
) -> OpenHomeDevice:
    """Fetch a device's root description and (optionally) every
    service's SCPD, returning a fully populated OpenHomeDevice.

    `location` is the URL SSDP gave us (the LOCATION header). Network
    errors propagate as RuntimeError so callers don't have to catch
    every requests-specific exception class — that's the contract the
    rest of the audio stack uses for HTTP utilities.

    `fetch_scpds=False` returns the device with empty action lists
    on each service. Useful when the caller only needs friendly_name
    + service URL list (e.g. for the picker UI) and doesn't want to
    pay the per-service round trip.
    """
    import requests  # local import keeps module-load cheap

    try:
        resp = requests.get(location, timeout=timeout)
        resp.raise_for_status()
    except Exception as exc:
        raise RuntimeError(
            f"failed to fetch device description from {location}: {exc}"
        ) from exc

    device = parse_device_description(resp.text, location)
    if not fetch_scpds:
        return device

    enriched_services: list[OpenHomeService] = []
    for svc in device.services:
        try:
            scpd_resp = requests.get(svc.scpd_url, timeout=timeout)
            scpd_resp.raise_for_status()
            actions = parse_scpd(scpd_resp.text)
        except Exception as exc:
            # An unreadable SCPD on one service shouldn't tank the
            # whole device — fall back to an empty action list so
            # callers can still target the OTHER services. Logged at
            # debug rather than warning because some manufacturers
            # gate certain services behind auth and we'd rather not
            # be noisy about them.
            log.debug(
                "openhome: scpd fetch for %s failed: %r",
                svc.scpd_url,
                exc,
            )
            actions = ()
        enriched_services.append(
            OpenHomeService(
                service_type=svc.service_type,
                service_id=svc.service_id,
                short_name=svc.short_name,
                control_url=svc.control_url,
                event_sub_url=svc.event_sub_url,
                scpd_url=svc.scpd_url,
                actions=actions,
            )
        )
    return OpenHomeDevice(
        udn=device.udn,
        friendly_name=device.friendly_name,
        manufacturer=device.manufacturer,
        model_name=device.model_name,
        model_number=device.model_number,
        services=tuple(enriched_services),
    )


# ---------------------------------------------------------------------
# SOAP action client (slice 2)
# ---------------------------------------------------------------------


class OpenHomeSOAPError(RuntimeError):
    """Raised when a SOAP call returns a UPnP fault.

    `code` is the UPnP errorCode (a small integer with documented
    semantics — 401 Invalid Action, 402 Invalid Args, 501 Action
    Failed, etc.). `description` is the human-readable string the
    device sent. `action` and `service_type` are echoed for log
    clarity when many calls are in flight.
    """

    def __init__(
        self,
        code: int,
        description: str,
        *,
        action: str = "",
        service_type: str = "",
    ) -> None:
        self.code = code
        self.description = description
        self.action = action
        self.service_type = service_type
        super().__init__(
            f"UPnP {code} {description!r}"
            f"{f' on {service_type}#{action}' if service_type else ''}"
        )


def invoke(
    service: OpenHomeService,
    action_name: str,
    args: Optional[dict[str, str]] = None,
    *,
    timeout: float = 10.0,
) -> dict[str, str]:
    """Issue a SOAP action against a service. Returns the out-args
    as a name -> string dict.

    `args` is a name -> stringified-value dict. Caller is responsible
    for converting non-string types (numbers, durations) to the right
    string form per the OpenHome spec — slice 3's wrappers handle the
    type-aware conversion for specific services.

    Raises OpenHomeSOAPError if the device returns a UPnP fault.
    Raises RuntimeError for transport failures (connection refused,
    timeout, malformed XML response). Both are catchable separately
    so callers can distinguish "device rejected the action" from
    "couldn't reach the device."
    """
    import requests  # local import keeps module-load cheap

    envelope = _build_soap_envelope(
        service.service_type, action_name, args or {}
    )
    soap_action = f'"{service.service_type}#{action_name}"'
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": soap_action,
        # Some OpenHome firmwares are picky about Connection: close.
        # The default keep-alive sometimes leaves the device's
        # response buffer half-full; closing per request avoids the
        # variant of that bug we'd otherwise have to chase later.
        "Connection": "close",
    }

    try:
        resp = requests.post(
            service.control_url,
            data=envelope.encode("utf-8"),
            headers=headers,
            timeout=timeout,
        )
    except Exception as exc:
        raise RuntimeError(
            f"SOAP transport to {service.control_url} failed: {exc}"
        ) from exc

    body = resp.text or ""
    # 500 with a SOAP body is the canonical UPnP fault response.
    # Parse the body to get the structured error rather than
    # surfacing a bare 'HTTP 500' to the caller. Some devices return
    # 200 even on errors though, so check body shape regardless of
    # status code.
    fault = _parse_soap_fault(body)
    if fault is not None:
        code, description = fault
        raise OpenHomeSOAPError(
            code,
            description,
            action=action_name,
            service_type=service.service_type,
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"SOAP HTTP {resp.status_code} from {service.control_url}: "
            f"{body[:200]}"
        )
    return _parse_soap_response(body, action_name)


# ---------------------------------------------------------------------
# SOAP helpers (encoding + parsing)
# ---------------------------------------------------------------------


def _xml_escape(value: str) -> str:
    """Minimal XML text escape for SOAP body argument values. We
    don't use `xml.sax.saxutils.escape` because that's missing some
    edge cases (the apostrophe doesn't need escaping in element
    text, but quotes don't either, and we want to keep the function
    explicit so tests can pin behaviour). Five-char rule covers
    everything that matters in element-text context."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _build_soap_envelope(
    service_type: str,
    action_name: str,
    args: dict[str, str],
) -> str:
    """Construct the SOAP envelope sent in the POST body.

    Hand-rolled rather than using a SOAP library because UPnP's SOAP
    profile is small and predictable, and external libraries
    (suds-jurko, zeep) all carry surprises around namespace
    resolution and WSDL generation that don't apply here. Slice 4
    will pass DIDL-Lite XML as a Metadata argument value, which
    means we have to XML-escape it — handled by `_xml_escape` below.
    """
    arg_xml = "".join(
        f"<{name}>{_xml_escape(value)}</{name}>"
        for name, value in args.items()
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action_name} xmlns:u="{service_type}">'
        f"{arg_xml}"
        f"</u:{action_name}>"
        "</s:Body>"
        "</s:Envelope>"
    )


def _parse_soap_response(body: str, action_name: str) -> dict[str, str]:
    """Pull out-args out of a successful SOAP response. The wrapper
    element is named `<ActionName>Response` per UPnP convention; its
    children are the out-arguments as text-content elements.

    Returns an empty dict for actions that have no out-args (e.g.
    `Playlist.Play`). Raises RuntimeError if the body isn't a
    parseable SOAP response — that's a malformed device, not a
    UPnP-spec fault."""
    if not body:
        return {}
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise RuntimeError(f"SOAP response not parseable: {exc}") from exc
    body_el = _find(root, "Body")
    if body_el is None:
        raise RuntimeError("SOAP response missing Body element")
    response_el = _find(body_el, f"{action_name}Response")
    if response_el is None:
        # Some devices skip the *Response wrapper. Walk the body's
        # immediate children to find a single child element and
        # treat its children as the out-args. Conservative — most
        # spec-compliant devices use the wrapper.
        children = list(body_el)
        if not children:
            return {}
        response_el = children[0]
    return {child.tag.split("}")[-1]: (child.text or "") for child in response_el}


def _parse_soap_fault(body: str) -> Optional[tuple[int, str]]:
    """Return (errorCode, errorDescription) if the body is a UPnP
    fault, else None. UPnP wraps the device-specific error inside
    `<s:Fault>/<detail>/<UPnPError>` — both the s: and the inner
    namespace can vary by encoder, so we use the wildcard tag match
    consistently with the rest of the parser."""
    if not body:
        return None
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    body_el = _find(root, "Body")
    if body_el is None:
        return None
    fault = _find(body_el, "Fault")
    if fault is None:
        return None
    detail = _find(fault, "detail")
    if detail is None:
        return None
    upnp_err = _find(detail, "UPnPError")
    if upnp_err is None:
        return None
    code_text = _text(_find(upnp_err, "errorCode"))
    desc_text = _text(_find(upnp_err, "errorDescription")) or "(no description)"
    try:
        code = int(code_text) if code_text else 0
    except ValueError:
        code = 0
    return (code, desc_text)


# ---------------------------------------------------------------------
# Service-specific controllers (slice 3)
#
# Type-safe wrappers around `invoke()` for the services Tidal Connect
# track-handoff (slice 4) actually needs: Playlist (load + play / pause
# / next / seek), Volume (level + mute), Time (position + duration
# polling), Info (current-track metadata read-back). Each controller
# is a thin object holding one OpenHomeService; methods translate
# Python-native types to the strings the SOAP envelope wants and
# parse out-args back into types the rest of the audio stack expects.
#
# Why classes instead of free functions: the action name + service
# argument always go together, so a class that captures both lets
# callers write `playlist.play()` instead of repeating the service
# every line. Also lets `from_device` factory methods cleanly return
# None when a device doesn't expose the service.
#
# Not all OpenHome actions are wrapped here — only the ones we
# actually need to plumb track handoff and basic transport control.
# Anything else can fall back to `invoke(service, action_name, ...)`
# directly until a wrapper is needed.
# ---------------------------------------------------------------------


class PlaylistController:
    """OpenHome Playlist service. Controls the device's queue —
    insert tracks (with stream URL + DIDL-Lite metadata), play /
    pause / stop, skip, seek to a position in the current track,
    clear the queue.

    The Playlist service's `Id` semantics: each insert returns a
    NewId that uniquely identifies that queue entry. SeekId /
    SeekIndex / DeleteId all key off it. We expose `insert()`'s
    return value so callers can hold onto the Id for future calls.
    """

    def __init__(self, service: OpenHomeService) -> None:
        self._service = service

    @classmethod
    def from_device(
        cls, device: OpenHomeDevice
    ) -> Optional["PlaylistController"]:
        svc = device.get_service("Playlist")
        return cls(svc) if svc else None

    def insert(
        self,
        after_id: int,
        uri: str,
        metadata: str = "",
    ) -> int:
        """Insert a track after the queue entry with the given Id.

        `after_id=0` inserts at the head of the queue (the queue
        starts empty with no Ids; 0 is the conventional 'start')
        and is what slice 4 will use for the very-first track of a
        new session.

        `metadata` is DIDL-Lite XML. Slice 4 generates this from
        the Tidal track metadata (title, artist, album, duration,
        cover). The SOAP layer XML-escapes it before embedding —
        the device sees it as text content and parses it as an
        embedded DIDL-Lite document.

        Returns the NewId of the inserted track. Slice 4 stores it
        so subsequent SeekSecond calls can target the right entry.
        """
        result = invoke(
            self._service,
            "Insert",
            {
                "AfterId": str(after_id),
                "Uri": uri,
                "Metadata": metadata,
            },
        )
        try:
            return int(result.get("NewId", "0") or "0")
        except ValueError:
            return 0

    def delete_all(self) -> None:
        """Clear the queue. We call this before inserting on
        connect to put the device into a known empty state — some
        devices preserve the queue across controller switches and
        we don't want our Insert to land in slot 47 of someone
        else's leftover playlist."""
        invoke(self._service, "DeleteAll")

    def play(self) -> None:
        invoke(self._service, "Play")

    def pause(self) -> None:
        invoke(self._service, "Pause")

    def stop(self) -> None:
        invoke(self._service, "Stop")

    def next_track(self) -> None:
        """Skip forward. Named with a `_track` suffix because plain
        `next` would shadow Python's `next()` builtin and read
        ambiguously inside the audio engine."""
        invoke(self._service, "Next")

    def previous_track(self) -> None:
        invoke(self._service, "Previous")

    def seek_second(self, position_s: int) -> None:
        """Seek the currently-playing track to the given absolute
        second offset. OpenHome SeekSecond takes an integer; the
        decimal-seconds variant is SeekSecondAbsolute on some
        firmwares but not universally implemented."""
        invoke(
            self._service,
            "SeekSecond",
            {"Value": str(int(position_s))},
        )

    def seek_id(self, track_id: int) -> None:
        """Jump to a previously-inserted track by Id. Useful when
        the queue has multiple entries and we want to switch
        between them without rebuilding the queue."""
        invoke(self._service, "SeekId", {"Value": str(track_id)})


class VolumeController:
    """OpenHome Volume service. Controls device-side volume + mute.

    OpenHome volume is 0..VolumeMax (where VolumeMax is a per-
    device value, often 100 but sometimes 80 or 60 for protected
    speakers). This wrapper accepts a 0..100 percentage from the
    audio engine and translates to the device's range with a
    `volume_max()` lookup; if the lookup fails we assume 100.
    """

    def __init__(self, service: OpenHomeService) -> None:
        self._service = service
        self._cached_max: Optional[int] = None

    @classmethod
    def from_device(
        cls, device: OpenHomeDevice
    ) -> Optional["VolumeController"]:
        svc = device.get_service("Volume")
        return cls(svc) if svc else None

    def set_volume(self, level_percent: int) -> None:
        """Set the device volume from a 0-100 percentage. Internally
        scales to the device's actual VolumeMax range."""
        clamped = max(0, min(100, int(level_percent)))
        vmax = self._max_or_default()
        # Round-half-up to nearest device unit. Underflow (eg 1%
        # mapping to 0 on a VolumeMax-of-50 device) is OK — that's
        # the device's resolution limit, not our problem.
        scaled = round(clamped * vmax / 100.0)
        invoke(self._service, "SetVolume", {"Value": str(scaled)})

    def get_volume(self) -> int:
        """Read current device volume back as 0-100 percentage."""
        result = invoke(self._service, "Volume")
        try:
            raw = int(result.get("Value", "0") or "0")
        except ValueError:
            return 0
        vmax = self._max_or_default()
        if vmax <= 0:
            return 0
        return round(raw * 100.0 / vmax)

    def set_mute(self, muted: bool) -> None:
        invoke(
            self._service,
            "SetMute",
            {"Value": "true" if muted else "false"},
        )

    def get_mute(self) -> bool:
        result = invoke(self._service, "Mute")
        return (result.get("Value", "false") or "false").lower() == "true"

    def volume_max(self) -> int:
        """Read the device's VolumeMax. Cached after the first call
        because it doesn't change at runtime — some devices only
        expose it on a paid-firmware tier and would otherwise
        round-trip on every set_volume."""
        if self._cached_max is not None:
            return self._cached_max
        try:
            result = invoke(self._service, "VolumeMax")
            value = int(result.get("Value", "100") or "100")
            self._cached_max = value
            return value
        except (RuntimeError, ValueError):
            # Some firmwares don't expose VolumeMax (it isn't
            # required by the spec). Default to 100 — the most
            # common case — and stop trying.
            self._cached_max = 100
            return 100

    def _max_or_default(self) -> int:
        """Wrap volume_max() to never raise; degrades silently to
        100 if the device rejects the call. Inline use saves a
        try/except scattered through every set_volume / get_volume."""
        try:
            return self.volume_max()
        except Exception:
            return 100


class TimeController:
    """OpenHome Time service. Reports the currently-playing track's
    duration + position, plus a TrackCount that increments on every
    track change. Slice 5 subscribes to its events for live UI
    updates; this wrapper is the polling fallback."""

    def __init__(self, service: OpenHomeService) -> None:
        self._service = service

    @classmethod
    def from_device(
        cls, device: OpenHomeDevice
    ) -> Optional["TimeController"]:
        svc = device.get_service("Time")
        return cls(svc) if svc else None

    def time(self) -> dict[str, int]:
        """Returns {duration, seconds, track_count} as ints. The
        action returns four values (also TrackId) but we don't
        currently use TrackId from the polling path."""
        result = invoke(self._service, "Time")

        def _int(name: str, default: int = 0) -> int:
            try:
                return int(result.get(name, str(default)) or str(default))
            except ValueError:
                return default

        return {
            "duration": _int("Duration"),
            "seconds": _int("Seconds"),
            "track_count": _int("TrackCount"),
        }


class InfoController:
    """OpenHome Info service. Reports metadata about whatever the
    device is currently rendering — title, artist, codec, bit
    depth, sample rate, URI. Slice 4 reads it back after Insert+Play
    to confirm the device accepted our handoff."""

    def __init__(self, service: OpenHomeService) -> None:
        self._service = service

    @classmethod
    def from_device(
        cls, device: OpenHomeDevice
    ) -> Optional["InfoController"]:
        svc = device.get_service("Info")
        return cls(svc) if svc else None

    def track(self) -> dict[str, str]:
        """Returns {uri, metadata} of the current track. The
        Metadata field is the DIDL-Lite XML the device parsed out
        of our Insert call; useful for verifying round-trip."""
        result = invoke(self._service, "Track")
        return {
            "uri": result.get("Uri", ""),
            "metadata": result.get("Metadata", ""),
        }


# ---------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------


def _parse_service_element(
    svc_el: ET.Element,
    base_url: str,
) -> Optional[OpenHomeService]:
    """Translate one <service> element into an OpenHomeService.
    Returns None for entries that don't have the minimum required
    fields (serviceType + controlURL) so a malformed entry doesn't
    bring down the rest of the parse."""
    service_type = _text(_find(svc_el, "serviceType"))
    control_path = _text(_find(svc_el, "controlURL"))
    if not service_type or not control_path:
        return None
    short_name = _short_name_from_service_type(service_type)
    return OpenHomeService(
        service_type=service_type,
        service_id=_text(_find(svc_el, "serviceId")),
        short_name=short_name,
        control_url=urljoin(base_url, control_path),
        event_sub_url=urljoin(base_url, _text(_find(svc_el, "eventSubURL"))),
        scpd_url=urljoin(base_url, _text(_find(svc_el, "SCPDURL"))),
    )


def _short_name_from_service_type(service_type: str) -> str:
    """Extract the human-readable service name from a URN.

    `urn:av-openhome-org:service:Playlist:1` -> `Playlist`
    `urn:schemas-upnp-org:service:AVTransport:1` -> `AVTransport`
    `urn:linn-co-uk:service:Klimax:1` -> `Klimax`

    Falls back to the raw URN if the structure isn't what we expect,
    so a vendor-extension service-type doesn't blow the parse. The
    caller can still look it up by full URN if needed."""
    parts = service_type.split(":")
    # Standard pattern: ['urn', '<authority>', 'service', '<name>', '<version>']
    if len(parts) >= 4 and parts[-2] != "service":
        return parts[-2]
    if len(parts) >= 5 and parts[-3] == "service":
        return parts[-2]
    return service_type


def _find(parent: ET.Element, tag: str) -> Optional[ET.Element]:
    """Namespace-agnostic findall-first. UPnP descriptions ship with
    the `urn:schemas-upnp-org:device-1-0` (or service-1-0) namespace
    declared as default, which makes elementtree's tag matching
    require the full Clark notation `{ns}tag` everywhere. The {*}
    wildcard works on every Python 3.8+ etree and lets us write
    plain English tag names regardless of the namespace prefix."""
    return parent.find(f"{{*}}{tag}")


def _findall(parent: ET.Element, tag: str) -> list[ET.Element]:
    return list(parent.findall(f"{{*}}{tag}"))


def _text(el: Optional[ET.Element]) -> str:
    """Extract text from an Element, stripped, with None safely
    returning empty string. Used everywhere parsing pulls a text
    node so we don't have to scatter `if el is not None` checks."""
    if el is None or el.text is None:
        return ""
    return el.text.strip()
