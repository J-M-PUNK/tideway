"""The Last.fm playcount batch fetches its tracks concurrently.

It used to loop one track at a time, so a ten-track artist page took
~4 s and held one of the browser's four API slots for all of it.
"""
from __future__ import annotations

import threading

import pytest

import server


def _batch(*pairs):
    return server._LastFmTrackPlaycountBatchRequest(
        items=[{"artist": a, "track": t} for a, t in pairs]
    )


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.setattr(server, "_require_auth", lambda: None)


def test_tracks_are_fetched_concurrently(monkeypatch):
    # Four fetches have to be in flight together to get past the
    # barrier; a one-at-a-time loop times it out.
    barrier = threading.Barrier(4, timeout=5)

    def get_track_playcount(artist, track):
        barrier.wait()
        return {"playcount": len(track)}

    monkeypatch.setattr(server.lastfm, "get_track_playcount", get_track_playcount)

    out = server.lastfm_track_playcounts(
        _batch(("A", "one"), ("A", "three"), ("B", "four!"), ("B", "sixsix"))
    )

    assert out["results"] == {
        "a|one": {"playcount": 3},
        "a|three": {"playcount": 5},
        "b|four!": {"playcount": 5},
        "b|sixsix": {"playcount": 6},
    }


def test_duplicates_are_fetched_once_and_blanks_skipped(monkeypatch):
    calls = []

    def get_track_playcount(artist, track):
        calls.append((artist, track))
        return {"playcount": 1}

    monkeypatch.setattr(server.lastfm, "get_track_playcount", get_track_playcount)

    out = server.lastfm_track_playcounts(
        _batch(("Heart", "Barracuda"), ("heart", "BARRACUDA"), ("", "x"), ("y", ""))
    )

    assert calls == [("Heart", "Barracuda")]
    assert out["results"] == {"heart|barracuda": {"playcount": 1}}


def test_a_failing_track_does_not_sink_the_batch(monkeypatch):
    def get_track_playcount(artist, track):
        if track == "bad":
            raise RuntimeError("Last.fm hiccup")
        return {"playcount": 7}

    monkeypatch.setattr(server.lastfm, "get_track_playcount", get_track_playcount)

    out = server.lastfm_track_playcounts(_batch(("A", "bad"), ("A", "good")))

    assert out["results"] == {"a|bad": {}, "a|good": {"playcount": 7}}


def test_an_empty_batch_makes_no_calls(monkeypatch):
    monkeypatch.setattr(
        server.lastfm,
        "get_track_playcount",
        lambda *a: pytest.fail("no tracks to fetch"),
    )
    assert server.lastfm_track_playcounts(_batch()) == {"results": {}}
