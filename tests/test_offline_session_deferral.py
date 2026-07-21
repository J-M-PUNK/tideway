"""Launching with no network must not look like being signed out.

`load_oauth_session` validates a restored session with a round-trip to
Tidal. Offline, that raises, so `session.user` and `session.session_id`
are never populated — and from then on tidalapi's `check_login()`
returns False *without* raising, because it short-circuits on those
attributes before it would make a request. The connection-error guard in
`_is_logged_in` therefore never fires, the user is reported signed out,
and `_require_local_access` 401s the on-disk library the offline mode
exists to serve (#292).

These tests pin the flag that tells the two cases apart, and the
watchdog retry that gets the session back once the network returns —
without it, a boot with no network leaves the session dead for the rest
of the process.
"""
from __future__ import annotations

import json

import curl_cffi.requests.exceptions as curl_exc
import pytest
import requests.exceptions as req_exc

from app import tidal_client
from app.tidal_client import TidalBackoffError, TidalClient


@pytest.fixture
def client():
    c = TidalClient()
    # Stop the watchdog thread; these tests drive the retry directly.
    c._refresh_stop.set()
    return c


@pytest.fixture
def session_file(tmp_path, monkeypatch):
    path = tmp_path / "tidal_session.json"
    path.write_text(
        json.dumps(
            {
                "token_type": "Bearer",
                "access_token": "at",
                "refresh_token": "rt",
                "expiry_time": "2099-01-01T00:00:00",
                "is_pkce": False,
            }
        )
    )
    monkeypatch.setattr(tidal_client, "SESSION_FILE", path)
    return path


@pytest.mark.parametrize(
    "exc",
    [
        req_exc.ConnectionError("dns down"),
        req_exc.ReadTimeout("stalled"),
        curl_exc.ConnectionError("dns down"),
        curl_exc.Timeout("stalled"),
    ],
)
def test_network_failure_defers_the_load(client, session_file, monkeypatch, exc):
    def unreachable(*a, **kw):
        raise exc

    monkeypatch.setattr(client.session, "load_oauth_session", unreachable)

    assert client.load_session() is False
    assert client.session_load_deferred() is True


def test_backoff_window_defers_the_load(client, session_file, monkeypatch):
    """A rate-limit / abuse backoff refuses the call inside our own
    request gate, before any HTTP leaves the process. The session is
    every bit as intact as it is when the network is down, and the
    abuse window runs 30 minutes — long enough to still be open at the
    next launch — so this must defer and retry too, not report the user
    signed out until they restart."""

    def gated(*a, **kw):
        raise TidalBackoffError(1800.0, "abuse-detected (HTTP 403)")

    monkeypatch.setattr(client.session, "load_oauth_session", gated)

    assert client.load_session() is False
    assert client.session_load_deferred() is True


def test_rejected_session_is_not_deferred(client, session_file, monkeypatch):
    """Tidal answered and said no. Retrying that on a timer would be
    pointless, and reporting it as offline would hide a real logout."""
    monkeypatch.setattr(
        client.session, "load_oauth_session", lambda *a, **kw: True
    )
    monkeypatch.setattr(client.session, "check_login", lambda: False)

    assert client.load_session() is False
    assert client.session_load_deferred() is False


def test_successful_load_clears_the_flag(client, session_file, monkeypatch):
    client._session_load_deferred = True
    monkeypatch.setattr(
        client.session, "load_oauth_session", lambda *a, **kw: True
    )
    monkeypatch.setattr(client.session, "check_login", lambda: True)

    assert client.load_session() is True
    assert client.session_load_deferred() is False


def test_watchdog_retry_recovers_the_session(client, session_file, monkeypatch):
    """The whole point of the flag: once the network is back, the load
    that was skipped at boot has to actually happen. Nothing else in the
    process re-runs it."""
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise req_exc.ConnectionError("dns down")
        return True

    monkeypatch.setattr(client.session, "load_oauth_session", flaky)
    monkeypatch.setattr(client.session, "check_login", lambda: calls["n"] > 1)

    assert client.load_session() is False
    assert client.session_load_deferred() is True

    client._retry_deferred_load()

    assert calls["n"] == 2
    assert client.session_load_deferred() is False


def test_recovery_fires_the_session_restored_hook(
    client, session_file, monkeypatch
):
    """The boot thread skips re-enqueueing pending downloads when the
    session looks signed out, and nothing else re-runs it. Without this
    hook a user who launched offline loses their pending downloads for
    the rest of the process even after the session comes back."""
    calls = {"n": 0, "restored": 0}
    client.on_session_restored = lambda: calls.__setitem__(
        "restored", calls["restored"] + 1
    )

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise req_exc.ConnectionError("dns down")
        return True

    monkeypatch.setattr(client.session, "load_oauth_session", flaky)
    monkeypatch.setattr(client.session, "check_login", lambda: calls["n"] > 1)

    assert client.load_session() is False
    assert calls["restored"] == 0

    client._retry_deferred_load()

    assert calls["restored"] == 1


def test_failed_retry_does_not_fire_the_restored_hook(
    client, session_file, monkeypatch
):
    calls = {"restored": 0}
    client.on_session_restored = lambda: calls.__setitem__(
        "restored", calls["restored"] + 1
    )

    def unreachable(*a, **kw):
        raise req_exc.ConnectionError("dns down")

    monkeypatch.setattr(client.session, "load_oauth_session", unreachable)

    assert client.load_session() is False
    client._retry_deferred_load()

    assert calls["restored"] == 0
    assert client.session_load_deferred() is True


def test_logout_clears_the_flag(client, session_file, monkeypatch):
    def unreachable(*a, **kw):
        raise req_exc.ConnectionError("dns down")

    monkeypatch.setattr(client.session, "load_oauth_session", unreachable)
    assert client.load_session() is False
    assert client.session_load_deferred() is True

    client.logout()

    assert client.session_load_deferred() is False
