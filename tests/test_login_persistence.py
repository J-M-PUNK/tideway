"""load_session()'s relaunch refresh must not silently log the user out
(#309).

When the app is reopened after the stored access token has expired,
load_session refreshes up front. The previous code swallowed any failure
with `except Exception: pass` and fell through to a stale-token
check_login() — so a transient blip (network not up at boot, 429, 5xx)
signed the user out exactly like a revoked token, with no log to tell
them apart. These pin the corrected behavior:

  - transient failure  -> keep the session, defer for retry (return False
    but `_session_load_deferred` True, without touching the stale token)
  - genuine rejection  -> fall through to check_login() (real re-login)
  - success            -> refreshed and validated
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
import tidalapi

from app import tidal_client
from app.tidal_client import TransientRefreshError


@pytest.fixture
def expired_session(tmp_path, monkeypatch):
    """Point SESSION_FILE at a file whose access token expired 2h ago.

    Naive UTC, because that is the frame the app actually writes
    (tidalapi's `datetime.utcnow()`). This used to use `datetime.now()`,
    which agreed with the reader's own wrong frame and so hid #309 —
    off-UTC machines never took the branch these tests exercise.
    """
    f = tmp_path / "tidal_session.json"
    expiry = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    ).isoformat()
    f.write_text(json.dumps({
        "token_type": "Bearer",
        "access_token": "stale",
        "refresh_token": "R1",
        "expiry_time": expiry,
        "is_pkce": False,
    }))
    monkeypatch.setattr(tidal_client, "SESSION_FILE", f)
    return f


@pytest.fixture
def tidal(monkeypatch):
    import server

    t = server.tidal
    monkeypatch.setattr(t, "_session_load_deferred", False)
    # Neutralize disk + network side effects of the happy path.
    monkeypatch.setattr(t, "save_session", lambda: None)
    return t


def _track_oauth(monkeypatch, tidal, check_result):
    calls = {"load_oauth": 0}
    monkeypatch.setattr(
        tidal.session, "load_oauth_session",
        lambda *a, **k: calls.__setitem__("load_oauth", calls["load_oauth"] + 1),
    )
    monkeypatch.setattr(tidal.session, "check_login", lambda: check_result)
    return calls


def test_transient_refresh_defers_and_keeps_session(expired_session, tidal, monkeypatch):
    def _boom(_rt):
        raise TransientRefreshError("HTTP 429")

    monkeypatch.setattr(tidal, "_token_refresh_capturing", _boom)
    calls = _track_oauth(monkeypatch, tidal, check_result=False)

    result = tidal.load_session()

    assert result is False
    assert tidal._session_load_deferred is True  # kept for retry, not logged out
    assert calls["load_oauth"] == 0  # returned before touching the stale token


def test_auth_rejection_falls_through_to_relogin(expired_session, tidal, monkeypatch):
    def _reject(_rt):
        raise tidalapi.exceptions.AuthenticationError("invalid_grant")

    monkeypatch.setattr(tidal, "_token_refresh_capturing", _reject)
    calls = _track_oauth(monkeypatch, tidal, check_result=False)

    result = tidal.load_session()

    assert result is False
    assert tidal._session_load_deferred is False  # genuine re-login, not deferred
    assert calls["load_oauth"] == 1  # fell through to validate (which fails)


def test_successful_launch_refresh_signs_in(expired_session, tidal, monkeypatch):
    monkeypatch.setattr(tidal, "_token_refresh_capturing", lambda _rt: True)
    calls = _track_oauth(monkeypatch, tidal, check_result=True)

    result = tidal.load_session()

    assert result is True
    assert calls["load_oauth"] == 1
