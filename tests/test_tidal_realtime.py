"""Tests for the Tidal realtime listener.

The WebSocket loop itself does live network IO against
api.tidal.com and pushkin-v2.tidal.com, so it isn't exercised in
unit tests. What is covered:

  - Listener lifecycle: construction, start/stop idempotency,
    fresh-snapshot semantics on status().
  - Module-level singleton (start_listener / get_listener /
    stop_listener).
  - Frame parser: valid JSON dict, garbage JSON, non-dict JSON.
  - Frame dispatch: PRIVILEGED_SESSION_NOTIFICATION fires the
    callback, RECONNECT closes the socket, sync and async
    callbacks are both handled.

The full connect loop is best tested as a manual integration
check against a real Tidal account, since faking the entire
api.tidal.com + Pushkin handshake is more code than the loop
itself.
"""
from __future__ import annotations

import asyncio

import pytest

from app import tidal_realtime
from app.tidal_realtime import (
    RealtimeListener,
    get_listener,
    start_listener,
    stop_listener,
)


@pytest.fixture(autouse=True)
def _reset_module_singleton():
    tidal_realtime._listener = None
    yield
    if tidal_realtime._listener is not None:
        tidal_realtime._listener.stop()
        tidal_realtime._listener = None


def _noop_token():
    return "dummy"


def _noop_callback(payload):  # noqa: D401
    return None


