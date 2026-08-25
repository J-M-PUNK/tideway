"""A pause pressed while a track is still loading must survive the load.

`pause()` accepts input in the "loading" state and takes only `_lock`,
while a resolve holds `_pipeline_lock` — so the two genuinely overlap,
and a cold resolve is bounded by `_RESOLVE_TIMEOUT_S` (45 s), which is
a long time for a user to be looking at a play button that hasn't
started yet.

The problem is that the pause leaves no trace. `_load_locked` sets
`_paused = False` as part of opening the new stream, and `play()` sets
it again on the way to "playing", so by the time `play_track()` reaches
its `play()` call there is nothing left to distinguish "the user paused
during this load" from "a fresh load that should start". The pause was
silently discarded and the track started anyway — the button did
nothing, which is the shape of the "pause feels dead" reports.

These tests drive `play_track()` against a stand-in `load` that
reproduces the real one's observable contract: transition to "loading",
open a stream, clear `_paused`, land in "paused". The internals of the
real load are covered elsewhere; what matters here is what
`play_track()` does either side of it.
"""
from __future__ import annotations

from app.audio.player import PCMPlayer


class _FakeStream:
    """Stands in for the sounddevice OutputStream. Only the two
    attributes `play()` touches."""

    def __init__(self) -> None:
        self.active = False
        self.started = False

    def start(self) -> None:
        self.started = True
        self.active = True


def _player() -> PCMPlayer:
    """A no-network player; `session_getter` is never invoked here."""
    return PCMPlayer(lambda: None)


def _install_fake_load(p: PCMPlayer, stream: _FakeStream, during_load=None):
    """Replace `load` with one that mirrors `_load_locked`'s visible
    effects. `during_load` runs at the point the real resolve would be
    in flight — the window in which a pause can land."""

    def fake_load(track_id: str, quality=None):
        with p._lock:
            p._transition("loading", track_id=track_id)
        if during_load is not None:
            during_load()
        with p._lock:
            p._stream = stream
            # Both of these are what the real _load_locked does, and
            # both are why the pause needs carrying separately.
            p._paused = False
            p._transition("paused")
        return p.snapshot()

    p.load = fake_load  # type: ignore[method-assign]


def test_pause_during_load_leaves_playback_stopped():
    p = _player()
    stream = _FakeStream()
    _install_fake_load(p, stream, during_load=lambda: p.pause())

    snap = p.play_track("t1")

    assert stream.started is False, "pause during load was discarded"
    assert snap.state == "paused"


def test_load_without_pause_starts_playback():
    """The fix must not turn every load into a no-op."""
    p = _player()
    stream = _FakeStream()
    _install_fake_load(p, stream)

    snap = p.play_track("t1")

    assert stream.started is True
    assert snap.state == "playing"


def test_pause_before_play_track_does_not_block_the_next_track():
    """Only a pause that lands *during* this load counts. Pausing, then
    picking a new track, has to start that track — otherwise the first
    click after any pause would do nothing."""
    p = _player()
    # Get into a genuinely paused, playing-capable state first.
    first = _FakeStream()
    _install_fake_load(p, first)
    p.play_track("t1")
    p.pause()
    assert p.snapshot().state == "paused"

    second = _FakeStream()
    _install_fake_load(p, second)
    snap = p.play_track("t2")

    assert second.started is True
    assert snap.state == "playing"
