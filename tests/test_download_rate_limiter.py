"""Tests for the per-track download rate limiter and the shared
aggregate limiter.

The throttle is the single biggest ban-risk reduction on the client
— it changes the CDN fetch signature from "scrape" to "aggressive
prefetch". If it regresses silently (cap not enforced), users who
trusted the setting would still get flagged by Tidal's CDN heuristics.
"""
import threading
import time

from app.downloader import _RateLimiter, _SharedRateLimiter, _apply_aggregate_rate


def test_unlimited_rate_does_not_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    limiter = _RateLimiter(0)
    for _ in range(10):
        limiter.consume(1_000_000)

    assert slept == []


def test_negative_rate_treated_as_unlimited(monkeypatch):
    """Defensive: a negative value shouldn't crash or stall."""
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    limiter = _RateLimiter(-5.0)
    limiter.consume(1_000_000)

    assert slept == []


def test_rate_limiter_paces_consumption(monkeypatch):
    """At 1 MB/s, consuming 1 MB should sleep ~1 s if called
    immediately after construction."""
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    slept = []

    def fake_sleep(secs: float) -> None:
        slept.append(secs)
        # Simulate time passing from the sleep.
        now[0] += secs

    monkeypatch.setattr(time, "sleep", fake_sleep)

    limiter = _RateLimiter(1_000_000)  # 1 MB/s

    limiter.consume(1_000_000)

    assert len(slept) == 1
    # Ideal elapsed = 1.0 s, actual elapsed = 0 s, so sleep ~ 1.0 s.
    assert 0.99 <= slept[0] <= 1.01


def test_rate_limiter_does_not_sleep_when_already_behind(monkeypatch):
    """If real time has already passed the ideal pace (network is
    slow), consume() must not sleep — don't compound latency on top
    of a slow connection."""
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    limiter = _RateLimiter(10_000_000)  # 10 MB/s

    # Jump forward 5 seconds before consuming 1 MB — ideal time is
    # 0.1 s, actual elapsed is 5 s, so we're already "behind" and
    # must pass straight through.
    now[0] += 5.0
    limiter.consume(1_000_000)

    assert slept == []


def test_rate_limiter_does_not_compound_debt_after_stall(monkeypatch):
    """Regression: the previous cumulative-bytes/elapsed implementation
    silently stopped throttling after a long pause because elapsed
    ran far ahead of bytes. A stall followed by a burst of chunks
    should pace each chunk on its own merits, not re-unthrottle
    in a catch-up burst."""
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    slept = []

    def fake_sleep(secs: float) -> None:
        slept.append(secs)
        now[0] += secs

    monkeypatch.setattr(time, "sleep", fake_sleep)

    rate = 1_000_000  # 1 MB/s
    limiter = _RateLimiter(rate)

    # First chunk paces normally.
    limiter.consume(100_000)  # ideal 0.1 s
    assert 0.09 <= slept[-1] <= 0.11

    # Simulate a 10-second socket stall (network pause, retry, etc.).
    now[0] += 10.0

    # Next three chunks should still be paced at the per-chunk rate.
    # The first consume after the stall benefits from the 10s delta
    # and sleeps zero — but crucially the cumulative-debt scheme from
    # the old implementation would have given us 99 zero-sleep chunks
    # to compound. Here, after the one-chunk "forgiveness" each
    # subsequent chunk gets paced on its own merits again.
    wall_before = now[0]
    for _ in range(3):
        limiter.consume(100_000)
    wall_after = now[0]

    # Three chunks of 100 KB at 1 MB/s ≈ 0.3 s, minus the one chunk
    # absorbed by the stall delta ≈ 0.2 s real time.
    assert 0.15 <= (wall_after - wall_before) <= 0.25


def test_rate_limiter_steady_state_respects_cap(monkeypatch):
    """Verify the cumulative pacing stays near the cap over many
    chunks."""
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    def fake_sleep(secs: float) -> None:
        now[0] += secs

    monkeypatch.setattr(time, "sleep", fake_sleep)

    rate = 10_000_000  # 10 MB/s
    limiter = _RateLimiter(rate)

    total_bytes = 0
    for _ in range(100):
        # Each chunk is 64 KB like the real downloader.
        limiter.consume(65536)
        total_bytes += 65536

    elapsed = now[0] - 1000.0
    # Accept a small window around the ideal: 100 chunks × 64 KB ≈
    # 6.4 MB, at 10 MB/s that's 0.64 s. Real-world jitter would be
    # bigger; in this synthetic test we should land within 5%.
    ideal = total_bytes / rate
    assert 0.95 * ideal <= elapsed <= 1.05 * ideal


