"""A signed-in session past the auth cache TTL is re-checked in the
background, not on the request.

Every player endpoint runs the `_require_local_access` gate, and the
gate's check_login() is a Tidal round-trip once the 30 s cache ages
out. Done on the request, that round-trip landed in front of whichever
call came first after the TTL — a play click after a few minutes of
listening, typically — at 300 ms to over a second.
"""
from __future__ import annotations

import threading
import time

import pytest

import server


@pytest.fixture(autouse=True)
def _fresh_auth_cache():
    server._session_ready.set()
    server._invalidate_auth_cache()
    was_deferred = server.tidal._session_load_deferred
    server.tidal._session_load_deferred = False
    yield
    server.tidal._session_load_deferred = was_deferred
    server._invalidate_auth_cache()


class _SlowCheck:
    """check_login() that blocks until released, recording which
    thread asked."""

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.release = threading.Event()
        self.started = threading.Event()
        self.threads: list[str] = []

    def __call__(self) -> bool:
        self.threads.append(threading.current_thread().name)
        self.started.set()
        assert self.release.wait(5.0)
        return self.answer


def _signed_in_but_stale():
    with server._auth_cache_lock:
        server._auth_cache["at"] = time.monotonic() - server._AUTH_CACHE_TTL - 1
        server._auth_cache["ok"] = True


def _wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.01)
    return pred()


def test_stale_signed_in_answers_without_waiting_for_tidal(monkeypatch):
    check = _SlowCheck()
    monkeypatch.setattr(server.tidal.session, "check_login", check)
    _signed_in_but_stale()

    t0 = time.monotonic()
    assert server._is_logged_in() is True
    assert time.monotonic() - t0 < 0.1

    # The re-check runs, off the request thread, and refreshes the cache.
    assert check.started.wait(2.0)
    assert check.threads == ["auth-recheck"]
    check.release.set()
    assert _wait_until(lambda: not server._auth_cache["refreshing"])
    assert time.monotonic() - server._auth_cache["at"] < server._AUTH_CACHE_TTL


def test_one_background_recheck_at_a_time(monkeypatch):
    check = _SlowCheck()
    monkeypatch.setattr(server.tidal.session, "check_login", check)
    _signed_in_but_stale()

    for _ in range(5):
        assert server._is_logged_in() is True
    assert check.started.wait(2.0)
    check.release.set()
    assert _wait_until(lambda: not server._auth_cache["refreshing"])
    assert len(check.threads) == 1


def test_a_rejection_found_in_the_background_signs_out_the_next_request(monkeypatch):
    check = _SlowCheck(answer=False)
    check.release.set()
    monkeypatch.setattr(server.tidal.session, "check_login", check)
    _signed_in_but_stale()

    assert server._is_logged_in() is True  # answered from the stale result
    assert _wait_until(lambda: server._auth_cache["ok"] is False)
    assert server._is_logged_in() is False


def test_logout_during_a_recheck_is_not_overwritten(monkeypatch):
    check = _SlowCheck(answer=True)
    monkeypatch.setattr(server.tidal.session, "check_login", check)
    _signed_in_but_stale()

    assert server._is_logged_in() is True
    assert check.started.wait(2.0)
    # The user logs out (or a Tidal call loses auth) while the
    # re-check is still on the network.
    server._invalidate_auth_cache()
    check.release.set()
    assert _wait_until(lambda: check.threads and not _recheck_threads_alive())

    # The in-flight check's "signed in" belongs to the old generation.
    assert server._auth_cache["ok"] is False
    assert server._auth_cache["at"] == 0.0


def test_signed_out_still_checks_on_the_request(monkeypatch):
    check = _SlowCheck(answer=False)
    check.release.set()
    monkeypatch.setattr(server.tidal.session, "check_login", check)
    with server._auth_cache_lock:
        server._auth_cache["at"] = time.monotonic() - server._AUTH_CACHE_TTL - 1
        server._auth_cache["ok"] = False

    assert server._is_logged_in() is False
    # No known-good answer to serve, so the caller waits for Tidal.
    assert check.threads == [threading.current_thread().name]


def _recheck_threads_alive() -> bool:
    return any(t.name == "auth-recheck" for t in threading.enumerate())
