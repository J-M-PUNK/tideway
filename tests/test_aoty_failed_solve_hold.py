"""A failed Cloudflare solve stops the requests queued behind it.

Each solve drives a hidden webview for up to 90 seconds, and only one
runs at a time. When one failed, every request that had drawn a
challenge meanwhile was waiting on the solve lock, and each then ran a
full solve of its own against the same wall. The three AOTY rows on
Home hung for minutes and often never loaded. A failed solve now holds
for `_FAILED_SOLVE_HOLD_SEC`, the same ten minutes the Home page shows
its "unavailable" notice, and the queued requests give up at once.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from app import aoty
from app import aoty_clearance

URL = "https://www.albumoftheyear.org/releases/this-week/"


class _Challenge:
    status_code = 403
    headers = {"cf-mitigated": "challenge"}
    text = ""
    encoding = "utf-8"


class _Ok:
    status_code = 200
    headers: dict = {}
    text = "<html>ok</html>"
    encoding = "utf-8"


@pytest.fixture(autouse=True)
def _reset():
    aoty._blocked_at = None
    aoty_clearance.reset_for_tests()
    yield
    aoty._blocked_at = None
    aoty_clearance.reset_for_tests()


@pytest.fixture(autouse=True)
def _as_background_solver():
    # These tests cover the solve path, which only the startup prewarm
    # may take (`solving_allowed()`); a request serving the UI never
    # solves. Run them as the prewarm does.
    with aoty_clearance.solving_allowed():
        yield


def test_requests_queued_behind_a_failed_solve_do_not_solve_again():
    solves = []
    all_challenged = threading.Barrier(3, timeout=5)

    def failing_solver(url, rejected=None):
        solves.append(url)
        time.sleep(0.2)
        return None

    aoty_clearance.register_solver(failing_solver)
    results = []

    def challenged_get(url, **kwargs):
        # Every fetch draws its challenge before any of them reaches
        # the solve lock, the way the Home rows arrive together.
        if "cookies" not in kwargs:
            all_challenged.wait()
        return _Challenge()

    def prewarm_fetch():
        # New threads don't inherit the context variable.
        with aoty_clearance.solving_allowed():
            results.append(aoty._fetch(URL))

    with patch("app.aoty.cffi_requests.get", side_effect=challenged_get):
        threads = [threading.Thread(target=prewarm_fetch) for _ in range(3)]
        started = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        elapsed = time.monotonic() - started

    assert results == [None, None, None]
    assert len(solves) == 1, f"one failed solve, then the rest give up: {solves}"
    assert elapsed < 0.6, f"queued requests waited on more solves ({elapsed:.2f}s)"
    assert aoty.is_scraper_blocked() is True


def test_a_solver_that_raises_also_holds():
    calls = []

    def broken_solver(url, rejected=None):
        calls.append(url)
        raise RuntimeError("window failed to open")

    aoty_clearance.register_solver(broken_solver)
    assert aoty_clearance.refresh(probe_url=URL) is None
    assert aoty_clearance.refresh(probe_url=URL) is None
    assert len(calls) == 1


def test_the_hold_expires(monkeypatch):
    calls = []

    def solver(url, rejected=None):
        calls.append(url)
        return None

    aoty_clearance.register_solver(solver)
    assert aoty_clearance.refresh(probe_url=URL) is None
    monkeypatch.setattr(aoty_clearance, "_FAILED_SOLVE_HOLD_SEC", 0.0)
    assert aoty_clearance.refresh(probe_url=URL) is None
    assert len(calls) == 2, "once the hold has passed, the next request solves again"


def test_a_successful_solve_clears_the_hold(monkeypatch):
    outcomes = iter([None, ({"cf_clearance": "fresh"}, "TestUA/1.0")])
    aoty_clearance.register_solver(lambda url, rejected=None: next(outcomes))

    assert aoty_clearance.refresh(probe_url=URL) is None
    monkeypatch.setattr(aoty_clearance, "_FAILED_SOLVE_HOLD_SEC", 0.0)
    fresh = aoty_clearance.refresh(probe_url=URL)
    assert fresh is not None
    monkeypatch.setattr(aoty_clearance, "_FAILED_SOLVE_HOLD_SEC", 600.0)

    # With the hold cleared, a clearance that is later rejected
    # re-solves straight away rather than sitting out the old failure.
    aoty_clearance.register_solver(
        lambda url, rejected=None: ({"cf_clearance": "newer"}, "TestUA/1.0")
    )
    newer = aoty_clearance.refresh(stale=fresh, probe_url=URL)
    assert newer is not None and newer.cookies["cf_clearance"] == "newer"


def test_a_fetch_still_succeeds_with_a_working_clearance_during_a_hold():
    # The hold only stops new solves. A cookie we already hold is still
    # tried first, so a hold never blocks a fetch that would work.
    aoty_clearance.register_solver(lambda url, rejected=None: None)
    assert aoty_clearance.refresh(probe_url=URL) is None
    with aoty_clearance._state_lock:
        aoty_clearance._clearance = aoty_clearance.Clearance(
            cookies={"cf_clearance": "works"},
            user_agent="TestUA/1.0",
            obtained_at=time.time(),
        )
    with patch("app.aoty.cffi_requests.get", return_value=_Ok()):
        assert aoty._fetch(URL) == "<html>ok</html>"
