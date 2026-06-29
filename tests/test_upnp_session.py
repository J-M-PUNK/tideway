"""Tests for the UPnP/DLNA renderer manager: connect handshake,
disconnect teardown, and the push_pcm audio tap.

These pin behaviour at the UpnpManager level. The SOAP wire shape
is covered separately in test_avtransport_controllers.py; here we
assume those wrappers work and verify the manager calls them in
the right order, builds + tears down the streaming pipeline
correctly, and behaves as expected when fed PCM at varying rates
+ dtypes.

Network is mocked out at two layers:
  - `fetch_device` is patched to return a synthetic OpenHomeDevice
    so connect() doesn't have to go to a real LAN host.
  - The AVTransportController.from_device path is real because
    its argument-list checks help catch regressions; the actual
    SOAP transport is patched at requests.post.
"""
from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock

import numpy as np
import pytest
import requests

from app.audio.openhome import OpenHomeDevice, OpenHomeService
from app.audio.upnp import (
    UpnpDevice,
    UpnpManager,
    _SessionState,
    _filter_dlna_renderer,
)


# ---------------------------------------------------------------------
# Fixtures: synthetic devices + SOAP wire mock
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


def _device_record() -> UpnpDevice:
    """The discovery-cache shape: what list_devices returns."""
    return UpnpDevice(
        id="uuid:test-renderer",
        name="Test WiiM",
        manufacturer="Linkplay",
        model="WiiM Pro",
        location="http://192.168.1.50:8080/desc.xml",
        service_types=(
            "urn:schemas-upnp-org:service:AVTransport:1",
            "urn:schemas-upnp-org:service:RenderingControl:1",
        ),
        has_avtransport=True,
    )


def _openhome_device(
    *,
    with_rendering_control: bool = True,
) -> OpenHomeDevice:
    """The full descriptor shape: what fetch_device returns."""
    services = [
        _service(
            service_type="urn:schemas-upnp-org:service:AVTransport:1",
            short_name="AVTransport",
        ),
    ]
    if with_rendering_control:
        services.append(_service(
            service_type="urn:schemas-upnp-org:service:RenderingControl:1",
            short_name="RenderingControl",
        ))
    return OpenHomeDevice(
        udn="uuid:test-renderer",
        friendly_name="Test WiiM",
        manufacturer="Linkplay",
        model_name="WiiM Pro",
        model_number="2.0",
        services=tuple(services),
    )


@pytest.fixture
def mock_soap(monkeypatch):
    """Replace requests.post with a stub that returns an empty SOAP
    response. Records calls so tests can assert which actions fired
    in what order.

    Only POSTs to this file's synthetic device control host
    (`_service` builds every control URL on 192.168.1.50:8080) are
    recorded. `requests.post` is process-global, so a background
    poller leaked by another test (tidal_connect polls Time +
    Volume on its own device) would otherwise land in this capture
    and corrupt the asserted SOAP sequence. Scoping the recorder to
    the device under test keeps these assertions hermetic without
    depending on suite-wide thread teardown.
    """
    _UNDER_TEST_HOST = "192.168.1.50:8080"
    calls: list[dict] = []

    class _MockResp:
        def __init__(self, body: str) -> None:
            self.text = body
            self.status_code = 200

    def _fake_post(url, data=None, headers=None, timeout=10.0):
        # Pull the action name out of the SOAPAction header
        # (`"<service>#<Action>"`) so we can build a syntactically
        # valid empty response that invoke()'s parser accepts.
        soap_action = (headers or {}).get("SOAPAction", "")
        action = soap_action.strip('"').split("#", 1)[-1] or "Unknown"
        body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            f'<s:Body><u:{action}Response xmlns:u="x"/></s:Body>'
            '</s:Envelope>'
        )
        if _UNDER_TEST_HOST in (url or ""):
            calls.append(
                {
                    "url": url,
                    "data": data,
                    "action": action,
                    "headers": headers or {},
                }
            )
        return _MockResp(body)

    monkeypatch.setattr(requests, "post", _fake_post)
    return calls


