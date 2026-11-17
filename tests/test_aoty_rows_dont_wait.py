"""The AOTY rows never wait on a Cloudflare solve, and still recover.

A solve drives a hidden webview for up to 90 seconds, so a Home request
must not sit through one. A request that finds no usable clearance
starts one solve on a background thread and returns at once; the rows
show as still loading (`pending`, `/api/aoty/status` `solving`) rather
than blocked, and fill in when the solve lands.

Before the background solve, only the startup prewarm could solve. Once
its clearance expired, or if its one attempt failed, the rows stayed
empty until Tideway was restarted, even though AOTY was clearing fine.
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


def _cleared_or_challenged(url, **kwargs):
    """Cloudflare as seen by `_fetch`: 200 with a clearance cookie, else
    the challenge."""
    if (kwargs.get("cookies") or {}).get("cf_clearance"):
        return _Ok()
    return _Challenge()


def _wait_until_idle(timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while aoty_clearance.is_solving():
        assert time.monotonic() < deadline, "the background solve never finished"
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def _reset():
    aoty._blocked_at = None
    aoty_clearance.reset_for_tests()
    yield
    _wait_until_idle()
    aoty._blocked_at = None
    aoty_clearance.reset_for_tests()


def test_a_challenged_request_returns_at_once_and_solves_in_the_background():
    release = threading.Event()
    solves = []

    def solver(url, rejected=None):
        solves.append(url)
        release.wait(5)
        return {"cf_clearance": "fresh"}, "TestUA/1.0"

    aoty_clearance.register_solver(solver)
    with patch("app.aoty.cffi_requests.get", side_effect=_cleared_or_challenged):
        started = time.monotonic()
        assert aoty._fetch(URL) is None
        elapsed = time.monotonic() - started
        assert elapsed < 0.5, f"the request waited on the solve ({elapsed:.2f}s)"

        # Still working on it, which is not the same as blocked.
        assert aoty_clearance.is_solving() is True
        assert aoty.is_scraper_blocked() is False

        release.set()
        _wait_until_idle()
        assert solves == [URL]

        # The next request uses what the background solve earned.
        assert aoty._fetch(URL) == "<html>ok</html>"


def test_a_burst_of_requests_starts_one_solve():
    release = threading.Event()
    solves = []

    def solver(url, rejected=None):
        solves.append(url)
        release.wait(5)
        return {"cf_clearance": "fresh"}, "TestUA/1.0"

    aoty_clearance.register_solver(solver)
    with patch("app.aoty.cffi_requests.get", side_effect=_cleared_or_challenged):
        for _ in range(5):
            assert aoty._fetch(URL) is None
        release.set()
        _wait_until_idle()
    assert solves == [URL]


def test_a_failed_background_solve_blocks_and_holds():
    solves = []
    aoty_clearance.register_solver(lambda url, rejected=None: solves.append(url) or None)
    with patch("app.aoty.cffi_requests.get", return_value=_Challenge()):
        assert aoty._fetch(URL) is None
        _wait_until_idle()
        assert aoty_clearance.solve_failed_recently() is True

        # Inside the hold no new solve starts, and the request now says
        # blocked, because a solve has actually failed.
        assert aoty._fetch(URL) is None
        assert aoty_clearance.is_solving() is False
        assert aoty.is_scraper_blocked() is True
    assert solves == [URL]


def test_a_request_does_not_wait_on_the_prewarm_solve():
    release = threading.Event()
    solving = threading.Event()

    def slow_solver(url, rejected=None):
        solving.set()
        release.wait(5)
        return None

    aoty_clearance.register_solver(slow_solver)

    def prewarm():
        with aoty_clearance.solving_allowed():
            aoty_clearance.refresh(probe_url=URL)

    background = threading.Thread(target=prewarm)
    background.start()
    try:
        assert solving.wait(5), "the prewarm solve never started"
        with patch("app.aoty.cffi_requests.get", return_value=_Challenge()):
            started = time.monotonic()
            assert aoty._fetch(URL) is None
            elapsed = time.monotonic() - started
        assert elapsed < 0.5, f"the request waited on the prewarm solve ({elapsed:.2f}s)"
        # The prewarm's solve is still running, so this is not a block.
        assert aoty.is_scraper_blocked() is False
    finally:
        release.set()
        background.join(5)


def test_a_request_uses_the_clearance_the_prewarm_earned():
    aoty_clearance.register_solver(
        lambda url, rejected=None: ({"cf_clearance": "earned"}, "TestUA/1.0")
    )
    with aoty_clearance.solving_allowed():
        assert aoty_clearance.refresh(probe_url=URL) is not None

    with patch("app.aoty.cffi_requests.get", return_value=_Ok()) as get:
        assert aoty._fetch(URL) == "<html>ok</html>"
    assert get.call_args.kwargs["cookies"] == {"cf_clearance": "earned"}


def test_a_rejected_clearance_picks_up_a_newer_one_without_solving():
    # The request's cookie was refused, but something has since earned
    # a different one: use that rather than solving again.
    solves = []
    aoty_clearance.register_solver(
        lambda url, rejected=None: solves.append(url) or None
    )
    old = aoty_clearance.Clearance({"cf_clearance": "old"}, "TestUA/1.0", time.time())
    newer = aoty_clearance.Clearance({"cf_clearance": "newer"}, "TestUA/1.0", time.time())
    with aoty_clearance._state_lock:
        aoty_clearance._clearance = newer

    assert aoty_clearance.refresh(stale=old, probe_url=URL) is newer
    assert solves == []


def test_solving_allowed_is_scoped_to_its_block():
    assert aoty_clearance._may_solve.get() is False
    with aoty_clearance.solving_allowed():
        assert aoty_clearance._may_solve.get() is True
    assert aoty_clearance._may_solve.get() is False


def test_status_says_solving_not_blocked_during_a_solve(monkeypatch):
    import server

    monkeypatch.setattr(server, "_require_auth", lambda: None)
    release = threading.Event()
    aoty_clearance.register_solver(lambda url, rejected=None: release.wait(5) and None)
    with patch("app.aoty.cffi_requests.get", return_value=_Challenge()):
        assert aoty._fetch(URL) is None
        status = server.aoty_status()
        assert status["solving"] is True
        assert status["blocked"] is False

        release.set()
        _wait_until_idle()
        status = server.aoty_status()
        assert status["solving"] is False
        assert status["blocked"] is True


def test_listing_is_pending_while_a_solve_runs(monkeypatch):
    import server

    monkeypatch.setattr(server, "_require_auth", lambda: None)
    monkeypatch.setattr(server.aoty_resolver, "resolve_listing", lambda page: page)
    release = threading.Event()
    aoty_clearance.register_solver(lambda url, rejected=None: release.wait(5) and None)
    with patch("app.aoty.cffi_requests.get", return_value=_Challenge()):
        page = server.aoty_recent_releases(offset=0, limit=6)
        assert page["items"] == []
        assert page["pending"] is True
        release.set()
        _wait_until_idle()
        assert server.aoty_recent_releases(offset=0, limit=6)["pending"] is False
