"""A clearance we hold is always worth trying, however old it is.

Only Cloudflare knows when a `cf_clearance` stops working, and the one
authoritative signal is a 403 on a request we thought was cleared —
which `_fetch` already turns into a refresh.

`aoty_clearance` used to screen on age instead: `get()` returned None
once a clearance passed 25 minutes, so a cookie that still worked went
unused. The next request went out bare, drew AOTY's managed challenge,
and paid a full solve — 30 to 90 seconds driving a hidden window — for
a cookie that would have been accepted. During ordinary use that fired
every 25 minutes and looked, from the Home page, like the AOTY rows
hanging and timing out at random.

The asymmetry is the point: trying a dead cookie costs one cheap 403
and the refresh we would have done anyway, while declining to try a
live one costs a minute of solving. So these pin that age never gates
reuse, and that a genuinely rejected clearance still drives exactly one
re-solve.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app import aoty
from app import aoty_clearance


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = "<html>ok</html>"
        self.encoding = "utf-8"


@pytest.fixture(autouse=True)
def _reset():
    aoty._blocked_at = None
    aoty_clearance.reset_for_tests()
    yield
    aoty._blocked_at = None
    aoty_clearance.reset_for_tests()


def _install(token: str, *, age_sec: float) -> aoty_clearance.Clearance:
    """Seed a held clearance that was obtained `age_sec` ago."""
    clearance = aoty_clearance.Clearance(
        cookies={"cf_clearance": token},
        user_agent="TestUA/1.0",
        obtained_at=time.time() - age_sec,
    )
    with aoty_clearance._state_lock:
        aoty_clearance._clearance = clearance
    return clearance


def test_an_hours_old_clearance_is_still_handed_out():
    _install("old-but-fine", age_sec=3600)
    held = aoty_clearance.get()
    assert held is not None, "age must not gate reuse"
    assert held.cookies["cf_clearance"] == "old-but-fine"


def test_an_old_clearance_is_actually_sent_rather_than_re_solved():
    # The regression in full: an old clearance that Cloudflare still
    # accepts must produce one cleared request and no solve at all.
    _install("old-but-fine", age_sec=3600)
    solves = []
    aoty_clearance.register_solver(
        lambda url, rejected=None: solves.append(url)
        or ({"cf_clearance": "fresh"}, "TestUA/1.0")
    )

    with patch("app.aoty.cffi_requests.get") as get:
        get.return_value = _FakeResponse(200)
        result = aoty._fetch("https://www.albumoftheyear.org/releases/this-week/")

    assert result == "<html>ok</html>"
    assert solves == [], "a working cookie must not trigger a solve"
    assert get.call_count == 1
    # And it went out carrying the old cookie, not bare.
    assert get.call_args.kwargs["cookies"] == {"cf_clearance": "old-but-fine"}
    assert get.call_args.kwargs["headers"] == {"User-Agent": "TestUA/1.0"}


def test_an_old_clearance_that_is_refused_still_re_solves_once():
    # The other half: when the cookie really is dead, the 403 is the
    # signal, and it drives exactly one solve rather than none or many.
    _install("actually-dead", age_sec=3600)
    seen = []

    def solver(url, rejected=None):
        seen.append(rejected)
        return ({"cf_clearance": "fresh"}, "TestUA/1.0")

    aoty_clearance.register_solver(solver)
    ok = _FakeResponse(200)
    with patch("app.aoty.cffi_requests.get") as get:
        get.side_effect = [
            _FakeResponse(403, {"cf-mitigated": "challenge"}),
            ok,
        ]
        result = aoty._fetch("https://www.albumoftheyear.org/releases/this-week/")

    assert result == "<html>ok</html>"
    assert aoty.is_scraper_blocked() is False
    # The solver was told which value failed, so it can wait for a
    # different one rather than handing the same dud back.
    assert seen == ["actually-dead"]
    assert get.call_count == 2


def test_refresh_returns_a_concurrent_solve_regardless_of_its_age():
    # Two threads miss at once; the loser must accept whatever the
    # winner produced. Screening that on age would send the loser off
    # to solve again for no reason.
    held = _install("solved-by-another-thread", age_sec=9999)
    aoty_clearance.register_solver(
        lambda url, rejected=None: pytest.fail("should not solve")
    )
    got = aoty_clearance.refresh(stale=None, probe_url="https://example.invalid/")
    assert got is held
