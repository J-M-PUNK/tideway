"""The AOTY rows never wait on a Cloudflare solve.

A solve drives a hidden webview for up to 90 seconds, and since
November 2026 AOTY's challenge wants a human click and then rejects the
embedded browser anyway. A Home request that drew a challenge used to
wait on that solve (the startup prewarm's, or its own) and showed the
"unavailable" notice only after most of a minute and a half. Now only
the startup prewarm, inside `solving_allowed()`, may solve; a request
serving the UI uses whatever clearance the prewarm earned, or fails at
once.
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


def test_a_challenged_request_returns_at_once_without_solving():
    solves = []
    aoty_clearance.register_solver(
        lambda url, rejected=None: solves.append(url)
        or ({"cf_clearance": "x"}, "TestUA/1.0")
    )
    with patch("app.aoty.cffi_requests.get", return_value=_Challenge()):
        started = time.monotonic()
        assert aoty._fetch(URL) is None
        elapsed = time.monotonic() - started

    assert solves == [], "a UI request must not start a solve"
    assert elapsed < 0.5
    # The Home notice still goes up.
    assert aoty.is_scraper_blocked() is True


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
    # The request's cookie was refused, but the prewarm has since
    # earned a different one: use that rather than giving up.
    solves = []
    aoty_clearance.register_solver(
        lambda url, rejected=None: solves.append(url) or None
    )
    old = aoty_clearance.Clearance({"cf_clearance": "old"}, "TestUA/1.0", time.time())
    newer = aoty_clearance.Clearance({"cf_clearance": "newer"}, "TestUA/1.0", time.time())
    with aoty_clearance._state_lock:
        aoty_clearance._clearance = newer

    assert aoty_clearance.refresh(stale=old, probe_url=URL) is newer
    assert aoty_clearance.refresh(stale=newer, probe_url=URL) is None
    assert solves == []


def test_solving_allowed_is_scoped_to_its_block():
    assert aoty_clearance._may_solve.get() is False
    with aoty_clearance.solving_allowed():
        assert aoty_clearance._may_solve.get() is True
    assert aoty_clearance._may_solve.get() is False