def _listener() -> RealtimeListener:
    return RealtimeListener(
        token_provider=_noop_token,
        on_other_device_started=_noop_callback,
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_listener_constructs_with_idle_phase():
    s = _listener().status()
    assert s.phase == "idle"
    assert s.last_error is None
    assert s.reconnect_count == 0
    assert s.events_received == 0


def test_is_protocol_known_is_true():
    """The Phase 1 capture landed; the listener can connect on its
    own. `is_protocol_known` is kept on the surface for the
    /api/realtime/status diagnostic but is constant True now."""
    assert _listener().is_protocol_known is True


def test_stop_is_idempotent():
    """Multiple stop() calls must not raise — lifespan teardown can
    fire it even when start() was never called."""
    listener = _listener()
    listener.stop()
    listener.stop()
    assert listener.status().phase == "stopped"


def test_status_returns_a_fresh_snapshot_each_call():
    """status() must return a new dataclass so callers can't
    poison internal state by mutating it."""
    listener = _listener()
    s1 = listener.status()
    s2 = listener.status()
    assert s1 is not s2
    s1.phase = "tampered"
    assert listener.status().phase != "tampered"


def test_module_singleton_helpers(monkeypatch):
    """start_listener() spawns an asyncio task, which needs a
    running loop — under pytest there isn't one. Patch start() to
    a no-op for the duration of this test so we can exercise the
    singleton wiring without standing up an event loop."""
    monkeypatch.setattr(RealtimeListener, "start", lambda self: None)
    assert get_listener() is None
    listener = start_listener(
        token_provider=_noop_token,
        on_other_device_started=_noop_callback,
    )
    assert get_listener() is listener
    again = start_listener(
        token_provider=lambda: "ignored",
        on_other_device_started=_noop_callback,
    )
    assert again is listener
    stop_listener()
    # Singleton stays around for status reads after stop; only the
    # running task is cancelled.
    assert get_listener() is listener


# ---------------------------------------------------------------------------
# Frame parser
# ---------------------------------------------------------------------------


def test_parse_frame_decodes_str_json():
    raw = (
        '{"type":"PRIVILEGED_SESSION_NOTIFICATION",'
        '"payload":{"clientDisplayName":"iOS",'
        '"sessionId":"0d8fecb2-dcdb-4555-97b0-7c807806e4ad"}}'
    )
    frame = RealtimeListener._parse_frame(raw)
    assert frame is not None
    assert frame["type"] == "PRIVILEGED_SESSION_NOTIFICATION"
    assert frame["payload"]["clientDisplayName"] == "iOS"
    assert (
        frame["payload"]["sessionId"]
        == "0d8fecb2-dcdb-4555-97b0-7c807806e4ad"
    )


def test_parse_frame_decodes_bytes_json():
    raw = b'{"type":"RECONNECT","payload":{}}'
    frame = RealtimeListener._parse_frame(raw)
    assert frame is not None
    assert frame["type"] == "RECONNECT"


def test_parse_frame_returns_none_on_garbage():
    assert RealtimeListener._parse_frame("not json at all") is None
    assert RealtimeListener._parse_frame(b"\xff\xfe\x00\x01") is None


def test_parse_frame_rejects_non_dict_top_level():
    """JSON arrays / strings / numbers parse fine but aren't routable
    frames. Treat as garbage and skip."""
    assert RealtimeListener._parse_frame('["array"]') is None
    assert RealtimeListener._parse_frame('"string"') is None
    assert RealtimeListener._parse_frame("42") is None


# ---------------------------------------------------------------------------
# Frame dispatch
# ---------------------------------------------------------------------------


class _FakeWS:
    """Bare-minimum stand-in for the websockets.connect coroutine's
    context. Records close() calls so tests can assert on RECONNECT
    handling without standing up a real WebSocket."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_dispatch_fires_callback_on_session_notification():
    received: list = []

    def cb(payload):
        received.append(payload)

    listener = RealtimeListener(
        token_provider=_noop_token,
        on_other_device_started=cb,
    )
    frame = {
        "type": "PRIVILEGED_SESSION_NOTIFICATION",
        "payload": {
            "clientDisplayName": "iOS",
            "sessionId": "abc",
        },
    }

    ws = _FakeWS()
    asyncio.run(listener._dispatch_frame(frame, ws))

    assert len(received) == 1
    assert received[0]["clientDisplayName"] == "iOS"
    assert listener.status().events_received == 1
    assert ws.closed is False


def test_dispatch_awaits_async_callback():
    """An async callback should be awaited, not left as a stray
    coroutine. The web client's pattern is sync (player.pause()) but
    the type hints allow async for callers that want to fan the
    pause out across multiple players."""
    received: list = []

    async def cb(payload):
        await asyncio.sleep(0)
        received.append(payload)

    listener = RealtimeListener(
        token_provider=_noop_token,
        on_other_device_started=cb,
    )
    frame = {"type": "PRIVILEGED_SESSION_NOTIFICATION", "payload": {}}
    asyncio.run(listener._dispatch_frame(frame, _FakeWS()))
    assert len(received) == 1


def test_dispatch_closes_socket_on_reconnect_frame():
    """RECONNECT means the server wants us to drop the current
    socket and reopen. _run()'s outer loop handles the reopen; here
    we just verify close() is called."""
    listener = _listener()
    ws = _FakeWS()
    asyncio.run(
        listener._dispatch_frame({"type": "RECONNECT"}, ws)
    )
    assert ws.closed is True


def test_dispatch_callback_exception_is_swallowed():
    """A buggy callback must not break the receive loop. We log and
    keep going so a transient callback failure doesn't permanently
    disable cross-device pause until the next reconnect."""

    def boom(payload):
        raise RuntimeError("callback explosion")

    listener = RealtimeListener(
        token_provider=_noop_token,
        on_other_device_started=boom,
    )
    frame = {"type": "PRIVILEGED_SESSION_NOTIFICATION", "payload": {}}
    # If the exception leaked, this would raise.
    asyncio.run(listener._dispatch_frame(frame, _FakeWS()))
    # The events_received counter still increments — we DID receive
    # the frame, the action just failed.
    assert listener.status().events_received == 1


def test_dispatch_ignores_unknown_frame_type():
    """Forward-compat: a future Pushkin event type we don't handle
    yet shouldn't crash or close the socket. Logged at warning level
    so it's visible if it ever happens."""
    listener = _listener()
    ws = _FakeWS()
    asyncio.run(
        listener._dispatch_frame(
            {"type": "SOME_FUTURE_EVENT", "payload": {}}, ws
        )
    )
    assert ws.closed is False
    assert listener.status().events_received == 0