@pytest.fixture
def mock_http_server(monkeypatch):
    """Replace `start_stream_http_server` with a stub. Real bind
    would touch the network and slow tests; we don't need it to
    test the SOAP handshake path."""
    fake_server = MagicMock()
    fake_server.server_address = ("0.0.0.0", 54321)

    def _fake_start(buffer, stream_path="/stream", content_type="audio/flac"):
        return fake_server

    monkeypatch.setattr("app.audio.upnp.start_stream_http_server", _fake_start)
    monkeypatch.setattr(
        "app.audio.upnp.primary_lan_ip", lambda: "192.168.1.10"
    )
    return fake_server


# ---------------------------------------------------------------------
# Discovery filter
# ---------------------------------------------------------------------


class TestFilterDlnaRenderer:
    def test_avtransport_v1_passes(self):
        assert _filter_dlna_renderer((
            "urn:schemas-upnp-org:service:AVTransport:1",
        )) is True

    def test_openhome_only_rejected(self):
        """A pure OpenHome device (no AVTransport) belongs to
        tidal_connect's discovery, not ours."""
        assert _filter_dlna_renderer((
            "urn:av-openhome-org:service:Playlist:1",
            "urn:av-openhome-org:service:Volume:1",
        )) is False

    def test_avtransport_v2_or_v3_still_passes(self):
        """The prefix match accepts any version."""
        assert _filter_dlna_renderer((
            "urn:schemas-upnp-org:service:AVTransport:3",
        )) is True

    def test_empty_service_list_rejected(self):
        assert _filter_dlna_renderer(()) is False


# ---------------------------------------------------------------------
# Connect handshake
# ---------------------------------------------------------------------