# --- shared aggregate limiter -----------------------------------------

def test_shared_limiter_unlimited_passes_through(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    limiter = _SharedRateLimiter(0)
    for _ in range(50):
        limiter.consume(1_000_000)

    assert slept == []


def test_shared_limiter_set_rate_swaps_in_new_cap():
    limiter = _SharedRateLimiter(0)
    assert limiter.bytes_per_sec == 0.0

    limiter.set_rate(20_000_000)

    assert limiter.bytes_per_sec == 20_000_000


def test_shared_limiter_set_rate_does_not_reset_when_unchanged():
    """Regression: set_rate is called per-track from every worker.
    A same-rate call must NOT wipe accumulated tokens or last-tick —
    otherwise worker A starting a new track would hand workers B and
    C a fresh full bucket and briefly un-throttle the aggregate."""
    limiter = _SharedRateLimiter(10_000_000)
    # Drain some tokens.
    limiter.consume(2_000_000)
    drained = limiter._tokens
    last_before = limiter._last

    # Same rate again.
    limiter.set_rate(10_000_000)

    # State unchanged.
    assert limiter._tokens == drained
    assert limiter._last == last_before


def test_apply_aggregate_rate_uses_three_times_per_track():
    """The aggregate cap is per-track × 3 so the default 3-worker
    setup hits no ceiling but cranking concurrent_downloads to 10
    does."""
    from app import downloader

    _apply_aggregate_rate(10)
    assert downloader._AGGREGATE_LIMITER.bytes_per_sec == 30_000_000

    _apply_aggregate_rate(0)
    assert downloader._AGGREGATE_LIMITER.bytes_per_sec == 0.0

    _apply_aggregate_rate(20)
    assert downloader._AGGREGATE_LIMITER.bytes_per_sec == 60_000_000


def test_shared_limiter_is_thread_safe():
    """Multiple workers consuming concurrently must not race the
    token bucket into a corrupt state. Use a generous rate so we
    never actually need to sleep — the test exercises the lock,
    not the wait path."""
    rate = 1_000_000_000  # 1 GB/s — so tokens always cover any consume
    limiter = _SharedRateLimiter(rate)

    def worker():
        for _ in range(100):
            limiter.consume(1_000)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Bucket invariants: rate unchanged, tokens within bounds.
    assert limiter.bytes_per_sec == rate
    assert 0 <= limiter._tokens <= rate


# --- bulk cooldown ----------------------------------------------------

def test_bulk_cooldown_skips_first_call_then_smears(monkeypatch):
    """First bulk enqueue should not wait. Subsequent calls within
    the cooldown window must sleep the remainder."""
    from app import downloader

    # Reset module state.
    downloader._last_bulk_enqueue_at = 0.0
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    slept = []

    def fake_sleep(secs: float) -> None:
        slept.append(secs)
        now[0] += secs

    monkeypatch.setattr(time, "sleep", fake_sleep)

    downloader._wait_for_bulk_cooldown()
    assert slept == []  # first call passes through

    # Second call 1s later must sleep the remaining ~2s of cooldown.
    now[0] += 1.0
    downloader._wait_for_bulk_cooldown()
    assert len(slept) == 1
    assert 1.9 <= slept[0] <= 2.1

    # Third call immediately after second: smears another full 3s.
    downloader._wait_for_bulk_cooldown()
    assert len(slept) == 2
    assert 2.9 <= slept[1] <= 3.1


def test_bulk_cooldown_passes_through_after_long_idle(monkeypatch):
    """Sitting idle past the cooldown window means the next call goes
    through immediately — we don't penalize sporadic users."""
    from app import downloader

    downloader._last_bulk_enqueue_at = 0.0
    now = [2000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    downloader._wait_for_bulk_cooldown()
    # 30 s later — well past the 3 s cooldown.
    now[0] += 30.0
    downloader._wait_for_bulk_cooldown()

    assert slept == []
