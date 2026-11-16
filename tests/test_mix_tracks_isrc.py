"""Mix tracks carry their ISRCs, so mix pages can show playcounts.

tidalapi builds a Mix from Tidal's `pages/mix`, whose track entries
leave out `isrc`: every mix track came back with none, so the mix page
had nothing to look Spotify playcounts up by and showed no play counts
at all. Tidal's `mixes/{id}/items` returns the same tracks with full
metadata, ISRC included, so mix_detail reads its tracks from there.
"""
from __future__ import annotations

import pytest

import server


def _raw_track(tid: int, isrc: str) -> dict:
    """A track as Tidal's mixes/{id}/items returns it: the same field
    set as a v1 catalog track (taken from a public one), neutral values."""
    artist = {"id": 1, "name": "Artist", "type": "MAIN", "picture": None}
    return {
        "id": tid,
        "title": f"Track {tid}",
        "version": None,
        "duration": 200,
        "isrc": isrc,
        "trackNumber": 1,
        "volumeNumber": 1,
        "explicit": False,
        "editable": False,
        "upload": False,
        "ai": False,
        "accessType": "PUBLIC",
        "allowStreaming": True,
        "streamReady": True,
        "adSupportedStreamReady": True,
        "djReady": True,
        "stemReady": False,
        "payToStream": False,
        "premiumStreamingOnly": False,
        "spotlighted": False,
        "streamStartDate": "2001-01-01T00:00:00.000+0000",
        "audioQuality": "LOSSLESS",
        "audioModes": ["STEREO"],
        "mediaMetadata": {"tags": ["LOSSLESS"]},
        "popularity": 50,
        "peak": 1.0,
        "replayGain": -8.0,
        "bpm": None,
        "key": None,
        "keyScale": None,
        "copyright": None,
        "url": f"http://www.tidal.com/track/{tid}",
        "mixes": {},
        "artist": artist,
        "artists": [artist],
        "album": {
            "id": 10,
            "title": "Album",
            "cover": None,
            "videoCover": None,
            "vibrantColor": None,
        },
    }


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _StubMix:
    title = "My Mix 1"
    sub_title = "Artist and more"

    def __init__(self, page_tracks=()):
        self._page_tracks = list(page_tracks)

    def items(self):
        return self._page_tracks

    def image(self, _size):
        return None


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    server._invalidate_detail_cache()
    monkeypatch.setattr(server, "_require_auth", lambda: None)
    yield
    server._invalidate_detail_cache()


def test_tracks_come_from_the_items_endpoint_with_isrcs(monkeypatch):
    calls = []

    def request(method, path, params=None, **_):
        calls.append((path, params))
        return _Resp({
            "items": [
                {"type": "track", "item": _raw_track(1, "USAAA0000001")},
                {"type": "video", "item": {"id": 99}},
                {"type": "track", "item": _raw_track(2, "USAAA0000002")},
            ],
            "totalNumberOfItems": 3,
        })

    monkeypatch.setattr(server.tidal.session, "mix", lambda _id: _StubMix())
    monkeypatch.setattr(server.tidal.session.request, "request", request)

    out = server.mix_detail("abc")

    assert [t["isrc"] for t in out["tracks"]] == ["USAAA0000001", "USAAA0000002"]
    assert out["name"] == "My Mix 1"
    assert calls[0][0] == "mixes/abc/items"


def test_a_long_mix_is_read_across_pages_in_order(monkeypatch):
    pages = {
        0: [{"type": "track", "item": _raw_track(i, f"USAAA{i:07d}")} for i in range(1, 101)],
        100: [{"type": "track", "item": _raw_track(i, f"USAAA{i:07d}")} for i in range(101, 121)],
    }

    def request(method, path, params=None, **_):
        return _Resp({"items": pages.get(params["offset"], []), "totalNumberOfItems": 120})

    monkeypatch.setattr(server.tidal.session, "mix", lambda _id: _StubMix())
    monkeypatch.setattr(server.tidal.session.request, "request", request)

    out = server.mix_detail("long")

    assert [t["id"] for t in out["tracks"]] == [str(i) for i in range(1, 121)]


def test_a_failed_items_fetch_still_loads_the_mix(monkeypatch):
    # The page's own tracks play fine; they only lack ISRCs.
    page_track = server.tidal.session.parse_track({**_raw_track(7, "IGNORED"), "isrc": None})

    def request(*_a, **_k):
        raise ConnectionError("rate limited")

    monkeypatch.setattr(server.tidal.session, "mix", lambda _id: _StubMix([page_track]))
    monkeypatch.setattr(server.tidal.session.request, "request", request)

    out = server.mix_detail("fallback")

    assert [t["id"] for t in out["tracks"]] == ["7"]
