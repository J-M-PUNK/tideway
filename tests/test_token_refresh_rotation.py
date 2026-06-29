"""Regression tests for refresh-token rotation handling.

tidalapi 0.8.11's Session.token_refresh() updates the access token
but discards any rotated refresh_token in Tidal's response. Tidal
does rotate refresh tokens and invalidates the previous one, so a
client that keeps re-persisting the original token gets logged out a
few days later. TidalClient._token_refresh_capturing mirrors
tidalapi's refresh but carries the rotated token back onto the
session; these tests pin that contract.
"""
from __future__ import annotations

import threading
from datetime import datetime
from unittest.mock import MagicMock

import pytest
import tidalapi

from app.tidal_client import TidalClient


class _Resp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict:
        return self._body


class _ReqSession:
    def __init__(self, resp: _Resp):
        self._resp = resp
        self.calls: list = []

    def post(self, url, params):
        self.calls.append((url, params))
        return self._resp


class _Config:
    api_oauth2_token = "https://auth.tidal.com/v1/oauth2/token"
    client_id = "device_id"
    client_secret = "device_secret"
    client_id_pkce = "pkce_id"
    client_secret_pkce = "pkce_secret"


class _Session:
    def __init__(self, resp: _Resp, *, is_pkce: bool = False):
        self.config = _Config()
        self.request_session = _ReqSession(resp)
        self.is_pkce = is_pkce
        self.access_token = "old_access"
        self.refresh_token = "old_refresh"
        self.token_type = "Bearer"
        self.expiry_time = None


class _BlockingReqSession(_ReqSession):
    """A request session whose POST blocks until released, so a test
    can pin a second thread *inside* the contended window. Records
    every POST so the test can assert how many actually fired."""

    def __init__(self, resp: _Resp):
        super().__init__(resp)
        self.entered = threading.Event()
        self.release = threading.Event()

    def post(self, url, params):
        self.entered.set()
        self.release.wait(timeout=5)
        return super().post(url, params)


def _client(session: _Session) -> TidalClient:
    c = TidalClient.__new__(TidalClient)
    c.session = session
    # __new__ bypasses __init__, so the lock the refresh paths rely on
    # isn't created; supply it. Harmless to the direct-call tests.
    c._refresh_lock = threading.Lock()
    return c


def test_rotated_refresh_token_is_captured():
    session = _Session(
        _Resp(
            200,
            {
                "access_token": "new_access",
                "refresh_token": "rotated_refresh",
                "expires_in": 86400,
                "token_type": "Bearer",
            },
        )
    )
    client = _client(session)

    assert client._token_refresh_capturing("old_refresh") is True
    # The whole point: the rotated token replaces the stored one so
    # the next save_session() persists something Tidal still honours.
    assert session.refresh_token == "rotated_refresh"
    assert session.access_token == "new_access"
    assert session.token_type == "Bearer"
    assert isinstance(session.expiry_time, datetime)
    # Refresh grant must go to the OAuth token endpoint with the
    # device-code client when not PKCE.
    url, params = session.request_session.calls[0]
    assert url == _Config.api_oauth2_token
    assert params["grant_type"] == "refresh_token"
    assert params["client_id"] == "device_id"


