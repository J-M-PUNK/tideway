"""Tests for the Tidal realtime listener scaffold.

The actual WebSocket connection logic depends on protocol constants
that aren't filled in yet (see app/tidal_realtime.py — Phase 1
capture pending), so these tests focus on the parts that exist:
listener construction, start/stop idempotency, status reporting,
and the singleton helpers.

Once the protocol capture lands and `_connect_and_serve` is real,
add tests with a mocked `websockets.connect` that exercise the
parser against captured-frame fixtures and the reconnect logic
under simulated drops.
"""
from __future__ import annotations

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
    """Clear the module-level listener between tests so order
    doesn't matter and a leaked listener from one test can't
    influence another."""
    tidal_realtime._listener = None
    yield
    if tidal_realtime._listener is not None:
        tidal_realtime._listener.stop()
        tidal_realtime._listener = None


def _noop_token():
    return "dummy"


def _noop_callback(payload):  # noqa: D401
    return None


def test_listener_constructs_with_idle_phase():
    """A freshly-built listener that hasn't been started reports
    `idle` (not `disabled`); `disabled` is reserved for the
    "protocol unknown so we won't even try" branch."""
    listener = RealtimeListener(
        token_provider=_noop_token,
        on_other_device_started=_noop_callback,
    )
    s = listener.status()
    assert s.phase == "idle"
    assert s.last_error is None
    assert s.reconnect_count == 0
    assert s.events_received == 0


def test_start_no_ops_when_protocol_unknown():
    """Until Phase 1 capture lands, start() should refuse to open a
    WebSocket and report `disabled`. Tested by relying on the
    placeholder constants (which the production module ships with
    until the capture is filled in)."""
    listener = RealtimeListener(
        token_provider=_noop_token,
        on_other_device_started=_noop_callback,
    )
    listener.start()
    # No task should have been spawned.
    assert listener._task is None
    s = listener.status()
    assert s.phase == "disabled"
    assert s.last_error is not None
    assert "Phase 1" in s.last_error


def test_stop_is_idempotent():
    """Calling stop() multiple times shouldn't raise. Important
    because lifespan teardown can fire stop() even when start()
    was a no-op."""
    listener = RealtimeListener(
        token_provider=_noop_token,
        on_other_device_started=_noop_callback,
    )
    listener.stop()
    listener.stop()
    assert listener.status().phase == "stopped"


def test_status_returns_a_fresh_snapshot_each_call():
    """status() should return a new ListenerStatus, not the
    listener's internal mutable instance. Caller should not be
    able to poison internal state by mutating the returned dict."""
    listener = RealtimeListener(
        token_provider=_noop_token,
        on_other_device_started=_noop_callback,
    )
    s1 = listener.status()
    s2 = listener.status()
    assert s1 is not s2
    s1.phase = "tampered"
    assert listener.status().phase != "tampered"


def test_module_singleton_helpers():
    """get_listener / start_listener / stop_listener cooperate so
    lifespan startup gets a working singleton without each call
    re-constructing."""
    assert get_listener() is None
    listener = start_listener(
        token_provider=_noop_token,
        on_other_device_started=_noop_callback,
    )
    assert get_listener() is listener
    # Second start_listener call returns the same instance.
    again = start_listener(
        token_provider=lambda: "ignored",
        on_other_device_started=_noop_callback,
    )
    assert again is listener
    stop_listener()
    # After stop, the singleton stays around for status reads (per
    # the realtime_status endpoint contract); only the running task
    # is cancelled.
    assert get_listener() is listener


def test_protocol_constants_are_documented_placeholders():
    """If someone fills in the protocol constants without also
    deleting the disabled-phase branch, this test catches it. The
    public `is_protocol_known` flag is the single source of truth
    for "we know how to talk to the bus."""
    listener = RealtimeListener(
        token_provider=_noop_token,
        on_other_device_started=_noop_callback,
    )
    if tidal_realtime._REALTIME_URL is None:
        assert listener.is_protocol_known is False
    else:
        # Once the capture lands and the URL is set, the listener
        # should report the protocol as known and the disabled
        # phase should no longer be reachable from start().
        assert listener.is_protocol_known is True