class TestConnect:
    def test_unknown_device_raises_value_error(self):
        mgr = UpnpManager()
        with pytest.raises(ValueError, match="unknown DLNA device"):
            mgr.connect("uuid:does-not-exist")

    def test_connect_issues_set_uri_then_play(
        self, mock_soap, mock_http_server, monkeypatch,
    ):
        """The handshake order matters: SetAVTransportURI MUST fire
        before Play, otherwise the device has no URL to pull from
        and Play returns either a 700 transition error or kicks
        the device into an undefined state. Verify the SOAP layer
        sees the calls in that order."""
        mgr = UpnpManager()
        device = _device_record()
        mgr._devices = {device.id: device}

        monkeypatch.setattr(
            "app.audio.upnp.fetch_device",
            lambda location, **_kw: _openhome_device(),
        )

        result = mgr.connect(device.id)
        assert result.id == device.id

        # The SOAP capture should have exactly two calls:
        # SetAVTransportURI then Play. (No other actions should
        # fire on connect.)
        actions = [c["action"] for c in mock_soap]
        assert actions == ["SetAVTransportURI", "Play"], (
            f"unexpected SOAP sequence: {actions}"
        )

    def test_connect_passes_lan_url_to_device(
        self, mock_soap, mock_http_server, monkeypatch,
    ):
        """The CurrentURI SOAP arg must be the LAN-reachable URL
        we just stood up. Localhost or 127.0.0.1 would not be
        reachable from the device on the LAN."""
        mgr = UpnpManager()
        device = _device_record()
        mgr._devices = {device.id: device}

        monkeypatch.setattr(
            "app.audio.upnp.fetch_device",
            lambda location, **_kw: _openhome_device(),
        )

        mgr.connect(device.id)
        # First call is SetAVTransportURI; pull the data and check
        # the URL we sent.
        set_uri_call = mock_soap[0]
        body = set_uri_call["data"].decode("utf-8")
        assert "192.168.1.10" in body, (
            f"LAN IP missing from URI; sent body: {body[:300]}"
        )
        assert "54321" in body, (
            f"HTTP server port missing from URI; sent body: {body[:300]}"
        )
        assert "/dlna/stream" in body, (
            f"stream path missing from URI; sent body: {body[:300]}"
        )

    def test_connect_rejects_device_without_avtransport(
        self, mock_soap, mock_http_server, monkeypatch,
    ):
        """If the device's descriptor has no AVTransport service
        (rare but possible. Device could have rebooted into a
        different profile between scan and connect), connect must
        fail with a clear message rather than build a controller
        out of None."""
        mgr = UpnpManager()
        device = _device_record()
        mgr._devices = {device.id: device}

        # Device with only Playlist (OpenHome-only); no AVTransport.
        openhome_only = OpenHomeDevice(
            udn="uuid:test-renderer",
            friendly_name="Test",
            manufacturer="",
            model_name="",
            model_number="",
            services=(_service(
                service_type="urn:av-openhome-org:service:Playlist:1",
                short_name="Playlist",
            ),),
        )
        monkeypatch.setattr(
            "app.audio.upnp.fetch_device",
            lambda location, **_kw: openhome_only,
        )

        with pytest.raises(RuntimeError, match="AVTransport"):
            mgr.connect(device.id)

        # No SOAP calls should have been issued; we bailed before
        # the handshake.
        assert mock_soap == []

    def test_connect_silencer_called_on_success(
        self, mock_soap, mock_http_server, monkeypatch,
    ):
        """The local audio output must be silenced when DLNA takes
        over, otherwise the user hears playback in stereo (local
        + remote with whatever LAN delay)."""
        mgr = UpnpManager()
        device = _device_record()
        mgr._devices = {device.id: device}

        monkeypatch.setattr(
            "app.audio.upnp.fetch_device",
            lambda location, **_kw: _openhome_device(),
        )

        silencer_calls: list[bool] = []
        mgr.set_local_silencer(silencer_calls.append)

        mgr.connect(device.id)
        assert silencer_calls == [True]

    def test_connect_handshake_failure_cleans_up_http_server(
        self, mock_soap, mock_http_server, monkeypatch,
    ):
        """If SetAVTransportURI raises mid-connect, the HTTP server
        we just spun up must be torn down. Otherwise the port
        leaks across retries."""
        mgr = UpnpManager()
        device = _device_record()
        mgr._devices = {device.id: device}

        monkeypatch.setattr(
            "app.audio.upnp.fetch_device",
            lambda location, **_kw: _openhome_device(),
        )

        # Force the FIRST SOAP call (SetAVTransportURI) to raise.
        def _raising_post(*args, **kwargs):
            raise RuntimeError("simulated SOAP failure")

        monkeypatch.setattr(requests, "post", _raising_post)

        with pytest.raises(RuntimeError):
            mgr.connect(device.id)

        # Manager must have torn down the http server (mock has
        # shutdown + server_close called).
        assert mock_http_server.shutdown.called
        assert mock_http_server.server_close.called
        # No session should be left dangling.
        assert mgr._session is None


# ---------------------------------------------------------------------
# Disconnect teardown
# ---------------------------------------------------------------------


class TestDisconnect:
    def test_disconnect_no_session_is_idempotent(self):
        mgr = UpnpManager()
        mgr.disconnect()  # Must not raise.
        assert mgr._session is None

    def test_disconnect_sends_avtransport_stop(
        self, mock_soap, mock_http_server, monkeypatch,
    ):
        """On graceful disconnect the device should receive
        AVTransport.Stop so it stops pulling our (now-dead) stream
        URL. Best-effort: a transport error doesn't escape."""
        mgr = UpnpManager()
        device = _device_record()
        mgr._devices = {device.id: device}

        monkeypatch.setattr(
            "app.audio.upnp.fetch_device",
            lambda location, **_kw: _openhome_device(),
        )

        mgr.connect(device.id)
        mock_soap.clear()  # Keep just the disconnect SOAP calls.

        mgr.disconnect()

        actions = [c["action"] for c in mock_soap]
        assert "Stop" in actions, f"Stop not sent on disconnect: {actions}"

    def test_disconnect_silencer_called_with_false(
        self, mock_soap, mock_http_server, monkeypatch,
    ):
        mgr = UpnpManager()
        device = _device_record()
        mgr._devices = {device.id: device}

        monkeypatch.setattr(
            "app.audio.upnp.fetch_device",
            lambda location, **_kw: _openhome_device(),
        )

        silencer_calls: list[bool] = []
        mgr.set_local_silencer(silencer_calls.append)

        mgr.connect(device.id)
        mgr.disconnect()
        assert silencer_calls == [True, False]


