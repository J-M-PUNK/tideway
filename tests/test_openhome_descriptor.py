"""Unit tests for app/audio/openhome — descriptor + SCPD parsing.

The module's job is "XML in, typed Python objects out." Without
real Tidal Connect hardware, we can't exercise the HTTP fetch path
end-to-end, but the parsing layer is testable today against
synthetic fixtures based on the published OpenHome / UPnP spec.

The fixtures here are reduced versions of real device descriptors —
small enough to read at a glance but structurally faithful to what
a Bluesound Node, Linn Selekt, etc. actually serves. When real
hardware becomes available, these should match (modulo vendor
extensions) and integration testing fills in the rest.
"""
from __future__ import annotations

import pytest

from app.audio.openhome import (
    OpenHomeDevice,
    OpenHomeService,
    fetch_device,
    parse_device_description,
    parse_scpd,
)


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

# Trimmed root device descriptor matching the structure OpenHome /
# Linn-style devices serve. Real devices declare more services
# (Time, Info, Radio, Credentials, Volume, Receiver, Sender,
# Diagnostics, Configuration, ...); three is enough to verify the
# parser can find them all.
DEVICE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion>
    <major>1</major>
    <minor>0</minor>
  </specVersion>
  <device>
    <UDN>uuid:4c494e4e-1234-1234-1234-aabbccddeeff</UDN>
    <friendlyName>Living Room Streamer</friendlyName>
    <manufacturer>Bluesound</manufacturer>
    <modelName>Node</modelName>
    <modelNumber>N130</modelNumber>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <serviceList>
      <service>
        <serviceType>urn:av-openhome-org:service:Product:1</serviceType>
        <serviceId>urn:av-openhome-org:serviceId:Product</serviceId>
        <SCPDURL>/Product/scpd.xml</SCPDURL>
        <controlURL>/Product/control</controlURL>
        <eventSubURL>/Product/event</eventSubURL>
      </service>
      <service>
        <serviceType>urn:av-openhome-org:service:Playlist:1</serviceType>
        <serviceId>urn:av-openhome-org:serviceId:Playlist</serviceId>
        <SCPDURL>/Playlist/scpd.xml</SCPDURL>
        <controlURL>/Playlist/control</controlURL>
        <eventSubURL>/Playlist/event</eventSubURL>
      </service>
      <service>
        <serviceType>urn:av-openhome-org:service:Volume:1</serviceType>
        <serviceId>urn:av-openhome-org:serviceId:Volume</serviceId>
        <SCPDURL>/Volume/scpd.xml</SCPDURL>
        <controlURL>/Volume/control</controlURL>
        <eventSubURL>/Volume/event</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>
"""


# Trimmed Playlist SCPD. The real spec has more actions (Insert,
# DeleteId, Play, Pause, Next, SeekId, ...); three is enough to
# verify the parser handles the action / argument structure right.
PLAYLIST_SCPD = """<?xml version="1.0" encoding="UTF-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion>
    <major>1</major>
    <minor>0</minor>
  </specVersion>
  <actionList>
    <action>
      <name>Play</name>
      <argumentList></argumentList>
    </action>
    <action>
      <name>Insert</name>
      <argumentList>
        <argument>
          <name>AfterId</name>
          <direction>in</direction>
          <relatedStateVariable>IdArray</relatedStateVariable>
        </argument>
        <argument>
          <name>Uri</name>
          <direction>in</direction>
          <relatedStateVariable>Uri</relatedStateVariable>
        </argument>
        <argument>
          <name>Metadata</name>
          <direction>in</direction>
          <relatedStateVariable>Metadata</relatedStateVariable>
        </argument>
        <argument>
          <name>NewId</name>
          <direction>out</direction>
          <relatedStateVariable>Id</relatedStateVariable>
        </argument>
      </argumentList>
    </action>
    <action>
      <name>SeekId</name>
      <argumentList>
        <argument>
          <name>Value</name>
          <direction>in</direction>
          <relatedStateVariable>Id</relatedStateVariable>
        </argument>
      </argumentList>
    </action>
  </actionList>
