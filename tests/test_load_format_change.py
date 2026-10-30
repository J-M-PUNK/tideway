"""A track change across output formats keeps the pipeline it built.

With a stream open, load() builds the new track's pipeline against the
stream's rate. When the result still can't feed that stream — a 16-bit
track after a 24-bit one at the mixer rate is the common case — only
the stream has to change. load() used to throw the pipeline away and
rebuild the same one through the full-teardown path, paying the
decoder init twice (`teardown=~800ms` then `decoder_init=~300ms`).

These drive the real load() with the pipeline build and the output
stream stubbed, and pin: one build, no teardown, the old stream
replaced by one in the new format, and the old stream's
finished_callback muzzled so it can't bridge onto the stale preload.
"""
from __future__ import annotations

import queue
import threading
from unittest.mock import MagicMock

import pytest

from app.audio.player import PCMPlayer, _Preload


class _FakeStream:
    def __init__(self, player: PCMPlayer | None = None) -> None:
        self._player = player
        self.active = False
        self.aborted = False
        self.closed = False

    def abort(self) -> None:
        self.aborted = True
        self.active = False
        # PortAudio runs the finished_callback as the stream goes
        # inactive, before abort() returns.
        if self._player is not None:
            self._player._on_stream_finished()

    def close(self) -> None:
        self.closed = True

    def start(self) -> None:
        self.active = True


def _pipeline(track_id: str, sd_dtype: str) -> _Preload:
    thread = MagicMock()
    thread.is_alive.return_value = False
    return _Preload(
        track_id=track_id,
        quality=None,
        duration_ms=180_000,
        stream_info=None,
        source_urls=["init", "seg1"],
        source_path=None,
        decoder=MagicMock(),
        queue=queue.Queue(),
        thread=thread,
        stop_flag=threading.Event(),
        done=threading.Event(),
        sample_rate=44100,
        channels=2,
        sd_dtype=sd_dtype,
    )


@pytest.fixture
def playing_24bit():
    """A player mid-track on a 44.1 kHz / int32 stream, with the next
    track already preloaded (the stale preload a click discards)."""
    p = PCMPlayer(lambda: None)
    old_stream = _FakeStream(p)
    old_stream.active = True
    p._stream = old_stream
    p._stream_sample_rate = 44100
    p._stream_sd_dtype = "int32"
    p._stream_channels = 2
    p._current_track_id = "t1"
    p._state = "playing"
    p._decoder = MagicMock()
    p._preload = _pipeline("t9", "int32")

    built: list[str] = []
    new_streams: list[tuple] = []

    def build(track_id, quality, match_rate=None):
        built.append(track_id)
        return _pipeline(track_id, "int16")

    def open_stream(rate, channels, dtype):
        new_streams.append((rate, channels, dtype, p._decoder))
        p._stream = _FakeStream()
        p._stream_sample_rate = rate
        p._stream_channels = channels
        p._stream_sd_dtype = dtype

    p._build_load_pipeline = build  # type: ignore[method-assign]
    p._open_output_stream = open_stream  # type: ignore[method-assign]
    p._teardown = MagicMock(side_effect=AssertionError("full teardown"))  # type: ignore[method-assign]
    p._bridge_to_preload = MagicMock()  # type: ignore[method-assign]
    return p, old_stream, built, new_streams


def test_format_change_reuses_the_built_pipeline(playing_24bit):
    p, old_stream, built, new_streams = playing_24bit

    snap = p.load("t2")

    assert built == ["t2"], "the pipeline was built more than once"
    p._teardown.assert_not_called()
    assert snap.state == "paused"
    assert p._current_track_id == "t2"


def test_format_change_replaces_the_stream_in_the_new_format(playing_24bit):
    p, old_stream, built, new_streams = playing_24bit

    p.load("t2")

    assert old_stream.aborted and old_stream.closed
    assert len(new_streams) == 1
    rate, channels, dtype, decoder_at_open = new_streams[0]
    assert (rate, channels, dtype) == (44100, 2, "int16")
    # Opened after the swap, so it configured the new track's decoder,
    # not the outgoing one.
    assert decoder_at_open is p._decoder
    assert p._stream_sd_dtype == "int16"
    # Opened but not started: load() leaves "paused" and play() starts it.
    assert p._stream.active is False


def test_old_streams_finished_callback_is_muzzled(playing_24bit):
    p, old_stream, built, new_streams = playing_24bit

    p.load("t2")

    # Unmuzzled, the outgoing stream's finished_callback would bridge
    # onto the stale preload for t9.
    p._bridge_to_preload.assert_not_called()
    assert p._replacing_stream is False
    assert p._preload is None
    assert p._state == "paused"


def test_matching_format_keeps_the_stream_open(playing_24bit):
    p, old_stream, built, new_streams = playing_24bit
    p._build_load_pipeline = lambda track_id, quality, match_rate=None: (  # type: ignore[method-assign]
        built.append(track_id) or _pipeline(track_id, "int32")
    )

    p.load("t2")

    assert built == ["t2"]
    assert not old_stream.aborted
    assert new_streams == []
    assert p._stream is old_stream
