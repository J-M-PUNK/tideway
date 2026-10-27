"""WASAPI stream starts need COM on the starting thread.

PortAudio only initialises COM on the thread that ran Pa_Initialize, and
Tideway starts streams from HTTP workers, the gapless bridge and device
recovery. A start on a thread without COM failed with "Error starting
stream ... WdmSyncIoctl", which is what the "skips through five tracks,
then plays the sixth" reports on a USB DAC were. These run against a
fake ole32 so they hold on every platform.
"""
from __future__ import annotations

import ctypes
import threading
from unittest.mock import MagicMock

import pytest

from app.audio import win_com
from app.audio.player import PCMPlayer


@pytest.fixture
def ole32(monkeypatch):
    fake = MagicMock()
    fake.CoInitializeEx.return_value = 0
    monkeypatch.setattr(win_com.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", MagicMock(ole32=fake), raising=False)
    # Each test starts from a thread that hasn't been initialised.
    monkeypatch.setattr(win_com, "_initialised", threading.local())
    return fake


def test_initialises_once_per_thread(ole32):
    win_com.ensure_com_initialized()
    win_com.ensure_com_initialized()
    assert ole32.CoInitializeEx.call_count == 1
    # Multithreaded apartment: worker threads have no message pump.
    assert ole32.CoInitializeEx.call_args.args == (None, 0x0)

    other = threading.Thread(target=win_com.ensure_com_initialized)
    other.start()
    other.join()
    assert ole32.CoInitializeEx.call_count == 2


@pytest.mark.parametrize(
    "hr",
    [
        1,  # S_FALSE: already initialised here
        -2147417850,  # RPC_E_CHANGED_MODE: already a single-threaded apartment
    ],
)
def test_already_initialised_threads_are_fine(ole32, hr):
    ole32.CoInitializeEx.return_value = hr
    win_com.ensure_com_initialized()  # must not raise


def test_a_real_failure_is_raised(ole32):
    ole32.CoInitializeEx.return_value = -2147024882  # E_OUTOFMEMORY
    with pytest.raises(OSError, match="8007000E"):
        win_com.ensure_com_initialized()


def test_noop_off_windows(monkeypatch):
    monkeypatch.setattr(win_com.sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "windll", None, raising=False)
    win_com.ensure_com_initialized()  # never touches ctypes.windll


def test_stream_starts_go_through_com_first(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(
        "app.audio.player.ensure_com_initialized", lambda: order.append("com")
    )
    stream = MagicMock()
    stream.start.side_effect = lambda: order.append("start")

    PCMPlayer._start_stream(stream)

    assert order == ["com", "start"]


def test_play_starts_the_stream_through_the_helper(monkeypatch):
    p = PCMPlayer(lambda: None)
    stream = MagicMock()
    stream.active = False
    p._stream = stream
    p._state = "paused"
    started: list[object] = []
    monkeypatch.setattr(PCMPlayer, "_start_stream", staticmethod(started.append))

    p.play()

    assert started == [stream]
    stream.start.assert_not_called()
