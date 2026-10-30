"""Concurrent resolves of the same track share one trip to Tidal.

A hover warm-up, the next-track preload and the click that follows
either of them all resolve the same (track, quality). Each used to send
its own pair of Tidal requests, so a click landing while the warm-up was
still on the network paid a full resolve of its own instead of the
remainder of the one already running.
"""
from __future__ import annotations

import threading
import time

import pytest

from app.audio.manifest_cache import ManifestCache
from app.audio.player import PCMPlayer, StreamInfo

URLS = ["init", "seg1", "seg2"]


@pytest.fixture
def player():
    p = PCMPlayer(lambda: None)
    p.release = threading.Event()  # type: ignore[attr-defined]
    p.calls = []  # type: ignore[attr-defined]
    p.fail_first = False  # type: ignore[attr-defined]

    def resolve_uncached(session, track_id, quality, cache_key, t0, *, set_current_meta=True):
        # Stand-in for the network resolve: slow, then fills the cache
        # the way the real one does.
        p.calls.append((track_id, quality, threading.current_thread().name))
        assert p.release.wait(5.0)
        if p.fail_first and len(p.calls) == 1:
            raise RuntimeError("Tidal did not respond")
        meta = {"title": f"song {track_id}"}
        info = StreamInfo(source="stream")
        p._manifest_cache.store(cache_key, list(URLS), 180.0, info, meta)
        if set_current_meta:
            p._current_track_meta = meta
        return (list(URLS), 180.0, info, {}, meta)

    p._resolve_uncached = resolve_uncached  # type: ignore[method-assign]
    return p


def _in_thread(fn, name):
    out: dict = {}

    def run():
        try:
            out["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - surfaced to the test
            out["error"] = exc

    t = threading.Thread(target=run, name=name, daemon=True)
    t.start()
    return t, out


def _wait_for(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.005)
    return pred()


def test_a_click_joins_the_warm_up_already_resolving(player):
    warm, warm_out = _in_thread(
        lambda: player._resolve_source("t1", "HI_RES", set_current_meta=False),
        "prefetch",
    )
    assert _wait_for(lambda: len(player.calls) == 1)
    click, click_out = _in_thread(
        lambda: player._resolve_source("t1", "HI_RES"), "click"
    )
    time.sleep(0.05)
    player.release.set()
    warm.join(3.0)
    click.join(3.0)

    assert [c[2] for c in player.calls] == ["prefetch"]  # one trip to Tidal
    assert click_out["value"][0] == URLS
    # The warm-up doesn't touch now-playing metadata; the click does,
    # from the answer it joined.
    assert player._current_track_meta == {"title": "song t1"}


def test_the_waiter_resolves_itself_when_the_owner_fails(player):
    player.fail_first = True
    first, first_out = _in_thread(lambda: player._resolve_source("t1", None), "a")
    assert _wait_for(lambda: len(player.calls) == 1)
    second, second_out = _in_thread(lambda: player._resolve_source("t1", None), "b")
    time.sleep(0.05)
    player.release.set()
    first.join(3.0)
    second.join(3.0)

    assert isinstance(first_out.get("error"), RuntimeError)
    assert second_out["value"][0] == URLS
    assert [c[2] for c in player.calls] == ["a", "b"]


def test_a_different_quality_is_resolved_separately(player):
    a, _ = _in_thread(lambda: player._resolve_source("t1", "LOSSLESS"), "a")
    b, _ = _in_thread(lambda: player._resolve_source("t1", "HI_RES"), "b")
    assert _wait_for(lambda: len(player.calls) == 2)
    player.release.set()
    a.join(3.0)
    b.join(3.0)


def test_the_claim_is_released_when_the_resolve_raises(player):
    player.fail_first = True
    player.release.set()
    with pytest.raises(RuntimeError):
        player._resolve_source("t1", None)
    assert player._manifest_cache.begin_fill(("t1", None)) is None


def test_begin_fill_hands_out_one_claim_per_key():
    cache = ManifestCache()
    assert cache.begin_fill(("t1", None)) is None
    pending = cache.begin_fill(("t1", None))
    assert isinstance(pending, threading.Event) and not pending.is_set()
    assert cache.begin_fill(("t2", None)) is None
    cache.end_fill(("t1", None))
    assert pending.is_set()
    assert cache.begin_fill(("t1", None)) is None
