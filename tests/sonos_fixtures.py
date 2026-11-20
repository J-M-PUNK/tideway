"""Sonos-shaped UPnP fixtures shared by the descriptor, discovery and
session tests.

The layout follows what a Sonos player serves at
`http://<ip>:1400/xml/device_description.xml`: a `ZonePlayer` root
whose own services are Sonos housekeeping (AlarmClock,
ZoneGroupTopology, ...), with the standard AV services on embedded
devices. AVTransport and RenderingControl live on the embedded
`MediaRenderer`, not on the root. That nesting is what the DLNA path
has to cope with; the housekeeping services are trimmed to a
representative few.
"""
from __future__ import annotations

SONOS_HOST = "192.168.1.60:1400"
SONOS_LOCATION = f"http://{SONOS_HOST}/xml/device_description.xml"
SONOS_UDN = "uuid:RINCON_000E58AABBCC01400"


def _svc(service_type: str, service_id: str, path: str) -> str:
    return f"""
        <service>
          <serviceType>urn:schemas-upnp-org:service:{service_type}</serviceType>
          <serviceId>urn:upnp-org:serviceId:{service_id}</serviceId>
          <controlURL>{path}/Control</controlURL>
          <eventSubURL>{path}/Event</eventSubURL>
          <SCPDURL>/xml/{service_id}1.xml</SCPDURL>
        </service>"""


def sonos_device_xml(
    udn: str = SONOS_UDN, room: str = "Living Room", ip: str = "192.168.1.60",
) -> str:
    """Description a Sonos player at `ip` serves, for room `room`."""
    return f"""<?xml version="1.0" encoding="utf-8" ?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:ZonePlayer:1</deviceType>
    <friendlyName>{ip} - Sonos One - RINCON_000E58AABBCC01400</friendlyName>
    <manufacturer>Sonos, Inc.</manufacturer>
    <modelNumber>S18</modelNumber>
    <modelName>Sonos One</modelName>
    <roomName>{room}</roomName>
    <displayName>One</displayName>
    <UDN>{udn}</UDN>
    <serviceList>
      {_svc("AlarmClock:1", "AlarmClock", "/AlarmClock")}
      {_svc("DeviceProperties:1", "DeviceProperties", "/DeviceProperties")}
      {_svc("ZoneGroupTopology:1", "ZoneGroupTopology", "/ZoneGroupTopology")}
    </serviceList>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
        <friendlyName>{ip} - Sonos One Media Server</friendlyName>
        <manufacturer>Sonos, Inc.</manufacturer>
        <modelName>Sonos One</modelName>
        <UDN>{udn}_MS</UDN>
        <serviceList>
          {_svc("ContentDirectory:1", "ContentDirectory", "/MediaServer/ContentDirectory")}
          {_svc("ConnectionManager:1", "ConnectionManager", "/MediaServer/ConnectionManager")}
        </serviceList>
      </device>
      <device>
        <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
        <friendlyName>{room} - Sonos One Media Renderer</friendlyName>
        <manufacturer>Sonos, Inc.</manufacturer>
        <modelName>Sonos One</modelName>
        <UDN>{udn}_MR</UDN>
        <serviceList>
          {_svc("RenderingControl:1", "RenderingControl", "/MediaRenderer/RenderingControl")}
          {_svc("ConnectionManager:1", "ConnectionManager", "/MediaRenderer/ConnectionManager")}
          {_svc("AVTransport:1", "AVTransport", "/MediaRenderer/AVTransport")}
        </serviceList>
      </device>
    </deviceList>
  </device>
</root>
"""


SONOS_DEVICE_XML = sonos_device_xml()


def sonos_location(ip: str) -> str:
    return f"http://{ip}:1400/xml/device_description.xml"


def zone_group_state(groups) -> str:
    """ZoneGroupState document in the current-firmware wrapping.
    `groups` is a list of (coordinator_uuid, [(uuid, ip, zone_name,
    invisible), ...])."""
    out = ["<ZoneGroupState><ZoneGroups>"]
    for coordinator, members in groups:
        out.append(f'<ZoneGroup Coordinator="{coordinator}" ID="{coordinator}:1">')
        for uuid, ip, zone, invisible in members:
            inv = ' Invisible="1"' if invisible else ""
            out.append(
                f'<ZoneGroupMember UUID="{uuid}" '
                f'Location="{sonos_location(ip)}" ZoneName="{zone}"{inv}/>'
            )
        out.append("</ZoneGroup>")
    out.append("</ZoneGroups></ZoneGroupState>")
    return "".join(out)


def soap_response(action: str, out_args: dict[str, str]) -> str:
    """A UPnP SOAP response body carrying `out_args` (escaped)."""
    from xml.sax.saxutils import escape

    args = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in out_args.items())
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<s:Body><u:{action}Response xmlns:u="x">{args}'
        f"</u:{action}Response></s:Body></s:Envelope>"
    )
