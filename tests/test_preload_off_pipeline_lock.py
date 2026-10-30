"""preload() does its network work without holding _pipeline_lock.

The frontend preloads the next track ten seconds into every track. The
resolve and decoder init behind that are network round-trips, and they
used to run under _pipeline_lock, so a click landing in that window
queued behind the whole preload before its own load() could start.

They now run unlocked and the result is installed under the lock only
if nothing superseded it meanwhile. These pin both halves: the lock is
free during the slow part, and a drop, a load, or a click on the very
track being preloaded keeps a late result from landing as "next".
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

import app.audio.player as player_mod
from app.audio.player import PCMPlayer, StreamInfo


@pytest.fixture
def player(monkeypatch):
    p = PCMPlayer(lambda: None)
    p._current_track_id = "t1"
    p._state = "playing"
    p._stream_sample_rate = 44100

    decoders: list[MagicMock] = []

    def make_decoder(source):
        d = MagicMock()
        d.output_sample_rate = 44100
        d.channels = 2
        d.sounddevice_dtype = "int16"
        # The decoder thread starts on install; give it nothing to do.
        d.next_pcm.return_value = None
        decoders.append(d)
        return d

    monkeypatch.setattr(player_mod, "_build_source", lambda spec, prefetched=None: object())
    monkeypatch.setattr(player_mod, "Decoder", make_decoder)
    p.decoders = decoders  # type: ignore[attr-defined]
    return p


def _resolve_running(p, during):
    """Stand in for the network resolve; `during` runs while it's in
    flight, which is the window a click or a stop can land in."""

    def resolve(track_id, quality, set_current_meta=True):
        during()
        return (["init", "seg1"], 180.0, StreamInfo(source="stream"), {}, None)

    p._resolve_source = resolve  # type: ignore[method-assign]


def test_pipeline_lock_is_free_during_the_network_work(player):
    got_lock: list[bool] = []

    def try_lock_from_another_thread():
        def probe():
            acquired = player._pipeline_lock.acquire(blocking=False)
            got_lock.append(acquired)
            if acquired:
                player._pipeline_lock.release()

        t = threading.Thread(target=probe)
        t.start()
        t.join()

    _resolve_running(player, try_lock_from_another_thread)
    result = player.preload("t2")

    # What a click's load() needs to start while this preload is busy.
    assert got_lock == [True]
    assert result["ok"] is True
    assert player._preload is not None and player._preload.track_id == "t2"


def test_a_drop_during_the_build_discards_it(player):
    _resolve_running(player, player._drop_preload)
    result = player.preload("t2")

    assert result == {"ok": False, "superseded": True}
    assert player._preload is None
    player.decoders[0].close.assert_called_once()


def test_a_load_during_the_build_discards_it(player):
    def load_starts():
        # What _load_locked's slow path does on entry.
        with player._lock:
            player._preload_gen += 1

    _resolve_running(player, load_starts)
    assert player.preload("t2")["superseded"] is True
    assert player._preload is None


def test_clicking_the_track_being_preloaded_keeps_it_out_of_next(player):
    def user_clicks_t2():
        player._current_track_id = "t2"

    _resolve_running(player, user_clicks_t2)
    assert player.preload("t2")["superseded"] is True
    # Installed, it would replay t2 at the end of t2.
    assert player._preload is None


def test_target_rate_is_read_when_installing(player):
    def stream_reopened_at_48k():
        player._stream_sample_rate = 48000

    _resolve_running(player, stream_reopened_at_48k)
    player.preload("t2")

    player.decoders[0].set_target_rate.assert_called_once_with(
        48000, float_output=PCMPlayer._float_output_for(False)
    )


def test_load_supersedes_a_preload_in_flight(player):
    # The bump has to happen on a real load, not only in the stand-in
    # above.
    player._stream = None
    player._teardown = MagicMock()  # type: ignore[method-assign]
    player._resolve_source = MagicMock(side_effect=RuntimeError("offline"))  # type: ignore[method-assign]
    before = player._preload_gen

    player.load("t3")

    assert player._preload_gen > before