</scpd>
"""


# ---------------------------------------------------------------------
# parse_device_description
# ---------------------------------------------------------------------


class TestParseDeviceDescription:
    def test_basic_device_fields(self):
        dev = parse_device_description(
            DEVICE_XML, "http://192.168.1.50:60006/desc.xml"
        )
        assert dev.friendly_name == "Living Room Streamer"
        assert dev.manufacturer == "Bluesound"
        assert dev.model_name == "Node"
        assert dev.model_number == "N130"
        assert dev.udn.startswith("uuid:")

    def test_services_parsed(self):
        dev = parse_device_description(
            DEVICE_XML, "http://192.168.1.50:60006/desc.xml"
        )
        names = [s.short_name for s in dev.services]
        assert names == ["Product", "Playlist", "Volume"]

    def test_service_urls_resolved_against_base(self):
        """controlURL etc. are paths relative to the base URL.
        After parsing they should be absolute so the SOAP client
        in slice 2 can use them directly."""
        dev = parse_device_description(
            DEVICE_XML, "http://192.168.1.50:60006/desc.xml"
        )
        playlist = dev.get_service("Playlist")
        assert playlist is not None
        assert playlist.control_url == (
            "http://192.168.1.50:60006/Playlist/control"
        )
        assert playlist.event_sub_url == (
            "http://192.168.1.50:60006/Playlist/event"
        )
        assert playlist.scpd_url == (
            "http://192.168.1.50:60006/Playlist/scpd.xml"
        )

    def test_get_service_case_insensitive(self):
        dev = parse_device_description(DEVICE_XML, "http://x/desc.xml")
        assert dev.get_service("playlist") is not None
        assert dev.get_service("PLAYLIST") is not None
        assert dev.get_service("Playlist") is not None

    def test_get_service_returns_none_for_missing(self):
        dev = parse_device_description(DEVICE_XML, "http://x/desc.xml")
        assert dev.get_service("Radio") is None
        assert dev.get_service("Credentials") is None

    def test_invalid_xml_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_device_description("<not-valid-xml", "http://x/")

    def test_no_device_element_raises(self):
        """Edge case: well-formed XML but missing the <device> tag.
        Better to fail loudly than to silently return a half-empty
        OpenHomeDevice."""
        empty = """<?xml version="1.0"?>
        <root xmlns="urn:schemas-upnp-org:device-1-0">
          <specVersion><major>1</major><minor>0</minor></specVersion>
        </root>"""
        with pytest.raises(ValueError):
            parse_device_description(empty, "http://x/")

    def test_handles_absolute_urls_in_service_block(self):
        """Some devices serve absolute URLs in their service blocks
        instead of relative paths. urljoin's behaviour: an absolute
        URL in the second arg overrides the base entirely. The
        parser shouldn't double-prefix them."""
        absolute_xml = DEVICE_XML.replace(
            "<controlURL>/Playlist/control</controlURL>",
            "<controlURL>http://other-host/Playlist/control</controlURL>",
        )
        dev = parse_device_description(absolute_xml, "http://192.168.1.50/d/")
        playlist = dev.get_service("Playlist")
        assert playlist is not None
        assert playlist.control_url == "http://other-host/Playlist/control"


# ---------------------------------------------------------------------
# parse_scpd
# ---------------------------------------------------------------------


class TestParseScpd:
    def test_action_count(self):
        actions = parse_scpd(PLAYLIST_SCPD)
        assert len(actions) == 3
        assert [a.name for a in actions] == ["Play", "Insert", "SeekId"]

    def test_action_with_no_arguments(self):
        actions = parse_scpd(PLAYLIST_SCPD)
        play = actions[0]
        assert play.name == "Play"
        assert play.arguments == ()
        assert play.in_arguments == ()
        assert play.out_arguments == ()

    def test_action_with_in_and_out_arguments(self):
        """Insert has three in-args (AfterId, Uri, Metadata) and
        one out-arg (NewId). The parser should split them on
        direction without losing order within each direction."""
        actions = parse_scpd(PLAYLIST_SCPD)
        insert = actions[1]
        assert insert.name == "Insert"
        in_names = [a.name for a in insert.in_arguments]
        assert in_names == ["AfterId", "Uri", "Metadata"]
        out_names = [a.name for a in insert.out_arguments]
        assert out_names == ["NewId"]

    def test_argument_state_variable_recorded(self):
        """relatedStateVariable is what slice 2's SOAP client uses
        to figure out argument types. The parser has to capture it
        even when it's just a name we don't otherwise use."""
        actions = parse_scpd(PLAYLIST_SCPD)
        insert = actions[1]
        afterid = insert.in_arguments[0]
        assert afterid.related_state_variable == "IdArray"
        uri = insert.in_arguments[1]
        assert uri.related_state_variable == "Uri"

    def test_empty_action_list(self):
        empty_scpd = """<?xml version="1.0"?>
        <scpd xmlns="urn:schemas-upnp-org:service-1-0">
          <actionList></actionList>
        </scpd>"""
        assert parse_scpd(empty_scpd) == ()

    def test_no_action_list_element_returns_empty(self):
        """A SCPD that's eventing-only (no actions, just state
        variables) should parse to an empty action list, not raise."""
        no_actions_scpd = """<?xml version="1.0"?>
        <scpd xmlns="urn:schemas-upnp-org:service-1-0">
          <serviceStateTable></serviceStateTable>
        </scpd>"""
        assert parse_scpd(no_actions_scpd) == ()

    def test_invalid_scpd_xml_raises(self):
        with pytest.raises(ValueError):
            parse_scpd("not xml at all")


