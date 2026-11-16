"""Only one of several simultaneous launches starts the app.

The single-instance guard used to be a health probe alone. Launches
started within the same second (a few clicks on the shortcut) all
probed before any of them had a server up, all saw nothing, and all
started: seven Tideway windows over one backend, and an update
installer that couldn't replace the running exe. An exclusive OS lock
in the data dir now decides which launch starts.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

import desktop

_REPO = Path(__file__).resolve().parent.parent

# Child process: try the lock once, report, then hold it until stdin
# closes so the parent decides when it is released.
_CHILD = (
    "import sys; sys.path.insert(0, {repo!r}); import desktop; "
    "print('won' if desktop._try_instance_lock() else 'lost', flush=True); "
    "sys.stdin.read()"
)


@pytest.fixture(autouse=True)
def _release_lock():
    # The lock is process-global; drop whatever a test took so the next
    # test (and the rest of the suite) starts unlocked.
    yield
    held = desktop._instance_lock_file
    desktop._instance_lock_file = None
    if held is not None:
        held.close()


def _spawn():
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD.format(repo=str(_REPO))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )


def test_simultaneous_launches_take_the_lock_exactly_once():
    children = [_spawn() for _ in range(6)]
    try:
        results = [c.stdout.readline().strip() for c in children]
    finally:
        for c in children:
            c.stdin.close()
            c.wait(timeout=10)
    assert sorted(results) == ["lost"] * 5 + ["won"], results


def test_the_lock_frees_when_its_holder_exits():
    holder = _spawn()
    try:
        assert holder.stdout.readline().strip() == "won"
        assert desktop._try_instance_lock() is False
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)
    assert desktop._try_instance_lock() is True


def test_a_launch_that_loses_the_lock_focuses_the_running_instance(monkeypatch):
    monkeypatch.setattr(desktop, "_try_instance_lock", lambda: False)
    probes = iter([False, False, True])
    monkeypatch.setattr(desktop, "_probe_existing_instance", lambda: next(probes))
    focused = []
    monkeypatch.setattr(desktop, "_ask_existing_to_focus", lambda: focused.append(1))
    monkeypatch.setattr(desktop, "_INSTANCE_POLL_SEC", 0.0)

    assert desktop._claim_instance() == "deferred"
    assert focused == [1]


def test_a_launch_takes_over_when_the_holder_goes_away(monkeypatch):
    # A relaunch during the old instance's shutdown: the lock frees
    # before anything answers health, so this launch starts.
    locks = iter([False, False, True])
    monkeypatch.setattr(desktop, "_try_instance_lock", lambda: next(locks))
    monkeypatch.setattr(desktop, "_probe_existing_instance", lambda: False)
    monkeypatch.setattr(desktop, "_INSTANCE_POLL_SEC", 0.0)

    assert desktop._claim_instance() == "start"


def test_holding_the_lock_still_defers_to_an_older_build_on_the_port(monkeypatch):
    # A build from before the lock existed never takes it, so the
    # health probe still has to catch it.
    monkeypatch.setattr(desktop, "_try_instance_lock", lambda: True)
    monkeypatch.setattr(desktop, "_probe_existing_instance", lambda: True)
    monkeypatch.setattr(desktop, "_ask_existing_to_focus", lambda: None)

    assert desktop._claim_instance() == "deferred"


def test_a_wedged_holder_is_reported_not_waited_on_forever(monkeypatch):
    monkeypatch.setattr(desktop, "_try_instance_lock", lambda: False)
    monkeypatch.setattr(desktop, "_probe_existing_instance", lambda: False)
    monkeypatch.setattr(desktop, "_INSTANCE_WAIT_SEC", 0.05)
    monkeypatch.setattr(desktop, "_INSTANCE_POLL_SEC", 0.01)

    started = time.monotonic()
    assert desktop._claim_instance() == "stuck"
    assert time.monotonic() - started < 2
