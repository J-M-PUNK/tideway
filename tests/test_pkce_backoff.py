"""Tests for the PKCE token-endpoint backoff path.

tidalapi's `pkce_get_auth_token` hits `auth.tidal.com/v1/oauth2/token`
via its own `requests.post` that doesn't pass through the gate on
`session.request.basic_request`. These tests verify that we still
engage the module-level backoff when that endpoint responds with a
rate-limit or abuse signal — the exact bug that banned the dev
account during login iteration.
"""
import pytest

from app import tidal_client
from app.tidal_client import TidalClient


class _FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def _HTTPError(status_code: int, text: str) -> Exception:
    err = Exception(f"simulated HTTPError {status_code}")
    err.response = _FakeResponse(status_code, text)
    return err


def test_token_endpoint_abuse_403_engages_30min_backoff(monkeypatch):
    """This is the exact Tidal response that banned us during PKCE
    debugging."""
    client = TidalClient()
    client._refresh_stop.set()

    def raise_abuse(*a, **kw):
        raise _HTTPError(
            403,
            '{"error":"abuse_detected",'
            '"error_description":"User account is suspended due to abuse",'
            '"status":403,"sub_status":12001}',
        )

    monkeypatch.setattr(
        client.session, "pkce_get_auth_token", raise_abuse
    )

    ok, reason = client.complete_pkce_login(
        "https://tidal.com/android/login/auth?code=FAKE"
    )

    assert ok is False
    state = tidal_client.tidal_backoff_state()
    assert state["active"] is True
    assert state["seconds_remaining"] > 29 * 60
    assert "abuse" in state["reason"].lower()


def test_token_endpoint_429_engages_60s_backoff(monkeypatch):
    client = TidalClient()
    client._refresh_stop.set()

    def raise_429(*a, **kw):
        raise _HTTPError(429, "rate-limited")

    monkeypatch.setattr(client.session, "pkce_get_auth_token", raise_429)

    ok, _ = client.complete_pkce_login(
        "https://tidal.com/android/login/auth?code=FAKE"
    )

    assert ok is False
    state = tidal_client.tidal_backoff_state()
    assert state["active"] is True
    assert 55 < state["seconds_remaining"] <= 60


def test_token_endpoint_plain_403_does_not_engage_backoff(monkeypatch):
    """A 403 without abuse/suspended in the body is an ordinary
    authorization failure — user pasted an already-used code,
    expired code, wrong client_id, etc. Those deserve a clean
    error message, not a 30-minute lockout."""
    client = TidalClient()
    client._refresh_stop.set()

    def raise_invalid_grant(*a, **kw):
        raise _HTTPError(
            403,
            '{"error":"invalid_grant","error_description":"code expired"}',
        )

    monkeypatch.setattr(
        client.session, "pkce_get_auth_token", raise_invalid_grant
    )

    ok, _ = client.complete_pkce_login(
        "https://tidal.com/android/login/auth?code=FAKE"
    )

    assert ok is False
    assert tidal_client.tidal_backoff_state()["active"] is False


def test_complete_refuses_during_active_backoff(monkeypatch):
    """Once engaged, subsequent Continue clicks shouldn't re-hit
    the token endpoint — the account is already under Tidal's
    watch and more requests only extend the window."""
    client = TidalClient()
    client._refresh_stop.set()

    calls = []

    def tracking_post(*a, **kw):
        calls.append(1)
        raise _HTTPError(200, "unreachable")

    monkeypatch.setattr(client.session, "pkce_get_auth_token", tracking_post)

    tidal_client._trigger_tidal_backoff(120.0, "pre-existing backoff")

    ok, reason = client.complete_pkce_login(
        "https://tidal.com/android/login/auth?code=FAKE"
    )

    assert ok is False
    assert "holding us off" in reason.lower()
    assert calls == []  # token endpoint was never reached
