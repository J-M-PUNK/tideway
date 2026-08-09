"""Tests for recommendation impression/outcome logging (#307).

The join between "what we recommended" (Tidal album + artist text) and
"what got played" (Last.fm scrobble text) is the fragile part — the two
services spell editions differently — so the normalizer gets most of the
attention here.
"""
import json

import pytest

from app import rec_analytics


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    """Point the module at a scratch file so tests never touch real state."""
    monkeypatch.setattr(rec_analytics, "_file", lambda: tmp_path / "imp.json")
    yield


def _section(key, albums):
    return {"key": key, "title": key, "subtitle": "", "albums": albums}


def _album(aid, name, artist):
    return {"id": aid, "name": name, "artists": [{"name": artist}]}


def test_records_impressions_per_section():
    rec_analytics.record_impressions([
        _section("fans_also_like", [_album("1", "Frailty", "Jane Remover")]),
        _section("made_for_you", [_album("2", "Geogaddi", "Boards of Canada")]),
    ])
    out = rec_analytics.stats()
    by = {s["section"]: s for s in out["sections"]}
    assert by["fans_also_like"]["albums"] == 1
    assert by["made_for_you"]["albums"] == 1
    assert out["tracked_albums"] == 2


def test_impressions_accumulate_across_serves():
    sec = [_section("fans_also_like", [_album("1", "Frailty", "Jane Remover")])]
    rec_analytics.record_impressions(sec)
    rec_analytics.record_impressions(sec)
    by = {s["section"]: s for s in rec_analytics.stats()["sections"]}
    # Two serves of the same album: two impressions, still one album.
    assert by["fans_also_like"]["impressions"] == 2
    assert by["fans_also_like"]["albums"] == 1


def test_play_rate_joins_on_normalized_names():
    rec_analytics.record_impressions([
        _section("row_a", [_album("1", "Frailty", "Jane Remover")]),
        _section("row_b", [_album("2", "Never Played", "Nobody")]),
    ])
    played = rec_analytics.played_keys_from_scrobbles(
        [{"artist": "Jane Remover", "album": "Frailty", "track": "Movies For Guys"}]
    )
    by = {s["section"]: s for s in rec_analytics.stats(played_keys=played)["sections"]}
    assert by["row_a"]["played"] == 1 and by["row_a"]["play_rate"] == 1.0
    assert by["row_b"]["played"] == 0 and by["row_b"]["play_rate"] == 0.0


@pytest.mark.parametrize("recommended,scrobbled", [
    ("Frailty (Deluxe Edition)", "Frailty"),
    ("Geogaddi", "Geogaddi (Remastered)"),
    ("Music Has The Right To Children", "Music Has the Right to Children"),
    ("Vol. 2", "Vol 2"),
    ("In Rainbows (2007 Remaster)", "In Rainbows"),
    ("Selected Ambient Works (Expanded) (Remastered)", "Selected Ambient Works"),
])
def test_edition_and_punctuation_differences_still_match(recommended, scrobbled):
    """Tidal and Last.fm disagree on edition suffixes and punctuation
    constantly. Missing these would silently report a played album as
    never played and drag every row's rate down."""
    rec_analytics.record_impressions([
        _section("row", [_album("1", recommended, "Artist")])
    ])
    played = rec_analytics.played_keys_from_scrobbles(
        [{"artist": "Artist", "album": scrobbled}]
    )
    by = {s["section"]: s for s in rec_analytics.stats(played_keys=played)["sections"]}
    assert by["row"]["played"] == 1, f"{recommended!r} should match {scrobbled!r}"


def test_different_albums_do_not_collide():
    """The normalizer must not be so aggressive that distinct records
    merge — a false match would inflate the play rate."""
    rec_analytics.record_impressions([
        _section("row", [
            _album("1", "Vol. 1", "Artist"),
            _album("2", "Vol. 2", "Artist"),
        ])
    ])
    played = rec_analytics.played_keys_from_scrobbles(
        [{"artist": "Artist", "album": "Vol. 1"}]
    )
    by = {s["section"]: s for s in rec_analytics.stats(played_keys=played)["sections"]}
    assert by["row"]["played"] == 1


