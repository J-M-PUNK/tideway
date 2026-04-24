"""Tests for the Tidal request gate — the primary ban-protection
mechanism.

The gate wraps tidalapi's Requests.basic_request so every HTTP call
is inspected for 429 / abuse-detected 403 and, when tripped, refuses
subsequent calls during the cooldown window. This module tests the
wrapper behaviour with a stub session so nothing touches the network.
"""
import time

import pytest

from app import tidal_client
from app.tidal_client import (
    TidalBackoffError,
    _install_tidal_request_gate,
    _trigger_tidal_backoff,
    tidal_backoff_state,
    tidal_jitter_sleep,
)


class _StubResponse:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


class _StubRequests:
    def __init__(self, responder):
        self.basic_request = responder


class _StubSession:
    def __init__(self, responder):
        self.request = _StubRequests(responder)


# --- 2xx pass-through --------------------------------------------------

def test_gate_passes_through_200():
    calls = []

    def responder(method, path, **kw):
        calls.append((method, path))
        return _StubResponse(200, text="{}")

    session = _StubSession(responder)
    _install_tidal_request_gate(session)

    resp = session.request.basic_request("GET", "/v1/tracks/123")

    assert resp.status_code == 200
    assert calls == [("GET", "/v1/tracks/123")]
    assert tidal_backoff_state()["active"] is False


# --- 429 → 60s backoff -------------------------------------------------

def test_gate_engages_60s_backoff_on_429():
    session = _StubSession(lambda *a, **kw: _StubResponse(429))
    _install_tidal_request_gate(session)

    resp = session.request.basic_request("GET", "/v1/tracks/123")

    assert resp.status_code == 429
    state = tidal_backoff_state()
    assert state["active"] is True
    assert 55.0 <= state["seconds_remaining"] <= 60.0
    assert "429" in state["reason"]


# --- abuse-detected 403 → 30min backoff --------------------------------

def test_gate_engages_30min_backoff_on_abuse_403():
    body = (
        '{"error":"abuse_detected",'
        '"error_description":"User account is suspended due to abuse",'
        '"status":403,"sub_status":12001}'
    )
    session = _StubSession(lambda *a, **kw: _StubResponse(403, text=body))
    _install_tidal_request_gate(session)

    resp = session.request.basic_request("POST", "/v1/oauth2/token")

    assert resp.status_code == 403
    state = tidal_backoff_state()
    assert state["active"] is True
    # 30-minute window, so > 29 minutes remaining.
    assert state["seconds_remaining"] > 29 * 60
    assert "abuse" in state["reason"].lower()


def test_gate_engages_backoff_on_suspended_403():
    """403 payloads that mention 'suspended' should also trigger the
    long backoff — Tidal's fraud layer varies the phrasing."""
    body = '{"error":"account_suspended","error_description":"Account suspended"}'
    session = _StubSession(lambda *a, **kw: _StubResponse(403, text=body))
    _install_tidal_request_gate(session)

    session.request.basic_request("GET", "/v1/any")

    state = tidal_backoff_state()
    assert state["active"] is True
    assert state["seconds_remaining"] > 29 * 60


def test_gate_does_not_engage_on_plain_403():
    """A 403 without abuse/suspended in the body shouldn't trip a
    30-minute backoff — could be an ordinary authorization problem."""
    body = '{"error":"forbidden","error_description":"Not your resource"}'
    session = _StubSession(lambda *a, **kw: _StubResponse(403, text=body))
    _install_tidal_request_gate(session)

    resp = session.request.basic_request("GET", "/v1/users/other")

    assert resp.status_code == 403
    assert tidal_backoff_state()["active"] is False


# --- refusal during backoff --------------------------------------------

def test_gate_refuses_calls_during_active_backoff():
    calls = []

    def responder(*a, **kw):
        calls.append(1)
        return _StubResponse(200)

    session = _StubSession(responder)
    _install_tidal_request_gate(session)
    _trigger_tidal_backoff(60.0, "simulated 429")

    with pytest.raises(TidalBackoffError) as exc_info:
        session.request.basic_request("GET", "/v1/anything")

    assert calls == []  # underlying responder never reached
    assert "simulated 429" in exc_info.value.reason
    assert exc_info.value.seconds_remaining > 0


# --- resume after expiry -----------------------------------------------

def test_gate_resumes_after_backoff_expires(monkeypatch):
    session = _StubSession(lambda *a, **kw: _StubResponse(200))
    _install_tidal_request_gate(session)
    _trigger_tidal_backoff(60.0, "simulated")

    original_time = time.time
    monkeypatch.setattr(time, "time", lambda: original_time() + 120.0)

    resp = session.request.basic_request("GET", "/v1/tracks")
    assert resp.status_code == 200


# --- jitter sleep ------------------------------------------------------

def test_jitter_sleep_stays_within_bounds(monkeypatch):
    """Jitter should never block for more than a quarter-second or
    skip the sleep entirely. Monkeypatch time.sleep so the test
    doesn't actually wait."""
    observed = []

    def fake_sleep(seconds: float) -> None:
        observed.append(seconds)

    monkeypatch.setattr(time, "sleep", fake_sleep)

    # Run many times to span the random range.
    for _ in range(1000):
        tidal_jitter_sleep()

    assert len(observed) == 1000
    assert all(0.0 < s <= 0.25 for s in observed)
    # Range must actually vary — otherwise the jitter isn't decorrelating.
    assert max(observed) > 0.15
    assert min(observed) < 0.10
