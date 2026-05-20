"""Tests for `get_user_info` returning None on failure.

Background: Last.fm's `user.getInfo` endpoint 404s when the
configured username doesn't resolve (e.g. the user renamed
their Last.fm account). The old code path returned `{}` in
that case, which the frontend treated as a present-but-empty
user object and crashed when rendering counts. The fix is to
return None — these tests pin that contract so a refactor
can't drop us back into the silent-empty-object behavior.
"""
from __future__ import annotations

from unittest.mock import patch

from app.lastfm import LastFmClient


def test_get_user_info_returns_none_on_no_data():
    """`_public_get` returns None when the API call fails — get_user_info
    must surface that as None, not paper over it with `{}`."""
    client = LastFmClient.__new__(LastFmClient)
    with patch.object(client, "_public_get", return_value=None):
        assert client.get_user_info() is None


def test_get_user_info_returns_none_when_no_user_in_payload():
    """Last.fm 404s for unknown users with a JSON body that has no
    `user` key. `_public_get` will return None for the HTTP 404, but
    a future change could route through and return the JSON anyway —
    pin the safety check that we ignore a payload without a username."""
    client = LastFmClient.__new__(LastFmClient)
    with patch.object(client, "_public_get", return_value={"foo": "bar"}):
        assert client.get_user_info() is None


def test_get_user_info_returns_none_when_user_has_no_name():
    """A `user` key with an empty `name` field is the same shape Last.fm
    returns for malformed/partial responses — also treat as None so
    the UI never has to render a header without a real username."""
    client = LastFmClient.__new__(LastFmClient)
    with patch.object(client, "_public_get", return_value={"user": {"name": ""}}):
        assert client.get_user_info() is None


def test_get_user_info_passes_through_good_payload():
    """Sanity check: a complete payload should still resolve into the
    expected dict shape with all counts defaulting to 0."""
    client = LastFmClient.__new__(LastFmClient)
    payload = {
        "user": {
            "name": "someuser",
            "realname": "Some User",
            "playcount": "12345",
            "track_count": "200",
            "artist_count": "50",
            "album_count": "30",
            "country": "US",
            "url": "https://www.last.fm/user/someuser",
            "registered": {"unixtime": "1500000000"},
            "image": [],
        }
    }
    with patch.object(client, "_public_get", return_value=payload):
        out = client.get_user_info()
    assert out is not None
    assert out["username"] == "someuser"
    assert out["playcount"] == 12345
    assert out["track_count"] == 200
    assert out["artist_count"] == 50
    assert out["album_count"] == 30
    assert out["registered_at"] == 1500000000
