"""Tests for the real Tidal Connect controller skeleton.

The wire-level pieces (TLS context, command frame shapes, request/
response correlation, discovery service constant) are testable
without hardware. The full WSS round-trip against a real device
isn't, and waits for a contributor with hardware.

Once captured-fixture frames from a real session are available,
add a `tests/fixtures/tidal_connect_real/` tree of JSON frames and
extend these tests to drive the receiver loop against them.
"""
from __future__ import annotations

import asyncio
import json
import ssl
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.audio import tidal_connect_real as tcr


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------


def test_service_type_matches_desktop_client():
    """The desktop client browses `_tidalconnect._tcp.local`. Anything
    else and our discovery sees zero devices."""
    assert tcr.TIDAL_CONNECT_SERVICE == "_tidalconnect._tcp.local."


def test_ca_bundle_loads_and_carries_two_certs():
    """The desktop client embeds TIDAL Root CA + TIDAL TS CA. If
    either is missing or malformed, every device connection fails
    TLS validation. Loading the bundle into an SSLContext is the
    cheapest way to confirm both certs parse and chain."""
    ctx = tcr._build_ssl_context()
    certs = ctx.get_ca_certs()
    assert len(certs) == 2
    cns = []
    for c in certs:
        # Subject is a tuple of RDN tuples; flatten and pull CN.
        for rdn in c.get("subject", ()):
            for attr, value in rdn:
                if attr == "commonName":
                    cns.append(value)
    assert "TIDAL Root CA" in cns
    assert "TIDAL TS CA" in cns


def test_ssl_context_skips_hostname_verification():
    """Tidal Connect devices serve certs with IP-based identities,
    not DNS names. Hostname verification has to be off, otherwise
    the WSS handshake fails the moment we connect."""
    ctx = tcr._build_ssl_context()
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_REQUIRED


# ---------------------------------------------------------------------------
# Frame builders (via a connection in isolation)
# ---------------------------------------------------------------------------


def _make_connection_with_fake_socket() -> tuple[
    tcr.TidalConnectConnection, MagicMock
]:
    """Construct a connection wired to a mock WebSocket so we can
    observe outbound frames without opening a real connection."""
    device = tcr.DiscoveredDevice(
        id="test", name="Test Device", address="192.168.1.50", port=12345
    )
    conn = tcr.TidalConnectConnection(
        device=device,
        token_provider=lambda: "test-token",
        on_notification=lambda _: None,
    )
    fake_socket = MagicMock()
    fake_socket.send = AsyncMock()
    conn._websocket = fake_socket
    return conn, fake_socket


def _last_sent_frame(fake_socket: MagicMock) -> dict:
    args, _ = fake_socket.send.call_args
    return json.loads(args[0])


def _drive_send(coro):
    """Run a connection coroutine to first await without waiting on
    the response future. The test resolves the future manually."""
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(coro)
        # Yield once so the await self._websocket.send(...) lands.
        loop.run_until_complete(asyncio.sleep(0))
        return loop, task
    except BaseException:
        loop.close()
        raise


def test_play_frame_shape():
    conn, fake = _make_connection_with_fake_socket()
    loop, task = _drive_send(conn.play())
    try:
        frame = _last_sent_frame(fake)
        assert frame["command"] == "play"
        assert "requestId" in frame
        # Resolve the pending future so the task can complete.
        rid = frame["requestId"]
        conn._pending[rid].set_result({"requestId": rid, "ok": True})
        loop.run_until_complete(task)
    finally:
        loop.close()


def test_seek_frame_carries_position():
    conn, fake = _make_connection_with_fake_socket()
    loop, task = _drive_send(conn.seek(45_000))
    try:
        frame = _last_sent_frame(fake)
        assert frame["command"] == "seek"
        assert frame["position"] == 45_000
        rid = frame["requestId"]
        conn._pending[rid].set_result({"requestId": rid})
        loop.run_until_complete(task)
    finally:
        loop.close()


def test_set_volume_clamps_to_int():
    """The desktop client's setVolume accepts an integer; if a
    caller passes a float we coerce so the device sees the right
    JSON type."""
    conn, fake = _make_connection_with_fake_socket()
    loop, task = _drive_send(conn.set_volume(33.7))  # type: ignore[arg-type]
    try:
        frame = _last_sent_frame(fake)
        assert frame["command"] == "setVolume"
        assert frame["level"] == 33
        assert isinstance(frame["level"], int)
        rid = frame["requestId"]
        conn._pending[rid].set_result({"requestId": rid})
        loop.run_until_complete(task)
    finally:
        loop.close()


def test_request_id_increments_per_send():
    """Each command must carry a fresh requestId so concurrent
    in-flight requests can be matched to their responses without
    collision."""
    conn, fake = _make_connection_with_fake_socket()
    loop = asyncio.new_event_loop()
    try:
        ids_seen: list[int] = []
        for _ in range(3):
            task = loop.create_task(conn.play())
            loop.run_until_complete(asyncio.sleep(0))
            frame = json.loads(fake.send.call_args[0][0])
            ids_seen.append(frame["requestId"])
            conn._pending[frame["requestId"]].set_result(
                {"requestId": frame["requestId"]}
            )
            loop.run_until_complete(task)
        assert ids_seen == sorted(ids_seen)
        assert len(set(ids_seen)) == 3
    finally:
        loop.close()


def test_set_repeat_mode_uses_string_enum():
    """The desktop client's RepeatMode is a string enum: OFF / ALL
    / SINGLE. We forward whatever the caller passes; this test
    just confirms the field name and that the value is sent
    verbatim."""
    conn, fake = _make_connection_with_fake_socket()
    loop, task = _drive_send(conn.set_repeat_mode("ALL"))
    try:
        frame = _last_sent_frame(fake)
        assert frame["command"] == "setRepeatMode"
        assert frame["repeatMode"] == "ALL"
        rid = frame["requestId"]
        conn._pending[rid].set_result({"requestId": rid})
        loop.run_until_complete(task)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Manager singleton
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_module_singleton():
    tcr._manager = None
    yield
    if tcr._manager is not None:
        try:
            tcr._manager.stop()
        except Exception:
            pass
        tcr._manager = None


def test_get_manager_returns_none_until_started():
    assert tcr.get_manager() is None


def test_start_manager_is_idempotent():
    m1 = tcr.start_manager(
        token_provider=lambda: None,
        on_notification=lambda _: None,
    )
    m2 = tcr.start_manager(
        token_provider=lambda: None,
        on_notification=lambda _: None,
    )
    assert m1 is m2
    assert tcr.get_manager() is m1
