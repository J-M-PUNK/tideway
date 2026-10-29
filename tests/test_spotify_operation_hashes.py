"""spotapi's GraphQL operation-hash lookup runs once per operation.

`BaseClient.part_hash` splits the concatenated Spotify web-player JS
bundles (7-10 million characters) on the operation name for every
request, holding the GIL for 6-13 ms each time. `_cache_operation_hashes`
wraps it so each operation is looked up once per `raw_hashes` string.
"""
from __future__ import annotations

import threading
import time

from app import spotify_public


class _FakeBase:
    def __init__(self, raw: str):
        self.raw_hashes = raw
        self.lookups: list[str] = []

    def part_hash(self, name: str) -> str:
        self.lookups.append(name)
        return f"{name}@{len(self.raw_hashes)}"


def test_each_operation_is_looked_up_once():
    base = _FakeBase("x" * 10)
    spotify_public._cache_operation_hashes(base)

    for _ in range(5):
        assert base.part_hash("getTrack") == "getTrack@10"
    assert base.part_hash("searchDesktop") == "searchDesktop@10"
    assert base.part_hash("getTrack") == "getTrack@10"

    assert base.lookups == ["getTrack", "searchDesktop"]


def test_a_refetched_bundle_is_looked_up_again():
    # If spotapi ever replaces raw_hashes (a new web-player build), the
    # cached hash belongs to the old bundle and must not be reused.
    base = _FakeBase("x" * 10)
    spotify_public._cache_operation_hashes(base)
    assert base.part_hash("getTrack") == "getTrack@10"

    base.raw_hashes = "y" * 20
    assert base.part_hash("getTrack") == "getTrack@20"
    assert base.lookups == ["getTrack", "getTrack"]


def test_the_first_lookup_caches_against_the_fetched_bundle():
    # The first part_hash call is what fetches the bundle (raw_hashes is
    # undefined until then); the cache must key on the fetched string,
    # not the placeholder that was there before the call.
    class _LazyBase(_FakeBase):
        def part_hash(self, name: str) -> str:
            self.raw_hashes = "z" * 30
            return super().part_hash(name)

    base = _LazyBase("")
    spotify_public._cache_operation_hashes(base)
    assert base.part_hash("getTrack") == "getTrack@30"
    assert base.part_hash("getTrack") == "getTrack@30"
    assert base.lookups == ["getTrack"]


def test_both_clients_are_wrapped(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(spotify_public, "_artist_mod", None)
    monkeypatch.setattr(spotify_public, "_song_mod", None)
    monkeypatch.setattr(
        spotify_public, "_cache_operation_hashes", lambda base: calls.append(base)
    )
    song, artist = spotify_public._ensure_client()
    assert calls == [song.base, artist.base]


def test_concurrent_first_lookups_wait_for_the_whole_bundle():
    # spotapi's first part_hash call downloads the bundles and builds
    # raw_hashes in place, main bundle first and then one chunk at a
    # time. Five threads making their first lookup together must not
    # each download the set, and none may read it half-built.
    class _SpotapiLikeBase:
        def __init__(self):
            self.raw_hashes = None
            self.downloads = 0

        def part_hash(self, name: str) -> str:
            if self.raw_hashes is None:
                self.downloads += 1
                self.raw_hashes = "main;"
                for chunk in ("a;", "b;", f"{name}=hash;"):
                    time.sleep(0.02)
                    self.raw_hashes += chunk
            marker = f"{name}="
            # Same failure as spotapi: IndexError when the operation
            # isn't in what has been downloaded so far.
            return self.raw_hashes.split(marker)[1].split(";")[0]

    base = _SpotapiLikeBase()
    spotify_public._cache_operation_hashes(base)
    start = threading.Barrier(5)
    results: list[object] = []

    def lookup():
        start.wait()
        try:
            results.append(base.part_hash("searchDesktop"))
        except IndexError as exc:
            results.append(exc)

    threads = [threading.Thread(target=lookup) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == ["hash"] * 5
    assert base.downloads == 1
