"""Tests for the AVTransport + RenderingControl SOAP wrappers.

These pin the wire-level shape of the SOAP requests we send so a
refactor can't silently change InstanceID, Channel, Speed, or
argument names. Every consumer DLNA renderer cares about each of
those, and a mismatch turns into "device returns 402 Invalid Args"
which is the kind of failure that's hard to debug from the user
side. The transport layer (`requests.post`) is mocked; we capture
the body the wrapper would send and assert against its parsed
shape.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Optional

import pytest
import requests

from app.audio.avtransport import (
    AVTransportController,
    RenderingControlController,
    _find_service,
)
from app.audio.openhome import OpenHomeDevice, OpenHomeService


# ---------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------


def _service(
    *,
    service_type: str,
    short_name: Optional[str] = None,
) -> OpenHomeService:
    sn = short_name or service_type.split(":")[-2]
    return OpenHomeService(
        service_type=service_type,
        service_id=f"urn:upnp-org:serviceId:{sn}",
        short_name=sn,
        control_url=f"http://192.168.1.50:8080/{sn}/control",
        event_sub_url=f"http://192.168.1.50:8080/{sn}/event",
        scpd_url=f"http://192.168.1.50:8080/{sn}/scpd.xml",
        actions=(),
    )


def _device(*services: OpenHomeService) -> OpenHomeDevice:
    return OpenHomeDevice(
        udn="uuid:test-renderer",
        friendly_name="Test Renderer",
        manufacturer="Test Co",
        model_name="TR-1",
        model_number="1.0",
        services=services,
    )


# Empty SOAP response: what every transport-control action returns
# when it succeeds. We only need this to satisfy invoke()'s parser;
# the wrapper itself doesn't read the response of action verbs.
_EMPTY_RESPONSE_TEMPLATE = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
    '<s:Body>'
    '<u:{action}Response xmlns:u="{service_type}"/>'
    '</s:Body>'
    '</s:Envelope>'
)


class _Capture:
    """Context manager wrapping `monkeypatch`-style replacement of
    requests.post. Records each POST call in `.calls` so tests can
    assert on URL, body, and headers. Pattern mirrors
    `tests/test_openhome_soap.py::_mock_post` so behaviour stays
    consistent across the SOAP test suite.
    """

    def __init__(
        self,
        response_action: str,
        response_service: str,
        response_body: Optional[str] = None,
    ) -> None:
        self.body = response_body or _EMPTY_RESPONSE_TEMPLATE.format(
            action=response_action, service_type=response_service,
        )
        self.calls: list[dict] = []
        self._original_post = None

    def __enter__(self) -> "_Capture":
        class _MockResp:
            def __init__(self, body: str) -> None:
                self.text = body
                self.status_code = 200

        body = self.body
        calls = self.calls

        def _fake_post(url, data=None, headers=None, timeout=10.0):
            calls.append(
                {
                    "url": url,
                    "data": data,
                    "headers": headers or {},
                    "timeout": timeout,
                }
            )
            return _MockResp(body)

        self._original_post = requests.post
        requests.post = _fake_post  # type: ignore[assignment]
        return self

    def __exit__(self, *_exc) -> None:
        requests.post = self._original_post  # type: ignore[assignment]

    @property
    def call(self) -> dict:
        """Single-call shortcut. Every wrapper method here issues
        exactly one SOAP request, so this is the convenient way to
        get at the captured request."""
        assert len(self.calls) == 1, (
            f"expected exactly one POST, got {len(self.calls)}"
        )
        return self.calls[0]


def _capture(
    response_action: str,
    response_service: str,
    response_body: Optional[str] = None,
) -> _Capture:
    return _Capture(response_action, response_service, response_body)


def _parse_envelope(xml_text: str) -> dict[str, str]:
    """Return a flat dict of arg-name -> arg-value from a SOAP
    envelope body. Used to assert wrapper-built payloads have the
    expected fields without coupling the test to whitespace or
    namespace prefixes."""
    root = ET.fromstring(xml_text)
    out: dict[str, str] = {}
    # Walk to the action element (single child of Body).
    body = root.find("{*}Body")
    assert body is not None, "no Body element"
    action = list(body)[0]
    for child in action:
        # Strip namespace from the tag if any.
        tag = child.tag.split("}", 1)[-1]
        out[tag] = child.text or ""
    return out


# ---------------------------------------------------------------------
# _find_service
# ---------------------------------------------------------------------


class TestFindService:
    def test_matches_avtransport_v1(self):
        """The default version; what most renderers advertise."""
        dev = _device(_service(
            service_type="urn:schemas-upnp-org:service:AVTransport:1",
        ))
        assert AVTransportController.from_device(dev) is not None

    def test_matches_avtransport_v3(self):
        """Newer Sonos / WiiM firmwares may advertise :2 or :3.
        The action set we use is identical so we should still match."""
        dev = _device(_service(
            service_type="urn:schemas-upnp-org:service:AVTransport:3",
        ))
        assert AVTransportController.from_device(dev) is not None

    def test_returns_none_if_no_avtransport(self):
        """OpenHome-only devices (Linn, some Naim) don't expose
        AVTransport. The wrapper must report that cleanly so the
        DLNA manager can refuse to connect rather than build a
        controller that 404s on every action."""
        dev = _device(_service(
            service_type="urn:av-openhome-org:service:Playlist:1",
        ))
        assert AVTransportController.from_device(dev) is None

    def test_picks_first_match_when_multiple_versions_present(self):
        """If a device somehow advertises both AVTransport:1 and
        AVTransport:2 (rare but possible), we just take the first
        one; they are action-compatible."""
        dev = _device(
            _service(
                service_type="urn:schemas-upnp-org:service:AVTransport:1",
            ),
            _service(
                service_type="urn:schemas-upnp-org:service:AVTransport:2",
            ),
        )
        ctrl = AVTransportController.from_device(dev)
        assert ctrl is not None
        assert ctrl.service_type == \
            "urn:schemas-upnp-org:service:AVTransport:1"


# ---------------------------------------------------------------------
# AVTransportController action arguments
# ---------------------------------------------------------------------


class TestSetAVTransportURI:
    def test_sends_instance_id_uri_metadata(self):
        svc = _service(
            service_type="urn:schemas-upnp-org:service:AVTransport:1",
            short_name="AVTransport",
        )
        ctrl = AVTransportController(svc)
        with _capture(
            "SetAVTransportURI", svc.service_type,
        ) as mock_post:
            ctrl.set_av_transport_uri(
                "http://192.168.1.10:54321/dlna/stream",
                '<DIDL-Lite><item>x</item></DIDL-Lite>',
            )
        sent_body = mock_post.call["data"].decode("utf-8")
        args = _parse_envelope(sent_body)
        assert args["InstanceID"] == "0"
        assert args["CurrentURI"] == "http://192.168.1.10:54321/dlna/stream"
        # DIDL is XML-escaped on the wire; parsed text comes back
        # unescaped, which is what we want to compare against.
        assert "DIDL-Lite" in args["CurrentURIMetaData"]

    def test_soap_action_header(self):
        """The SOAPAction header is required by spec: devices MUST
        reject requests without it. Verify the wrapper passes it
        through `invoke()` correctly."""
        svc = _service(
            service_type="urn:schemas-upnp-org:service:AVTransport:1",
            short_name="AVTransport",
        )
        ctrl = AVTransportController(svc)
        with _capture(
            "SetAVTransportURI", svc.service_type,
        ) as mock_post:
            ctrl.set_av_transport_uri("http://x/", "<DIDL-Lite/>")
        headers = mock_post.call["headers"]
        assert headers["SOAPAction"] == \
            f'"{svc.service_type}#SetAVTransportURI"'


class TestPlayPauseStop:
    def test_play_with_default_speed(self):
        svc = _service(
            service_type="urn:schemas-upnp-org:service:AVTransport:1",
            short_name="AVTransport",
        )
        ctrl = AVTransportController(svc)
        with _capture("Play", svc.service_type) as mock_post:
            ctrl.play()
        args = _parse_envelope(mock_post.call["data"].decode())
        assert args["InstanceID"] == "0"
        assert args["Speed"] == "1"

    def test_pause_no_extra_args(self):
        svc = _service(
            service_type="urn:schemas-upnp-org:service:AVTransport:1",
            short_name="AVTransport",
        )
        ctrl = AVTransportController(svc)
        with _capture("Pause", svc.service_type) as mock_post:
            ctrl.pause()
        args = _parse_envelope(mock_post.call["data"].decode())
        assert args == {"InstanceID": "0"}

    def test_stop_no_extra_args(self):
        svc = _service(
            service_type="urn:schemas-upnp-org:service:AVTransport:1",
            short_name="AVTransport",
        )
        ctrl = AVTransportController(svc)
        with _capture("Stop", svc.service_type) as mock_post:
            ctrl.stop()
        args = _parse_envelope(mock_post.call["data"].decode())
        assert args == {"InstanceID": "0"}


class TestSeek:
    @pytest.mark.parametrize("position_s,expected", [
        (0, "00:00:00"),
        (45, "00:00:45"),
        (60, "00:01:00"),
        (3661, "01:01:01"),
        (3600 * 4 + 30, "04:00:30"),
    ])
    def test_formats_position_as_rel_time(self, position_s, expected):
        """REL_TIME unit requires HH:MM:SS. Devices that get a bare
        integer or a different unit (ABS_COUNT / TRACK_NR) reject
        with 710 Seek Mode Not Supported."""
        svc = _service(
            service_type="urn:schemas-upnp-org:service:AVTransport:1",
            short_name="AVTransport",
        )
        ctrl = AVTransportController(svc)
        with _capture("Seek", svc.service_type) as mock_post:
            ctrl.seek(position_s)
        args = _parse_envelope(mock_post.call["data"].decode())
        assert args["Unit"] == "REL_TIME"
        assert args["Target"] == expected

    def test_negative_position_clamped_to_zero(self):
        """Frontend can't send a negative seek but defense in depth:
        the wrapper must not produce '-01:00:00' which the device
        would reject."""
        svc = _service(
            service_type="urn:schemas-upnp-org:service:AVTransport:1",
            short_name="AVTransport",
        )
        ctrl = AVTransportController(svc)
        with _capture("Seek", svc.service_type) as mock_post:
            ctrl.seek(-10)
        args = _parse_envelope(mock_post.call["data"].decode())
        assert args["Target"] == "00:00:00"


# ---------------------------------------------------------------------
# RenderingControl
# ---------------------------------------------------------------------


class TestRenderingControl:
    def test_from_device_finds_v1(self):
        dev = _device(_service(
            service_type="urn:schemas-upnp-org:service:RenderingControl:1",
        ))
        assert RenderingControlController.from_device(dev) is not None

    def test_from_device_returns_none_when_absent(self):
        """Some renderers (especially headless TV streamers) only
        expose AVTransport, no RenderingControl. Manager treats
        volume/mute as no-ops in that case rather than failing."""
        dev = _device(_service(
            service_type="urn:schemas-upnp-org:service:AVTransport:1",
        ))
        assert RenderingControlController.from_device(dev) is None

    def test_set_volume_clamps_to_0_100(self):
        svc = _service(
            service_type="urn:schemas-upnp-org:service:RenderingControl:1",
            short_name="RenderingControl",
        )
        ctrl = RenderingControlController(svc)
        with _capture("SetVolume", svc.service_type) as mock_post:
            ctrl.set_volume(150)
        args = _parse_envelope(mock_post.call["data"].decode())
        assert args["DesiredVolume"] == "100"
        assert args["Channel"] == "Master"
        assert args["InstanceID"] == "0"

        with _capture("SetVolume", svc.service_type) as mock_post:
            ctrl.set_volume(-5)
        args = _parse_envelope(mock_post.call["data"].decode())
        assert args["DesiredVolume"] == "0"

    def test_set_mute_encodes_as_one_or_zero(self):
        """UPnP mute is the string '1' or '0', not 'true'/'false'.
        Some firmwares accept the bool form, but spec is the digit
        form and that's what we send."""
        svc = _service(
            service_type="urn:schemas-upnp-org:service:RenderingControl:1",
            short_name="RenderingControl",
        )
        ctrl = RenderingControlController(svc)
        with _capture("SetMute", svc.service_type) as mock_post:
            ctrl.set_mute(True)
        args = _parse_envelope(mock_post.call["data"].decode())
        assert args["DesiredMute"] == "1"

        with _capture("SetMute", svc.service_type) as mock_post:
            ctrl.set_mute(False)
        args = _parse_envelope(mock_post.call["data"].decode())
        assert args["DesiredMute"] == "0"

    def test_get_volume_parses_response(self):
        svc = _service(
            service_type="urn:schemas-upnp-org:service:RenderingControl:1",
            short_name="RenderingControl",
        )
        ctrl = RenderingControlController(svc)
        body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            '<s:Body>'
            f'<u:GetVolumeResponse xmlns:u="{svc.service_type}">'
            '<CurrentVolume>42</CurrentVolume>'
            '</u:GetVolumeResponse>'
            '</s:Body>'
            '</s:Envelope>'
        )
        with _capture("GetVolume", svc.service_type, response_body=body):
            assert ctrl.get_volume() == 42

    def test_get_volume_unparseable_returns_zero(self):
        """Some Cambridge / Yamaha firmwares return CurrentVolume
        as 'unknown' or empty when the device hasn't settled. Don't
        crash the caller. Return 0 and let the user see a slider
        at the bottom (visibly wrong) rather than a 500."""
        svc = _service(
            service_type="urn:schemas-upnp-org:service:RenderingControl:1",
            short_name="RenderingControl",
        )
        ctrl = RenderingControlController(svc)
        body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            '<s:Body>'
            f'<u:GetVolumeResponse xmlns:u="{svc.service_type}">'
            '<CurrentVolume>unknown</CurrentVolume>'
            '</u:GetVolumeResponse>'
            '</s:Body>'
            '</s:Envelope>'
        )
        with _capture("GetVolume", svc.service_type, response_body=body):
            assert ctrl.get_volume() == 0
