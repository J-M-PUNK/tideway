"""Tests for the OpenHome SOAP action client (slice 2).

`invoke()` is the workhorse: it builds a SOAP envelope, POSTs it to
the device's controlURL, and parses the response (or fault) back
into a Python dict. These tests cover the encode + decode halves
against synthetic device responses that mirror what real OpenHome
devices return per the UPnP spec. The HTTP layer is mocked so the
suite runs without any network.

When real hardware shows up, the same fixtures should hold (modulo
vendor-specific namespace prefixes which the parser already handles
via wildcard tag matching). Failures will pinpoint exactly which
shape diverges.
"""
from __future__ import annotations

import pytest

from app.audio.openhome import (
    OpenHomeAction,
    OpenHomeArgument,
    OpenHomeSOAPError,
    OpenHomeService,
    _build_soap_envelope,
    _parse_soap_fault,
    _parse_soap_response,
    _xml_escape,
    invoke,
)


# ---------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------


def _service(
    *,
    service_type: str = "urn:av-openhome-org:service:Playlist:1",
    short_name: str = "Playlist",
    control_url: str = "http://192.168.1.50:60006/Playlist/control",
    actions: tuple = (),
) -> OpenHomeService:
    return OpenHomeService(
        service_type=service_type,
        service_id=f"urn:av-openhome-org:serviceId:{short_name}",
        short_name=short_name,
        control_url=control_url,
        event_sub_url=control_url.replace("control", "event"),
        scpd_url=control_url.replace("control", "scpd.xml"),
        actions=actions,
    )


# Sample successful SOAP response for `Playlist.Insert` — three
# in-args, one out-arg (NewId). Mirrors what a real OpenHome device
# returns. Parsing must extract `NewId` regardless of namespace
# prefix (some devices use s:, some use SOAP-ENV:).
INSERT_RESPONSE = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:InsertResponse xmlns:u="urn:av-openhome-org:service:Playlist:1">
      <NewId>42</NewId>
    </u:InsertResponse>
  </s:Body>
</s:Envelope>
"""


# Sample successful response for an action with NO out-args
# (Play, Pause, Stop). The Body contains the *Response wrapper but
# nothing inside — invoke() should return an empty dict, not raise.
EMPTY_RESPONSE = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body>
    <u:PlayResponse xmlns:u="urn:av-openhome-org:service:Playlist:1"/>
  </s:Body>
</s:Envelope>
"""


# Sample UPnP fault response. errorCode 402 = Invalid Args.
FAULT_RESPONSE_402 = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body>
    <s:Fault>
      <faultcode>s:Client</faultcode>
      <faultstring>UPnPError</faultstring>
      <detail>
        <UPnPError xmlns="urn:schemas-upnp-org:control-1-0">
          <errorCode>402</errorCode>
          <errorDescription>Invalid Args</errorDescription>
        </UPnPError>
      </detail>
    </s:Fault>
  </s:Body>
</s:Envelope>
"""


# Some firmwares return the UPnPError with a missing description.
# The parser should still surface a useful message, not crash.
FAULT_RESPONSE_NO_DESC = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body>
    <s:Fault>
      <faultcode>s:Client</faultcode>
      <faultstring>UPnPError</faultstring>
      <detail>
        <UPnPError xmlns="urn:schemas-upnp-org:control-1-0">
          <errorCode>501</errorCode>
        </UPnPError>
      </detail>
    </s:Fault>
  </s:Body>
</s:Envelope>
"""


# ---------------------------------------------------------------------
# _xml_escape
# ---------------------------------------------------------------------


class TestXmlEscape:
    def test_ampersand(self):
        assert _xml_escape("a & b") == "a &amp; b"

    def test_lt_gt(self):
        assert _xml_escape("<x>") == "&lt;x&gt;"

    def test_quotes(self):
        assert _xml_escape('"hello"') == "&quot;hello&quot;"

    def test_apostrophe(self):
        assert _xml_escape("don't") == "don&apos;t"

    def test_combined(self):
        """The DIDL-Lite XML we'll embed as a Metadata argument in
        slice 4 contains <, >, &, and quotes. Verify the escaper
        turns it into safe text-content."""
        raw = '<item id="1">&amp;</item>'
        escaped = _xml_escape(raw)
        # Re-escapes the already-escaped &amp; — that's correct.
        # The original input had a literal `&amp;` which is a 5-char
        # text sequence that we need to escape further so the
        # device sees `&amp;` as text.
        assert "&amp;amp;" in escaped
        assert "&lt;" in escaped
        assert "&gt;" in escaped
        assert "&quot;" in escaped


# ---------------------------------------------------------------------
# _build_soap_envelope
# ---------------------------------------------------------------------