# ---------------------------------------------------------------------
# push_pcm: the realtime tap
# ---------------------------------------------------------------------


def _attach_session(mgr: UpnpManager) -> _SessionState:
    """Stuff a fake session into the manager. Skips the network-
    bound connect() path entirely. Used only by push_pcm tests
    that need a session but don't care about the SOAP handshake."""
    av_ctrl = MagicMock()
    rc_ctrl = MagicMock()
    sess = _SessionState(
        device=_device_record(),
        openhome_device=_openhome_device(),
        av=av_ctrl,
        rc=rc_ctrl,
    )
    mgr._session = sess
    return sess


class TestPushPcmNoSession:
    def test_no_session_returns_cheaply(self):
        """Audio callback hits push_pcm on every frame even when
        the user isn't using DLNA. Must not raise."""
        mgr = UpnpManager()
        pcm = np.zeros((512, 2), dtype=np.int16)
        mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        assert mgr._session is None


class TestPushPcmEncoderBuild:
    def test_first_chunk_builds_encoder(self):
        mgr = UpnpManager()
        sess = _attach_session(mgr)
        pcm = np.zeros((1024, 2), dtype=np.int16)
        mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        assert sess.encoder is not None
        assert sess.encoder_rate == 44100
        assert sess.encoder_channels == 2
        assert sess.encoder_dtype == "int16"

    def test_float32_converts_to_int32(self):
        """WASAPI shared mode delivers float32 PCM. The encoder is
        integer-only. push_pcm must convert before encoding."""
        mgr = UpnpManager()
        sess = _attach_session(mgr)
        pcm = np.full((512, 2), 0.5, dtype=np.float32)
        mgr.push_pcm(pcm, sample_rate=48000, dtype="float32")
        assert sess.encoder is not None
        assert sess.encoder_dtype == "int32"

    def test_intersample_peak_clips_safely(self):
        """A float32 sample > 1.0 (intersample overshoot from a
        brick-walled master) would overflow int32 if the scale
        wasn't clipped. push_pcm must not raise on these."""
        mgr = UpnpManager()
        _attach_session(mgr)
        pcm = np.array([[1.5, -1.5]] * 256, dtype=np.float32)
        mgr.push_pcm(pcm, sample_rate=44100, dtype="float32")  # no raise


class TestPushPcmEncoderReuse:
    def test_same_params_reuses_encoder(self):
        """The hot path: same rate / channels / dtype must NOT
        rebuild the encoder. A rebuild on every chunk produces
        audible glitches at every boundary."""
        mgr = UpnpManager()
        sess = _attach_session(mgr)
        pcm = np.zeros((1024, 2), dtype=np.int16)
        mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        first_encoder = sess.encoder
        for _ in range(50):
            mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        assert sess.encoder is first_encoder

    def test_rate_change_rebuilds(self):
        """Track-change to a different sample rate rebuilds the
        encoder. One discontinuity at the boundary; same behaviour
        the local engine has when it reopens its OutputStream
        across rate changes."""
        mgr = UpnpManager()
        sess = _attach_session(mgr)
        mgr.push_pcm(
            np.zeros((1024, 2), dtype=np.int16),
            sample_rate=44100, dtype="int16",
        )
        first = sess.encoder
        mgr.push_pcm(
            np.zeros((1024, 2), dtype=np.int16),
            sample_rate=96000, dtype="int16",
        )
        assert sess.encoder is not first
        assert sess.encoder_rate == 96000


