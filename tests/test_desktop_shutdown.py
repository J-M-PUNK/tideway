"""Tests for the desktop shell's shutdown path.

Covers the Linux quit-hang fix: `_graceful_shutdown` must be
idempotent (the post-start() path, the Linux closed handler, and the
exit watchdog all call it) and keep going past failing steps, and the
exit watchdog must force a logged exit when the close sequence wedges
before the `closed` event (the GTK geometry-read deadlock class).

The watchdog's force-exit is `os._exit`, monkeypatched to a recorder
here. Every test that arms a watchdog waits for the recorder to fire
before returning — a watchdog thread outliving the monkeypatch would
call the REAL os._exit and kill the pytest process.
"""
from __future__ import annotations

import sys
import threading

import pytest

import desktop


class _StubUvicorn:
    def __init__(self):
        self.should_exit = False


class _StubPlayer:
    def __init__(self, raise_on_stop: bool = False):
        self.stopped = 0
        self.raise_on_stop = raise_on_stop

    def stop(self):
        self.stopped += 1
        if self.raise_on_stop:
            raise RuntimeError("portaudio teardown boom")


class _StubDownloader:
    def __init__(self):
        self.persisted = 0

    def _persist_pending(self):
        self.persisted += 1


class _StubServerModule:
    """Stands in for the real `server` module inside
    `_graceful_shutdown`'s lazy imports."""

    def __init__(self, player=None):
        self._pcm_player_singleton = player
        self.macos_now_playing_bridge = None
        self.downloader = _StubDownloader()


@pytest.fixture(autouse=True)
def reset_shutdown_state():
    """The shutdown latches are module-level Events; clear them so
    each test starts from a fresh process-lifetime state."""
    desktop._graceful_ran.clear()
    desktop._watchdog_armed.clear()
    yield
    desktop._graceful_ran.clear()
    desktop._watchdog_armed.clear()


@pytest.fixture
def stub_server_module(monkeypatch):
    stub = _StubServerModule(player=_StubPlayer())
    monkeypatch.setitem(sys.modules, "server", stub)
    return stub


@pytest.fixture
def exit_recorder(monkeypatch):
    """Replace os._exit with a recorder. Returns (event, codes):
    `event` fires on the first call, `codes` collects exit codes."""
    fired = threading.Event()
    codes: list[int] = []

    def _record(code):
        codes.append(code)
        fired.set()

    monkeypatch.setattr(desktop.os, "_exit", _record)
    return fired, codes


def test_graceful_shutdown_runs_once_and_flushes_everything(stub_server_module):
    uv = _StubUvicorn()
    desktop._graceful_shutdown(uv)
    desktop._graceful_shutdown(uv)  # second call is a no-op

    assert uv.should_exit is True
    assert stub_server_module.downloader.persisted == 1
    assert stub_server_module._pcm_player_singleton.stopped == 1


def test_graceful_shutdown_continues_past_failing_steps(monkeypatch):
    """A wedged audio teardown must not stop the download-queue
    persist — each step is independent."""
    stub = _StubServerModule(player=_StubPlayer(raise_on_stop=True))
    monkeypatch.setitem(sys.modules, "server", stub)
    uv = _StubUvicorn()

    desktop._graceful_shutdown(uv)  # must not raise

    assert stub._pcm_player_singleton.stopped == 1
    assert uv.should_exit is True
    assert stub.downloader.persisted == 1


def test_watchdog_forces_exit_when_close_wedges(stub_server_module, exit_recorder):
    """The deadlock class: close began (closing fired) but `closed`
    never arrived. The watchdog must finish the shutdown work itself
    and force-exit."""
    fired, codes = exit_recorder
    uv = _StubUvicorn()

    desktop._arm_exit_watchdog(uv, deadline_s=0.05)

    assert fired.wait(5.0), "watchdog never forced an exit"
    assert codes == [0]
    # The watchdog ran the graceful shutdown before exiting.
    assert uv.should_exit is True
    assert stub_server_module.downloader.persisted == 1


def test_watchdog_does_not_rerun_completed_shutdown(
    stub_server_module, exit_recorder
):
    """If the clean path already flushed state, the watchdog's own
    call is a no-op — no double persist."""
    fired, codes = exit_recorder
    uv = _StubUvicorn()

    desktop._graceful_shutdown(uv)
    assert stub_server_module.downloader.persisted == 1

    desktop._arm_exit_watchdog(uv, deadline_s=0.05)

    assert fired.wait(5.0), "watchdog never forced an exit"
    assert codes == [0]
    assert stub_server_module.downloader.persisted == 1


def test_watchdog_arms_only_once(stub_server_module, exit_recorder, monkeypatch):
    fired, codes = exit_recorder
    uv = _StubUvicorn()

    started: list[str] = []
    real_thread = threading.Thread

    class _CountingThread(real_thread):
        def __init__(self, *args, **kwargs):
            started.append(kwargs.get("name", ""))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(desktop.threading, "Thread", _CountingThread)
    desktop._arm_exit_watchdog(uv, deadline_s=0.05)
    desktop._arm_exit_watchdog(uv, deadline_s=0.05)

    assert fired.wait(5.0)
    assert started.count("exit-watchdog") == 1