class TestBuildSoapEnvelope:
    def test_basic_structure(self):
        env = _build_soap_envelope(
            "urn:av-openhome-org:service:Playlist:1", "Play", {}
        )
        assert env.startswith('<?xml version="1.0"')
        assert "<s:Envelope" in env
        assert "<s:Body>" in env
        assert (
            '<u:Play xmlns:u="urn:av-openhome-org:service:Playlist:1">'
            in env
        )
        assert "</u:Play>" in env
        assert "</s:Body>" in env
        assert "</s:Envelope>" in env

    def test_args_serialized_as_child_elements(self):
        env = _build_soap_envelope(
            "urn:av-openhome-org:service:Playlist:1",
            "Insert",
            {"AfterId": "0", "Uri": "http://example/track.flac"},
        )
        assert "<AfterId>0</AfterId>" in env
        assert "<Uri>http://example/track.flac</Uri>" in env

    def test_arg_values_are_xml_escaped(self):
        """The Metadata argument used in slice 4 contains an XML
        document (DIDL-Lite). It MUST be escaped before going into
        the envelope or the device will see two nested XML trees
        and reject the call."""
        env = _build_soap_envelope(
            "urn:av-openhome-org:service:Playlist:1",
            "Insert",
            {"Metadata": '<item><title>Hello & "World"</title></item>'},
        )
        # Should NOT contain a literal nested <item> tag.
        assert "<Metadata><item>" not in env
        # SHOULD contain the escaped form.
        assert "&lt;item&gt;" in env
        assert "&amp;" in env
        assert "&quot;World&quot;" in env

    def test_action_namespace_matches_service_type(self):
        """A Linn-namespace service type should produce an envelope
        whose action element references the same namespace, even
        though it's not the standard openhome.org one. The SOAP
        spec requires the namespace match for the device to route
        the call."""
        env = _build_soap_envelope(
            "urn:linn-co-uk:service:Diagnostics:1", "Echo", {}
        )
        assert 'xmlns:u="urn:linn-co-uk:service:Diagnostics:1"' in env


# ---------------------------------------------------------------------
# _parse_soap_response
# ---------------------------------------------------------------------


class TestParseSoapResponse:
    def test_extracts_out_args(self):
        result = _parse_soap_response(INSERT_RESPONSE, "Insert")
        assert result == {"NewId": "42"}

    def test_empty_response_for_no_out_arg_action(self):
        result = _parse_soap_response(EMPTY_RESPONSE, "Play")
        assert result == {}

    def test_empty_body_returns_empty_dict(self):
        """A device that responds with HTTP 200 + empty body to a
        no-out-arg action shouldn't raise. Some firmwares do this
        despite the spec saying they should send the *Response
        wrapper."""
        assert _parse_soap_response("", "Play") == {}

    def test_malformed_xml_raises_runtime_error(self):
        """A device returning unparseable XML is broken, not just
        rejecting the action. Surface as RuntimeError so the caller
        can distinguish device-side error (UPnPError) from
        protocol-level malformation."""
        with pytest.raises(RuntimeError) as exc:
            _parse_soap_response("<not-soap>", "Insert")
        assert "not parseable" in str(exc.value).lower()

    def test_response_without_action_wrapper(self):
        """Some firmwares skip the *Response wrapper element. The
        parser walks down to the first child of <Body> in that
        case rather than failing."""
        unwrapped = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body>
    <SomeOtherWrapper>
      <NewId>99</NewId>
    </SomeOtherWrapper>
  </s:Body>
</s:Envelope>
"""
        result = _parse_soap_response(unwrapped, "Insert")
        assert result == {"NewId": "99"}


# ---------------------------------------------------------------------
# _parse_soap_fault
# ---------------------------------------------------------------------


class TestParseSoapFault:
    def test_returns_none_for_success_response(self):
        assert _parse_soap_fault(INSERT_RESPONSE) is None
        assert _parse_soap_fault(EMPTY_RESPONSE) is None

    def test_extracts_code_and_description(self):
        result = _parse_soap_fault(FAULT_RESPONSE_402)
        assert result == (402, "Invalid Args")

    def test_handles_missing_description(self):
        result = _parse_soap_fault(FAULT_RESPONSE_NO_DESC)
        assert result is not None
        code, desc = result
        assert code == 501
        assert desc == "(no description)"

    def test_returns_none_for_malformed_xml(self):
        """Malformed XML is a transport problem, not a fault. The
        caller's status-code check will then surface RuntimeError."""
        assert _parse_soap_fault("not xml") is None

    def test_returns_none_for_empty_body(self):
        assert _parse_soap_fault("") is None


# ---------------------------------------------------------------------
# invoke — end-to-end with mocked HTTP
# ---------------------------------------------------------------------


def _mock_post(monkeypatch, *, body: str, status_code: int = 200):
    """Helper: replace requests.post with a stub that returns the
    given body + status. Records calls so tests can assert on the
    SOAPAction header etc."""
    calls: list[dict] = []

    class _MockResp:
        def __init__(self, body: str, status_code: int):
            self.text = body
            self.status_code = status_code

    def _fake_post(url, data=None, headers=None, timeout=10.0):
        calls.append(
            {
                "url": url,
                "data": data,
                "headers": headers or {},
                "timeout": timeout,
            }
        )
        return _MockResp(body, status_code)

    import requests
    monkeypatch.setattr(requests, "post", _fake_post)
    return calls