def test_liked_is_exact_by_id():
    rec_analytics.record_impressions([
        _section("row", [
            _album("1", "A", "X"), _album("2", "B", "Y"), _album("3", "C", "Z"),
        ])
    ])
    out = rec_analytics.stats(liked_ids={"2"})
    assert out["sections"][0]["liked"] == 1


def test_sections_ranked_by_play_rate():
    rec_analytics.record_impressions([
        _section("good", [_album("1", "Hit", "A")]),
        _section("bad", [_album("2", "Miss", "B")]),
    ])
    played = rec_analytics.played_keys_from_scrobbles([{"artist": "A", "album": "Hit"}])
    out = rec_analytics.stats(played_keys=played)
    assert out["sections"][0]["section"] == "good"


def test_store_is_bounded(monkeypatch):
    monkeypatch.setattr(rec_analytics, "_MAX_ALBUMS", 5)
    for i in range(12):
        rec_analytics.record_impressions([
            _section("row", [_album(str(i), f"Album {i}", "A")])
        ])
    assert rec_analytics.stats()["tracked_albums"] == 5


def test_corrupt_store_does_not_raise(tmp_path, monkeypatch):
    path = tmp_path / "imp.json"
    path.write_text("{not json")
    monkeypatch.setattr(rec_analytics, "_file", lambda: path)
    # Recording recovers rather than propagating a parse error into the
    # recommendations endpoint.
    rec_analytics.record_impressions([_section("row", [_album("1", "A", "X")])])
    assert rec_analytics.stats()["tracked_albums"] == 1
    assert json.loads(path.read_text())["albums"]


def test_albums_without_ids_are_skipped():
    rec_analytics.record_impressions([
        _section("row", [{"name": "No Id", "artists": [{"name": "X"}]}])
    ])
    assert rec_analytics.stats()["tracked_albums"] == 0


def test_pruning_does_not_evict_the_albums_that_worked(monkeypatch):
    """Retention must not correlate with the outcome being measured.

    Acting on a recommendation (saving it) excludes the album from every
    row afterwards, so it stops being served and its serving-recency goes
    stale — while an album the listener ignored stays in rotation and
    keeps looking fresh. Evicting by serving-recency therefore drops the
    successes and keeps the failures, sagging every measured rate over
    time for no real reason. Eviction has to key on first sighting, which
    no outcome can influence.
    """
    monkeypatch.setattr(rec_analytics, "_MAX_ALBUMS", 3)
    clock = [100]
    monkeypatch.setattr(rec_analytics.time, "time", lambda: clock[0])

    ignored = [_album(f"ignored{i}", f"Ignored {i}", "B") for i in range(3)]
    rec_analytics.record_impressions([_section("row", ignored)])

    # Seen later than the ignored ones, then acted on — never served again.
    clock[0] = 200
    rec_analytics.record_impressions([
        _section("row", [_album("acted-on", "Acted On", "A")])
    ])

    # The ignored albums keep getting re-served, refreshing their recency.
    clock[0] = 500
    rec_analytics.record_impressions([_section("row", ignored)])

    # One more sighting tips the store past the cap and triggers eviction.
    clock[0] = 600
    rec_analytics.record_impressions([
        _section("row", [_album("newest", "Newest", "C")])
    ])

    with rec_analytics._lock:
        remaining = set(rec_analytics._load()["albums"])
    # Keying on serving-recency would evict "acted-on" first: it has the
    # stalest last_seen precisely because the listener acted on it.
    assert "acted-on" in remaining, "the album that worked was evicted"
    assert "newest" in remaining
    assert len(remaining) == 3
