"""OpenHome service descriptor parser.

OpenHome (linn-co-uk / av-openhome-org) is the UPnP-derived control
protocol that Tidal Connect targets implement. Every Bluesound,
Linn, NAD, Cambridge, etc. unit on a LAN exposes:

  - A root device description XML at the LOCATION URL we got from
    SSDP. Lists the device's friendly name, manufacturer, model, and
    every service it implements.
  - For each service, a separate SCPD (Service Control Protocol
    Description) XML that lists the SOAP-callable actions, their
    arguments, and the related state variables.

This module turns those XMLs into typed Python objects the rest of
Tideway can issue control calls against. Pure parsing + HTTP fetch;
no SOAP, no eventing, no Tidal-specific logic. Slice 2 builds the
SOAP client on top.

Why separate from `tidal_connect.py`: OpenHome is a generic protocol
useful for any UPnP-AV controller we might build (we already have
`upnp.py` for plain MediaRenderer; OpenHome is a more capable layer
above that). Keeping the parser independent lets it be reused if
Tidal Connect turns out to need adjustments and we want a fallback
for plain OpenHome control.
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
