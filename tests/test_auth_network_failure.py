"""`_is_logged_in` must distinguish "the network is unreachable" from
"Tidal rejected the session".

check_login() returns False on its own (no round-trip) when the session
has no credentials, so a connection-level exception can only happen for
a session that IS signed in. Turning that into "logged out" 401'd the
local-only endpoints (downloaded library, cached playback) the moment
the wifi dropped and flashed the login screen at a signed-in user
(#261). Same for a Tidal rate-limit backoff window, which refuses the
check before it leaves the process.
"""
from __future__ import annotations

import curl_cffi.requests.exceptions as curl_exc
import requests.exceptions as req_exc
import pytest

import server
from app.tidal_client import TidalBackoffError


@pytest.fixture(autouse=True)
def _fresh_auth_cache():
    server._session_ready.set()
    server._invalidate_auth_cache()
    yield
    server._invalidate_auth_cache()


def _check(monkeypatch, effect) -> bool:
    def check_login():
        if isinstance(effect, BaseException):
            raise effect
        return effect

    monkeypatch.setattr(server.tidal.session, "check_login", check_login)
    return server._is_logged_in()


@pytest.mark.parametrize(
    "exc",
    [
        req_exc.ConnectionError("dns down"),
        req_exc.ReadTimeout("stalled"),
        curl_exc.ConnectionError("dns down"),
        curl_exc.Timeout("stalled"),
        TidalBackoffError(60.0, "rate_limited"),
    ],
)
def test_network_failure_keeps_session_signed_in(monkeypatch, exc):
    assert _check(monkeypatch, exc) is True


def test_auth_rejection_still_signs_out(monkeypatch):
    assert _check(monkeypatch, False) is False


def test_unexpected_error_still_signs_out(monkeypatch):
    assert _check(monkeypatch, ValueError("boom")) is False
