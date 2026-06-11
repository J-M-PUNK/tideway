"""Regression: _graceful_shutdown must close the audio stream.

sounddevice registers an atexit Pa_Terminate(). If the OutputStream
is still open when the interpreter finalizes, PortAudio tears it
down from that atexit hook against a live CoreAudio callback and
SIGABRTs (malloc heap corruption -> the macOS "Tideway quit
unexpectedly" dialog). This only began once the macOS quit paths
actually terminated the process. The shutdown path therefore has to
stop the player (ordered abort + close) before main() returns, and
this test pins that so a future refactor can't silently drop it.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import desktop
import server


@pytest.fixture(autouse=True)
def reset_graceful_latch():
    """_graceful_shutdown is idempotent via a module-level latch (the
    post-start() path, the Linux closed handler, and the exit watchdog
    all call it). Clear it so each test exercises a fresh shutdown."""
    desktop._graceful_ran.clear()
    yield
    desktop._graceful_ran.clear()


def test_graceful_shutdown_stops_audio_player(monkeypatch):
    player = MagicMock(name="pcm_player")
    monkeypatch.setattr(server, "_pcm_player_singleton", player, raising=False)
    monkeypatch.setattr(
        server.downloader, "_persist_pending", MagicMock(), raising=False
    )
    fake_server = MagicMock(name="uvicorn_server")

    desktop._graceful_shutdown(fake_server)

    # uvicorn told to drain, downloader flushed, and crucially the
    # audio stream torn down before the process exits.
    assert fake_server.should_exit is True
    server.downloader._persist_pending.assert_called_once()
    player.stop.assert_called_once()


def test_graceful_shutdown_survives_no_player(monkeypatch):
    # Quitting before any track ever played: the singleton is None.
    # Must not raise on the way out.
    monkeypatch.setattr(server, "_pcm_player_singleton", None, raising=False)
    monkeypatch.setattr(
        server.downloader, "_persist_pending", MagicMock(), raising=False
    )
    desktop._graceful_shutdown(MagicMock())


def test_graceful_shutdown_swallows_player_stop_errors(monkeypatch):
    # A teardown failure must not abort the shutdown — better a leaked
    # stream than a crash dialog hiding the real exit.
    player = MagicMock(name="pcm_player")
    player.stop.side_effect = RuntimeError("device already gone")
    monkeypatch.setattr(server, "_pcm_player_singleton", player, raising=False)
    monkeypatch.setattr(
        server.downloader, "_persist_pending", MagicMock(), raising=False
    )
    desktop._graceful_shutdown(MagicMock())
    player.stop.assert_called_once()