class TestPushPcmEncodeFailureLatch:
    """A failing encoder (e.g. PyAV/FFmpeg dying on a device whose
    locale makes av.error mis-decode the error message) leaves the
    device connected to our stream URL with no audio ever arriving.
    push_pcm must not crash the realtime thread, but the FIRST failure
    has to be surfaced loudly so the symptom is diagnosable rather than
    buried at debug level. The `encode_failed` latch is what gates the
    one-shot loud report; a later success clears it.
    """

    def _attach_with_encoder(self, raises: bool):
        mgr = UpnpManager()
        sess = _attach_session(mgr)
        sess.encoder = MagicMock()
        sess.encoder_rate = 44100
        sess.encoder_channels = 2
        sess.encoder_dtype = "int16"
        if raises:
            sess.encoder.encode.side_effect = RuntimeError("ffmpeg boom")
        else:
            sess.encoder.encode.return_value = b""
        return mgr, sess

    def test_encode_failure_does_not_raise_and_latches(self):
        mgr, sess = self._attach_with_encoder(raises=True)
        pcm = np.zeros((1024, 2), dtype=np.int16)
        mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")  # no raise
        assert sess.encode_failed is True

    def test_repeated_failure_stays_latched(self):
        mgr, sess = self._attach_with_encoder(raises=True)
        pcm = np.zeros((1024, 2), dtype=np.int16)
        for _ in range(5):
            mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        assert sess.encode_failed is True

    def test_successful_encode_clears_latch(self):
        mgr, sess = self._attach_with_encoder(raises=True)
        pcm = np.zeros((1024, 2), dtype=np.int16)
        mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        assert sess.encode_failed is True
        # Encoder recovers and returns real bytes; the latch clears so
        # a future failure episode reports again.
        sess.encoder.encode.side_effect = None
        sess.encoder.encode.return_value = b"flacbytes"
        mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        assert sess.encode_failed is False
        assert sess.bytes_encoded == len(b"flacbytes")


class TestPushPcmEmpty:
    def test_empty_array_no_session_is_noop(self):
        mgr = UpnpManager()
        pcm = np.zeros((0, 2), dtype=np.int16)
        mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        assert mgr._session is None

    def test_empty_array_with_session_is_noop(self):
        """Empty input should not build an encoder. Defends
        against a wakeup on the audio callback before any decoder
        output has materialized."""
        mgr = UpnpManager()
        sess = _attach_session(mgr)
        pcm = np.zeros((0, 2), dtype=np.int16)
        mgr.push_pcm(pcm, sample_rate=44100, dtype="int16")
        assert sess.encoder is None


# ---------------------------------------------------------------------
# /api/dlna/refresh: NaN / inf rejection
# ---------------------------------------------------------------------


class TestRefreshEndpointNaNGuard:
    """Pydantic accepts NaN and Infinity for `float` by default. The
    handler rejects them explicitly because: (1) the Python-side
    `max(1.0, min(15.0, NaN))` clamp returns NaN (NaN comparisons
    are false), and (2) we can't use Pydantic `ge`/`le` constraints
    because FastAPI's default 422 error response includes the
    offending input value, and `json.dumps(NaN)` raises ValueError,
    turning a clean rejection into a 500 stack trace.
    """

    @pytest.fixture
    def client(self):
        import server  # noqa: WPS433
        from fastapi.testclient import TestClient

        # `/api/dlna/refresh` is gated by `_require_local_access`. In
        # CI, default settings have offline_mode=False, so the request
        # gets a 401 before our handler ever runs and we can't observe
        # the NaN guard. Flip it on for the test scope and restore.
        original = server.settings.offline_mode
        server.settings.offline_mode = True
        try:
            with TestClient(server.app) as c:
                yield c
        finally:
            server.settings.offline_mode = original

    @pytest.mark.parametrize("body_value", ["NaN", "Infinity", "-Infinity"])
    def test_rejects_nan_and_infinity(self, client, body_value):
        r = client.request(
            "POST",
            "/api/dlna/refresh",
            content=f'{{"timeout_s": {body_value}}}',
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400, r.text
        assert "finite" in r.json()["detail"].lower()
