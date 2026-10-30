"""Last.fm calls share one pooled session.

A bare `requests.get` opens a new connection per call, and the TLS
handshake to ws.audioscrobbler.com is most of a call's cost (130-420 ms
fresh, ~65 ms kept alive). An artist page's playcount batch makes one
call per track, so each one paid the handshake again.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import requests

from app import lastfm
from app.lastfm import LastFmClient


class _Resp:
    ok = True

    def json(self):
        return {"track": {"playcount": "1234", "listeners": "56", "userloved": "0"}}


def _client() -> LastFmClient:
    client = LastFmClient.__new__(LastFmClient)
    client._lock = threading.RLock()
    client.creds = SimpleNamespace(username="someone")
    client._effective_api_key = lambda: "key"
    return client


def test_track_playcount_goes_through_the_pooled_session(monkeypatch):
    def fresh_connection(*a, **kw):
        raise AssertionError("opened a fresh connection instead of reusing one")

    monkeypatch.setattr(requests, "get", fresh_connection)
    calls = []

    def pooled_get(url, params=None, timeout=None):
        calls.append(params["method"])
        return _Resp()

    monkeypatch.setattr(lastfm._HTTP, "get", pooled_get)

    out = _client().get_track_playcount("Fleetwood Mac", "Dreams")

    assert calls == ["track.getInfo"]
    assert out["playcount"] == 1234
