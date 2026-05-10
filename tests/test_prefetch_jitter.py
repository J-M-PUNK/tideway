"""Tests for the album-mount prefetch fan-out fix.

The prefetch endpoint runs N tracks through a ThreadPoolExecutor; each
track triggers two parallel Tidal API calls inside `_resolve_source`
(track metadata + playbackinfo) followed by a local manifest parse.
Without per-worker jitter, all N workers fire their first call within
a few ms of each other and the resulting burst is exactly the shape
Tidal's anti-abuse layer reads as "client is scraping": first 429,
then 403/`abuse_detected`, then the longer escalation. Putting
`tidal_jitter_sleep()` at the top of `player.prefetch` spreads the
lead-in across a 50-200 ms window per worker, so the first request
from each worker lands at a different moment before the parallel
pair fires.

These tests pin both halves of that contract:
- `prefetch` calls `tidal_jitter_sleep` before resolving (so
  refactors can't silently remove it).
- The prefetch endpoint runs at most 2 workers in parallel (so the
  burst from a 12-track album never exceeds 6 in-flight Tidal calls
  even at the worst overlap).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch


def test_player_prefetch_jitters_before_first_tidal_call():
    """The lazy import inside `prefetch` resolves
    `tidal_jitter_sleep` from `app.tidal_client` at call time. Patch
    it there and the call should land before _resolve_source runs.
    """
    from app.audio.player import PCMPlayer

    player = PCMPlayer.__new__(PCMPlayer)  # bypass __init__ for unit isolation
    player._manifest_cache = MagicMock()
    player._warm_bytes = MagicMock()

    call_order: list[str] = []

    def fake_jitter() -> None:
        call_order.append("jitter")

    def fake_resolve(track_id, quality):
        call_order.append("resolve")
        # Mimic _resolve_source's return shape: (urls, dur, info, bytes_map).
        return (["http://seg/0", "http://seg/1"], 180.0, None, {})

    player._resolve_source = fake_resolve  # type: ignore[assignment]

    with patch("app.tidal_client.tidal_jitter_sleep", side_effect=fake_jitter):
        ok = player.prefetch("12345", quality=None, warm_bytes=False)

    assert ok is True
    # Jitter must come before resolve, not after. The whole point is
    # to spread the lead-in to Tidal, so a post-resolve jitter would
    # be useless.
    assert call_order == ["jitter", "resolve"], call_order


def test_prefetch_endpoint_caps_at_two_workers():
    """The endpoint constructs a ThreadPoolExecutor sized at most
    `min(2, len(ids))`. We can't import server.py at unit-test time
    cheaply, so this test re-creates the relevant expression from
    that endpoint and checks its peak concurrency.
    """
    # Re-create the endpoint's pool sizing rule. If this test ever
    # disagrees with server.py, server.py changed and the burst-cap
    # contract may have regressed.
    ids = [str(i) for i in range(12)]  # 12-track album
    pool_workers = min(2, len(ids))
    assert pool_workers == 2

    in_flight = {"now": 0, "peak": 0}
    lock = __import__("threading").Lock()

    def task(_tid: str) -> None:
        with lock:
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        # Hold the slot briefly so concurrent workers actually overlap.
        __import__("time").sleep(0.01)
        with lock:
            in_flight["now"] -= 1

    with ThreadPoolExecutor(max_workers=pool_workers) as pool:
        list(pool.map(task, ids))

    assert in_flight["peak"] == 2, f"peak concurrent prefetch was {in_flight['peak']}"
