"""The Tidal session loads on a background boot thread so the app window
opens without blocking on a Tidal round-trip at import.

The load being asynchronous creates one hazard: if the first auth check
runs before the session finishes loading, it would inspect an empty
session, decide "logged out", and cache that for the whole 30s TTL —
flashing the login screen on every cold start. `_is_logged_in` guards
against that by waiting on `_session_ready`. These tests pin both halves
of that contract: the boot thread signals readiness, and `_is_logged_in`
blocks until it does.
"""
from __future__ import annotations

import threading

import server


def test_boot_thread_signals_session_ready():
    # The boot thread is spawned at import; even with no valid session
    # file it sets the event (load_session returns fast). Bounded so a
    # wedged load can't hang CI.
    assert server._session_ready.wait(timeout=10.0)


def test_is_logged_in_blocks_until_session_ready():
    # The boot thread spawned at import sets _session_ready when it
    # finishes. If it's still running when this test clears the event,
    # it re-sets it mid-test and the probe below doesn't block. Join
    # it first so the clear is authoritative.
    for t in threading.enumerate():
        if t.name == "tidal-session-boot":
            t.join(timeout=30.0)
    server._invalidate_auth_cache()
    server._session_ready.clear()
    try:
        result: dict[str, object] = {}

        def call() -> None:
            result["value"] = server._is_logged_in()

        t = threading.Thread(target=call, name="auth-probe")
        t.start()
        # Session isn't "ready" yet — the call must still be blocked.
        t.join(timeout=0.3)
        assert t.is_alive(), "_is_logged_in returned before the session was ready"

        # Releasing readiness lets it resolve promptly.
        server._session_ready.set()
        t.join(timeout=15.0)
        assert not t.is_alive(), "_is_logged_in did not unblock after _session_ready"
        assert isinstance(result.get("value"), bool)
    finally:
        # Leave the event set so later tests (and the running app) don't
        # block on a flag this test cleared.
        server._session_ready.set()