def test_absent_refresh_token_keeps_existing():
    # Tidal only returns refresh_token when it rotates; when absent
    # the stored one is still valid and must be preserved.
    session = _Session(
        _Resp(
            200,
            {
                "access_token": "new_access",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )
    )
    client = _client(session)

    assert client._token_refresh_capturing("old_refresh") is True
    assert session.refresh_token == "old_refresh"
    assert session.access_token == "new_access"


def test_pkce_session_uses_pkce_client():
    session = _Session(
        _Resp(
            200,
            {
                "access_token": "a",
                "refresh_token": "r",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        ),
        is_pkce=True,
    )
    client = _client(session)

    client._token_refresh_capturing("old_refresh")
    _url, params = session.request_session.calls[0]
    assert params["client_id"] == "pkce_id"
    assert params["client_secret"] == "pkce_secret"


def test_non_200_raises_authentication_error():
    session = _Session(
        _Resp(
            401,
            {"error": "invalid_grant", "error_description": "expired"},
        )
    )
    client = _client(session)

    with pytest.raises(tidalapi.exceptions.AuthenticationError):
        client._token_refresh_capturing("dead_refresh")


def test_400_invalid_grant_is_permanent():
    # A dead/revoked refresh token comes back as 400 invalid_grant.
    # That's the genuine re-login case and must raise AuthenticationError.
    session = _Session(
        _Resp(400, {"error": "invalid_grant", "error_description": "revoked"})
    )
    client = _client(session)

    with pytest.raises(tidalapi.exceptions.AuthenticationError):
        client._token_refresh_capturing("dead_refresh")


def test_429_is_transient_not_auth_error():
    # A rate-limit during a background refresh must NOT be mistaken for a
    # dead token. Treating it as permanent is what deleted valid sessions
    # and logged users out days later.
    from app.tidal_client import TransientRefreshError

    session = _Session(
        _Resp(429, {"error": "rate_limit", "error_description": "slow down"})
    )
    client = _client(session)

    with pytest.raises(TransientRefreshError):
        client._token_refresh_capturing("good_refresh")
    # The refresh token is untouched — it's still valid.
    assert session.refresh_token == "old_refresh"


def test_5xx_is_transient():
    from app.tidal_client import TransientRefreshError

    session = _Session(_Resp(503, {"error": "server_error"}))
    client = _client(session)

    with pytest.raises(TransientRefreshError):
        client._token_refresh_capturing("good_refresh")
    assert session.refresh_token == "old_refresh"


def test_non_json_error_body_is_transient():
    # A 5xx that returns an HTML error page (resp.json() raises) has no
    # OAuth error code to read — treat as transient, not a logout.
    from app.tidal_client import TransientRefreshError

    class _HtmlResp(_Resp):
        def json(self):
            raise ValueError("not json")

    session = _Session(_HtmlResp(502, {}))
    client = _client(session)

    with pytest.raises(TransientRefreshError):
        client._token_refresh_capturing("good_refresh")
    assert session.refresh_token == "old_refresh"


def test_refresh_once_swallows_transient_to_false():
    # _refresh_once is the chokepoint force_refresh relies on. A transient
    # failure must come back as False (give up, keep session) rather than
    # propagate — force_refresh logs the user out on a raised
    # AuthenticationError, and a 429 must never reach that branch.
    session = _Session(_Resp(429, {"error": "rate_limit"}))
    client = _client(session)
    client.save_session = MagicMock()

    assert client._refresh_once(based_on="old_refresh") is False
    assert session.refresh_token == "old_refresh"
    client.save_session.assert_not_called()


def test_concurrent_internal_refresh_is_single_flight():
    """Two parallel 401s through tidalapi's internal refresh path
    (_token_refresh_and_persist) must collapse to ONE network refresh.

    This is the regression that logs users out every few days: that
    path used to take no lock, so both threads POSTed the same refresh
    token. Tidal rotates on the first POST and invalidates the old
    token; the second POST (or its persisted result) then carries a
    dead-lineage token, and the next refresh fails. The blocking POST
    pins the first thread inside the contended window so a lockless
    regression deterministically fires a second POST.
    """
    body = {
        "access_token": "new_access",
        "refresh_token": "rotated_refresh",
        "expires_in": 86400,
        "token_type": "Bearer",
    }
    session = _Session(_Resp(200, body))
    blocking = _BlockingReqSession(_Resp(200, body))
    session.request_session = blocking
    client = _client(session)
    client.save_session = MagicMock()

    results: list[bool] = []

    def worker():
        results.append(client._token_refresh_and_persist("old_refresh"))

    t1 = threading.Thread(target=worker, name="refresh-1")
    t2 = threading.Thread(target=worker, name="refresh-2")
    t1.start()
    # t1 is now inside post(), holding _refresh_lock. Start the
    # contender; it blocks on the lock (fixed) or races into post()
    # (regressed). Either way, releasing lets the truth show.
    assert blocking.entered.wait(timeout=5), "first refresh never reached the network"
    t2.start()
    blocking.release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    # Exactly one network refresh; the contender reused the rotated
    # result rather than re-POSTing the invalidated token.
    assert len(blocking.calls) == 1
    assert results == [True, True]
    assert session.refresh_token == "rotated_refresh"
    client.save_session.assert_called_once()
