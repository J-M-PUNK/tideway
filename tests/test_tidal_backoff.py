"""Tests for the Tidal request-gate backoff state machine.

Doesn't touch tidalapi or the network — exercises the module-level
state via the public helpers. Resets the state before each test so
ordering doesn't matter.
"""
import time

from app import tidal_client


def test_initial_state_is_inactive():
    state = tidal_client.tidal_backoff_state()
    assert state["active"] is False
    assert state["seconds_remaining"] == 0.0
    assert state["reason"] == ""


def test_trigger_engages_backoff():
    tidal_client._trigger_tidal_backoff(60.0, "rate-limited (HTTP 429)")
    state = tidal_client.tidal_backoff_state()
    assert state["active"] is True
    assert 55.0 < state["seconds_remaining"] <= 60.0
    assert "rate-limited" in state["reason"]


def test_longer_backoff_replaces_shorter():
    """A 60-second rate-limit shouldn't shrink an already-active
    30-minute abuse backoff."""
    tidal_client._trigger_tidal_backoff(1800.0, "abuse")
    tidal_client._trigger_tidal_backoff(60.0, "soft 429")

    state = tidal_client.tidal_backoff_state()
    assert state["seconds_remaining"] > 1700.0
    assert state["reason"] == "abuse"


def test_shorter_backoff_does_not_extend_longer():
    tidal_client._trigger_tidal_backoff(60.0, "soft 429")
    tidal_client._trigger_tidal_backoff(1800.0, "abuse")

    state = tidal_client.tidal_backoff_state()
    assert state["seconds_remaining"] > 1700.0
    assert state["reason"] == "abuse"


def test_backoff_clears_after_expiry(monkeypatch):
    tidal_client._trigger_tidal_backoff(60.0, "rate-limit")
    # Simulate time passing past the expiry window.
    original_time = time.time
    monkeypatch.setattr(time, "time", lambda: original_time() + 120.0)

    state = tidal_client.tidal_backoff_state()
    assert state["active"] is False
    assert state["seconds_remaining"] == 0.0


def test_backoff_error_carries_reason():
    err = tidal_client.TidalBackoffError(42.0, "abuse-detected")
    assert err.seconds_remaining == 42.0
    assert err.reason == "abuse-detected"
    assert "42" in str(err)
    assert "abuse-detected" in str(err)
