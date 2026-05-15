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

from datetime import datetime

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


def _client(session: _Session) -> TidalClient:
    c = TidalClient.__new__(TidalClient)
    c.session = session
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