class TestInvoke:
    def test_successful_call_returns_out_args(self, monkeypatch):
        calls = _mock_post(monkeypatch, body=INSERT_RESPONSE)
        svc = _service()
        result = invoke(
            svc,
            "Insert",
            {"AfterId": "0", "Uri": "http://x/track", "Metadata": "<x/>"},
        )
        assert result == {"NewId": "42"}
        # Verify the SOAPAction header is well-formed.
        soap_action = calls[0]["headers"].get("SOAPAction", "")
        assert soap_action == (
            '"urn:av-openhome-org:service:Playlist:1#Insert"'
        )

    def test_fault_response_raises_openhome_soap_error(self, monkeypatch):
        _mock_post(monkeypatch, body=FAULT_RESPONSE_402, status_code=500)
        svc = _service()
        with pytest.raises(OpenHomeSOAPError) as exc:
            invoke(svc, "Insert", {"AfterId": "abc"})  # bad arg type
        assert exc.value.code == 402
        assert exc.value.description == "Invalid Args"
        assert exc.value.action == "Insert"
        assert "Playlist:1" in exc.value.service_type

    def test_fault_response_with_200_status_still_raises(self, monkeypatch):
        """A few OpenHome firmwares answer with HTTP 200 even on
        UPnP faults. The body shape is what matters; status code
        is advisory."""
        _mock_post(monkeypatch, body=FAULT_RESPONSE_402, status_code=200)
        svc = _service()
        with pytest.raises(OpenHomeSOAPError):
            invoke(svc, "Insert", {})

    def test_no_out_args_returns_empty_dict(self, monkeypatch):
        _mock_post(monkeypatch, body=EMPTY_RESPONSE)
        svc = _service()
        assert invoke(svc, "Play") == {}

    def test_transport_failure_raises_runtime_error(self, monkeypatch):
        def _raise(*_a, **_k):
            raise OSError("connection refused")

        import requests
        monkeypatch.setattr(requests, "post", _raise)
        svc = _service()
        with pytest.raises(RuntimeError) as exc:
            invoke(svc, "Play")
        assert "transport" in str(exc.value).lower()

    def test_envelope_posted_to_service_control_url(self, monkeypatch):
        calls = _mock_post(monkeypatch, body=EMPTY_RESPONSE)
        svc = _service(control_url="http://10.0.0.5:55178/Playlist/control")
        invoke(svc, "Play")
        assert calls[0]["url"] == "http://10.0.0.5:55178/Playlist/control"

    def test_envelope_body_contains_action(self, monkeypatch):
        calls = _mock_post(monkeypatch, body=EMPTY_RESPONSE)
        svc = _service()
        invoke(svc, "Play")
        body = calls[0]["data"].decode("utf-8")
        assert "<u:Play" in body
        assert "Playlist:1" in body

    def test_args_serialized_into_envelope(self, monkeypatch):
        calls = _mock_post(monkeypatch, body=INSERT_RESPONSE)
        svc = _service()
        invoke(
            svc,
            "Insert",
            {"AfterId": "0", "Uri": "http://x/y.flac"},
        )
        body = calls[0]["data"].decode("utf-8")
        assert "<AfterId>0</AfterId>" in body
        assert "<Uri>http://x/y.flac</Uri>" in body

    def test_http_500_without_fault_body_raises_runtime_error(
        self, monkeypatch
    ):
        """5xx with a non-fault body is a server-side malfunction,
        not a UPnP-spec error. Distinguish from OpenHomeSOAPError so
        callers can decide whether to retry."""
        _mock_post(
            monkeypatch,
            body="<html>Internal Server Error</html>",
            status_code=500,
        )
        svc = _service()
        with pytest.raises(RuntimeError) as exc:
            invoke(svc, "Play")
        assert "500" in str(exc.value)


# ---------------------------------------------------------------------
# OpenHomeSOAPError
# ---------------------------------------------------------------------


class TestOpenHomeSOAPError:
    def test_message_includes_code_and_description(self):
        err = OpenHomeSOAPError(
            402,
            "Invalid Args",
            action="Insert",
            service_type="urn:av-openhome-org:service:Playlist:1",
        )
        msg = str(err)
        assert "402" in msg
        assert "Invalid Args" in msg
        assert "Insert" in msg
        assert "Playlist" in msg

    def test_attributes_accessible(self):
        err = OpenHomeSOAPError(501, "Action Failed")
        assert err.code == 501
        assert err.description == "Action Failed"
        assert err.action == ""
        assert err.service_type == ""

    def test_is_runtime_error_subclass(self):
        """Callers that want to catch any control-plane error can
        catch RuntimeError and pick up both transport failures and
        UPnP faults. Specific catch on OpenHomeSOAPError is for
        callers that want to react to spec error codes."""
        err = OpenHomeSOAPError(402, "Invalid Args")
        assert isinstance(err, RuntimeError)