# ---------------------------------------------------------------------
# fetch_device — HTTP-mocked
# ---------------------------------------------------------------------


class TestFetchDevice:
    def test_happy_path(self, monkeypatch):
        """Verify fetch_device wires the description and the per-
        service SCPD calls together. We mock requests.get so this
        runs without any network."""
        responses = {
            "http://192.168.1.50:60006/desc.xml": DEVICE_XML,
            "http://192.168.1.50:60006/Playlist/scpd.xml": PLAYLIST_SCPD,
            "http://192.168.1.50:60006/Product/scpd.xml": (
                """<?xml version='1.0'?>
                <scpd xmlns='urn:schemas-upnp-org:service-1-0'>
                  <actionList></actionList>
                </scpd>"""
            ),
            "http://192.168.1.50:60006/Volume/scpd.xml": (
                """<?xml version='1.0'?>
                <scpd xmlns='urn:schemas-upnp-org:service-1-0'>
                  <actionList></actionList>
                </scpd>"""
            ),
        }

        class _MockResp:
            def __init__(self, body: str):
                self.text = body
                self.status_code = 200

            def raise_for_status(self):
                pass

        def _fake_get(url, timeout=10.0):
            if url not in responses:
                raise RuntimeError(f"unexpected url {url}")
            return _MockResp(responses[url])

        import requests
        monkeypatch.setattr(requests, "get", _fake_get)

        dev = fetch_device("http://192.168.1.50:60006/desc.xml")
        assert dev.friendly_name == "Living Room Streamer"
        playlist = dev.get_service("Playlist")
        assert playlist is not None
        assert len(playlist.actions) == 3
        assert playlist.actions[1].name == "Insert"

    def test_scpd_failure_yields_empty_actions_not_crash(self, monkeypatch):
        """If a single SCPD URL 404s or hangs, the device should
        still come back with the OTHER services intact and the bad
        one with an empty action list. Empirical: vendor SDKs
        sometimes gate certain services behind auth and respond
        500 to anonymous SCPD fetches."""

        class _MockResp:
            def __init__(self, body: str):
                self.text = body
                self.status_code = 200

            def raise_for_status(self):
                pass

        def _fake_get(url, timeout=10.0):
            if url.endswith("desc.xml"):
                return _MockResp(DEVICE_XML)
            if "Playlist" in url:
                return _MockResp(PLAYLIST_SCPD)
            # Volume + Product fail.
            raise RuntimeError("simulated SCPD fetch failure")

        import requests
        monkeypatch.setattr(requests, "get", _fake_get)

        dev = fetch_device("http://192.168.1.50:60006/desc.xml")
        playlist = dev.get_service("Playlist")
        volume = dev.get_service("Volume")
        assert playlist is not None
        assert len(playlist.actions) == 3
        assert volume is not None
        assert volume.actions == ()  # empty, didn't crash the device parse

    def test_root_description_fetch_failure_propagates(self, monkeypatch):
        """If the root descriptor fetch fails, the device cannot be
        constructed at all. Surface it as RuntimeError so callers
        can catch one specific class instead of every requests-
        specific exception."""

        def _fake_get(url, timeout=10.0):
            raise RuntimeError("network down")

        import requests
        monkeypatch.setattr(requests, "get", _fake_get)
        with pytest.raises(RuntimeError) as exc:
            fetch_device("http://x/desc.xml")
        assert "failed to fetch device description" in str(exc.value)

    def test_fetch_scpds_false_skips_inner_calls(self, monkeypatch):
        """The picker UI may want device metadata without paying
        the per-service round trip. fetch_scpds=False should mean
        exactly one HTTP call: the root description."""
        calls: list[str] = []

        class _MockResp:
            text = DEVICE_XML

            def raise_for_status(self):
                pass

        def _fake_get(url, timeout=10.0):
            calls.append(url)
            return _MockResp()

        import requests
        monkeypatch.setattr(requests, "get", _fake_get)

        dev = fetch_device(
            "http://x/desc.xml", fetch_scpds=False
        )
        assert len(calls) == 1
        assert dev.friendly_name == "Living Room Streamer"
        for svc in dev.services:
            assert svc.actions == ()
