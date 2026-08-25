"""Threadpool-saturation diagnostics for /api/diagnostics/concurrency.

227 of server.py's 230 endpoints are sync `def`, which Starlette runs
on anyio's worker threadpool rather than the event loop. Each request
therefore holds a thread for its whole duration, blocking Tidal I/O
included, and the pool defaults to 40 with nothing in the app raising
it. Past 40 concurrent requests, the 41st waits for a thread.

Measured on a synthetic app with the same shape: a fast endpoint
answers in 1-2 ms with 39 slow requests in flight, 273 ms at 45, and
1951 ms at 100. A cliff, not a slope.

Whether Tideway reaches 40 in normal use is the open question, and
it's the leading explanation for four separate reports (UI lag, "Fetch
is aborted", covers not rendering, pause feeling unresponsive). These
counters exist so a user who hits the lag can read the answer off the
endpoint instead of anyone guessing — `started_while_saturated` is the
figure that settles it.
"""
from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Same shape as tests/test_settings_endpoint.py: offline mode on so
    `_require_local_access` doesn't reject an unauthenticated test, and
    the settings snapshot restored afterwards."""
    import app.settings as _settings_mod
    import server

    monkeypatch.setattr(_settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    original_settings = copy.deepcopy(server.settings)
    server.settings.offline_mode = True
    server.downloader.settings = server.settings

    with TestClient(server.app) as c:
        yield c

    server.settings = original_settings
    server.downloader.settings = original_settings


def test_reports_the_real_thread_ceiling(client):
    """Read live off anyio rather than hardcoded, so raising the limiter
    can't silently invalidate the diagnostic. 40 is anyio's default and
    nothing in the app changes it — if this ever fails, someone raised
    it and the contention analysis needs redoing."""
    body = client.get("/api/diagnostics/concurrency").json()

    assert body["thread_pool_size"] == 40


def test_counts_requests_and_records_a_peak(client):
    before = client.get("/api/diagnostics/concurrency").json()
    client.get("/api/diagnostics/concurrency")
    after = client.get("/api/diagnostics/concurrency").json()

    assert after["requests_total"] > before["requests_total"]
    # The request reading the counter is itself in flight.
    assert after["peak_in_flight"] >= 1
    assert after["peak_at"] is not None


def test_quiet_process_reports_no_saturation(client):
    """The negative result matters as much as the positive one. A serial
    test client never fills the pool, so this must read zero — if it
    didn't, the counter would be measuring something other than what it
    claims and a real report could not be trusted."""
    body = client.get("/api/diagnostics/concurrency").json()

    assert body["started_while_saturated"] == 0
    assert body["saturated"] is False


def test_in_flight_unwinds_after_each_request(client):
    """The finally block has to release the slot on every path, or
    in_flight ratchets up and the whole diagnostic reads as permanent
    saturation after a few hundred requests."""
    for _ in range(5):
        client.get("/api/diagnostics/concurrency")
    body = client.get("/api/diagnostics/concurrency").json()

    # Only the reading request itself is outstanding.
    assert body["in_flight"] == 1


def test_a_failing_endpoint_still_releases_its_slot(client):
    """Same guarantee when the handler raises rather than returns — a
    404 goes through the same middleware."""
    before = client.get("/api/diagnostics/concurrency").json()["in_flight"]
    client.get("/api/definitely-not-a-real-endpoint")
    after = client.get("/api/diagnostics/concurrency").json()["in_flight"]

    assert after == before


def test_reports_the_queue_rather_than_just_the_pool(client):
    """in_flight counts accepted requests including any waiting for a
    thread; threads_borrowed is anyio's count of workers checked out.
    The gap between them is the queue, which is the thing worth seeing."""
    body = client.get("/api/diagnostics/concurrency").json()

    assert "threads_borrowed" in body
    assert body["threads_borrowed"] >= 0
    assert body["longest_in_flight_seconds"] >= 0.0
