"""Phase 2 of issue #354: the player defers track advance to the
renderer's clock while a bounded DLNA file is being served.

``PCMPlayer._renderer_clock_active`` is the gate: when the UpnpManager
watchdog has confirmed the renderer reports its own position, the muted
desktop decoder must not advance the queue on its earlier EOF. Instead
``on_renderer_track_ended`` (called by the watchdog at the renderer's
real boundary) emits `ended` so the frontend advances, keeping the
preload and 'playing' state so the follow-up play_track(next) adopts it.

These pin the gate, the EOF handling in renderer-clock mode, and the
ended/playing emit contract.
"""
from __future__ import annotations

import queue
import threading
from unittest.mock import MagicMock

from app.audio import player as player_mod
from app.audio.player import PCMPlayer, _Preload


class _FakeUpnp:
    def __init__(self, active: bool = True, clock: bool = True,
                 consumed: str | None = None) -> None:
        self._active = active
        self._clock = clock
        self._consumed = consumed
        self.started: list = []

    def is_active(self) -> bool:
        return self._active

    def renderer_clock_active(self) -> bool:
        return self._clock

    def is_gapless(self) -> bool:
        return True

    def last_consumed_track_id(self):
        return self._consumed

    def start_passthrough(self, source, prefetched=None, metadata=None,
                          *, track_id=None, **kwargs):
        self.started.append(track_id)


class _FakeStream:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _player() -> PCMPlayer:
    return PCMPlayer(lambda: None)


# ---------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------


def test_renderer_clock_active_requires_bounded_session(monkeypatch):
    p = _player()
    monkeypatch.setattr(player_mod, "_upnp_manager", None)
    assert p._renderer_clock_active() is False

    monkeypatch.setattr(player_mod, "_upnp_manager", _FakeUpnp(active=False, clock=True))
    assert p._renderer_clock_active() is False

    monkeypatch.setattr(player_mod, "_upnp_manager", _FakeUpnp(active=True, clock=False))
    assert p._renderer_clock_active() is False

    monkeypatch.setattr(player_mod, "_upnp_manager", _FakeUpnp(active=True, clock=True))
    assert p._renderer_clock_active() is True


def test_try_gapless_swap_bails_on_renderer_clock(monkeypatch):
    p = _player()
    monkeypatch.setattr(player_mod, "_upnp_manager", _FakeUpnp(True, True))
    p._preload = MagicMock()

    assert p._try_gapless_swap() is False
    # The bail must not consume the preload — the renderer-driven adopt
    # still needs it.
    assert p._preload is not None


# ---------------------------------------------------------------------
# EOF in renderer-clock mode
# ---------------------------------------------------------------------


def _prime_eof(p: PCMPlayer, stream, preload) -> None:
    p._stream = stream
    p._preload = preload
    p._state = "playing"
    p._replacing_stream = False
    p._pcm_queue = queue.Queue()
    p._decoder_done = threading.Event()
    p._decoder_done.set()


def test_eof_keeps_preload_and_streamless_in_renderer_clock(monkeypatch):
    p = _player()
    monkeypatch.setattr(player_mod, "_upnp_manager", _FakeUpnp(True, True))
    stream = _FakeStream()
    pre = MagicMock()
    _prime_eof(p, stream, pre)

    p._on_stream_finished()

    assert p._stream is None, "dead local stream must be dropped"
    assert stream.closed is True
    assert p._preload is pre, (
        "preload must survive for the frontend's play_track(next) adopt"
    )
    assert p._state == "playing", (
        "state stays playing; the watchdog drives the real `ended`"
    )


def test_eof_clears_preload_without_renderer_clock():
    p = _player()
    p._upnp_manager = None  # explicit: local clock drives
    stream = _FakeStream()
    _prime_eof(p, stream, None)

    p._on_stream_finished()

    assert p._state == "ended"
    assert p._stream is None


# ---------------------------------------------------------------------
# on_renderer_track_ended contract
# ---------------------------------------------------------------------


def test_on_renderer_track_ended_emits_ended_then_stays_playing(monkeypatch):
    p = _player()
    monkeypatch.setattr(player_mod, "_upnp_manager", _FakeUpnp(True, True))
    p._state = "playing"
    seen = []
    p.subscribe(lambda snap: seen.append(snap.state))

    p.on_renderer_track_ended()

    assert seen == ["ended"]
    assert p._state == "playing"


def test_on_renderer_track_ended_noop_without_clock(monkeypatch):
    p = _player()
    monkeypatch.setattr(player_mod, "_upnp_manager", _FakeUpnp(True, False))
    p._state = "playing"
    seen = []
    p.subscribe(lambda snap: seen.append(snap.state))

    p.on_renderer_track_ended()

    assert seen == []


def test_on_renderer_track_ended_ignores_non_playing_states(monkeypatch):
    p = _player()
    monkeypatch.setattr(player_mod, "_upnp_manager", _FakeUpnp(True, True))
    p._state = "idle"
    seen = []
    p.subscribe(lambda snap: seen.append(snap.state))

    p.on_renderer_track_ended()

    assert seen == []


# ---------------------------------------------------------------------
# Cast (bounded per-track) adoption
# ---------------------------------------------------------------------


def _cast_preload(track_id: str) -> _Preload:
    import queue as _q

    return _Preload(
        track_id=track_id,
        quality=None,
        duration_ms=1000,
        stream_info=None,
        source_urls=["https://x/seg"],
        source_path=None,
        queue=_q.Queue(),
        cast=True,
    )


def test_cast_adopt_emits_playing_and_is_idempotent(monkeypatch):
    """The renderer auto-advanced into the pre-staged next track. The
    player must adopt the cast preload and emit `playing` for N+1 so the
    frontend re-syncs, without touching a decoder. A duplicate fire for
    the same consumed track is a no-op."""
    p = _player()
    fake = _FakeUpnp(True, True, consumed="N1")
    monkeypatch.setattr(player_mod, "_upnp_manager", fake)
    p._state = "playing"
    p._current_track_id = "N0"
    p._preload = _cast_preload("N1")
    seen = []
    p.subscribe(lambda snap: seen.append((snap.state, snap.track_id)))

    p.on_renderer_track_ended()

    assert p._current_track_id == "N1"
    assert p._preload is None
    assert ("playing", "N1") in seen

    before = len(seen)
    p.on_renderer_track_ended()  # duplicate fire
    assert len(seen) == before
    assert p._current_track_id == "N1"


def test_cast_adopt_requires_consumption_evidence(monkeypatch):
    """A fire with no recorded consumption must NOT adopt N+1: that
    would show N+1 in the UI while the renderer is still on N. It falls
    back to the announce path instead."""
    p = _player()
    fake = _FakeUpnp(True, True, consumed=None)
    monkeypatch.setattr(player_mod, "_upnp_manager", fake)
    p._state = "playing"
    p._current_track_id = "N0"
    pre = _cast_preload("N1")
    p._preload = pre
    seen = []
    p.subscribe(lambda snap: seen.append(snap.state))

    p.on_renderer_track_ended()

    assert p._current_track_id == "N0"
    assert p._preload is pre
    assert p._state == "playing"
    assert seen == ["ended"]


def test_cast_adopt_noop_when_consumed_differs_from_preload(monkeypatch):
    p = _player()
    fake = _FakeUpnp(True, True, consumed="OTHER")
    monkeypatch.setattr(player_mod, "_upnp_manager", fake)
    p._state = "playing"
    p._current_track_id = "N0"
    pre = _cast_preload("N1")
    p._preload = pre

    p.on_renderer_track_ended()

    assert p._current_track_id == "N0"
    assert p._preload is pre

