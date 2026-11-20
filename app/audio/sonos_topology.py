"""Sonos zone-group topology for the DLNA output path.

Every Sonos player answers SSDP and carries its own AVTransport, but
only a group's coordinator accepts transport commands: a member of a
group answers SetAVTransportURI with a fault, and the hidden halves of
a stereo pair or home-theatre set (`Invisible="1"`) aren't rooms a user
would pick at all. The Sonos app and Spotify list one entry per group,
addressed to its coordinator. This module reads the topology that lets
the DLNA picker do the same.

The topology comes from `ZoneGroupTopology.GetZoneGroupState` on any
player in the household; every player returns the whole household's
groups. The out-arg is an XML document as a string. Current firmware
wraps it as `<ZoneGroupState><ZoneGroups>...`, older firmware returns
`<ZoneGroups>` as the root; both are handled by walking for
`ZoneGroup` elements.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

from app.audio.openhome import OpenHomeDevice, invoke

_ZGT_PREFIX = "urn:schemas-upnp-org:service:ZoneGroupTopology:"


@dataclass(frozen=True)
class ZoneMember:
    uuid: str  # "RINCON_..." (the UDN without its "uuid:" prefix)
    location: str  # the member's description URL
    zone_name: str
    invisible: bool


@dataclass(frozen=True)
class ZoneGroup:
    coordinator_uuid: str
    members: tuple[ZoneMember, ...]

    @property
    def coordinator(self) -> Optional[ZoneMember]:
        return next(
            (m for m in self.members if m.uuid == self.coordinator_uuid),
            None,
        )

    def display_name(self) -> str:
        """Room names of the visible members, coordinator first, the way
        the Sonos app labels a group ("Living Room + Kitchen"). A stereo
        pair is two members with the same room name, so names are
        de-duplicated in order."""
        ordered = sorted(
            (m for m in self.members if not m.invisible),
            key=lambda m: m.uuid != self.coordinator_uuid,
        )
        names: list[str] = []
        for m in ordered:
            if m.zone_name and m.zone_name not in names:
                names.append(m.zone_name)
        return " + ".join(names)


def udn_to_uuid(udn: str) -> str:
    """`uuid:RINCON_x` -> `RINCON_x`, the form topology uses."""
    return udn[5:] if udn.startswith("uuid:") else udn


def parse_zone_group_state(xml: str) -> tuple[ZoneGroup, ...]:
    """Parse the ZoneGroupState document. Raises ValueError on XML that
    doesn't parse; returns () when it holds no groups."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"ZoneGroupState XML invalid: {exc}") from exc
    groups: list[ZoneGroup] = []
    for group_el in root.iter("ZoneGroup"):
        members = tuple(
            ZoneMember(
                uuid=m.get("UUID", ""),
                location=m.get("Location", ""),
                zone_name=m.get("ZoneName", ""),
                invisible=m.get("Invisible") == "1",
            )
            # Direct children only: home-theatre satellites are nested
            # <Satellite> elements, which iter() would not match anyway,
            # but a member is never nested inside another member.
            for m in group_el.findall("ZoneGroupMember")
        )
        groups.append(ZoneGroup(
            coordinator_uuid=group_el.get("Coordinator", ""),
            members=members,
        ))
    return tuple(groups)


def fetch_zone_groups(
    tree: OpenHomeDevice, timeout: float,
) -> Optional[tuple[ZoneGroup, ...]]:
    """Ask the player described by `tree` for the household topology.
    None when it doesn't expose ZoneGroupTopology (not a Sonos).
    Transport errors and SOAP faults propagate to the caller, which
    decides how to degrade."""
    holder = tree.find_device_with_service(_ZGT_PREFIX)
    if holder is None:
        return None
    service = next(
        s for s in holder.services if s.service_type.startswith(_ZGT_PREFIX)
    )
    out = invoke(service, "GetZoneGroupState", {}, timeout=timeout)
    return parse_zone_group_state(out.get("ZoneGroupState", ""))


def group_of(
    groups: tuple[ZoneGroup, ...], uuid: str,
) -> Optional[ZoneGroup]:
    """The group `uuid` belongs to, or None."""
    for g in groups:
        if any(m.uuid == uuid for m in g.members):
            return g
    return None
