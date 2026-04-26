"""Tests for Force Volume.

Force Volume pins the software volume slider at 100 so the audio
callback never multiplies samples by anything less than 1.0. Software
attenuation literally costs bit-depth precision (each halving = ~1
bit lost), which audiophiles avoid by handing volume control to the
DAC, amp, or OS mixer instead. Tidal's official client doesn't expose
this as a named feature; the same effect is achieved there by leaving
the slider at 100 and not touching it. We expose it explicitly so
the slider gets locked and an accidental drag can't break the
bit-perfect chain.
"""
from unittest.mock import MagicMock

from app.audio.player import PCMPlayer


def _player() -> PCMPlayer:
    """A PCMPlayer with stub session_getter. The constructor doesn't
    open a stream, so this is safe even without a real Tidal session."""
    return PCMPlayer(session_getter=lambda: MagicMock())


def test_default_volume_and_force_volume_state():
    p = _player()
    assert p._volume == 100
    assert p._force_volume is False


def test_set_volume_normally_changes_volume():
    p = _player()
    p.set_volume(50)
    assert p._volume == 50


def test_set_volume_clamps_to_valid_range():
    p = _player()
    p.set_volume(150)
    assert p._volume == 100
    p.set_volume(-10)
    assert p._volume == 0


def test_set_volume_is_no_op_when_force_volume_on():
    p = _player()
    p.set_force_volume(True)
    p.set_volume(50)
    assert p._volume == 100, "Force Volume must pin the slider at 100"


def test_enabling_force_volume_snaps_volume_to_100():
    p = _player()
    p.set_volume(30)
    assert p._volume == 30
    p.set_force_volume(True)
    assert p._volume == 100


def test_disabling_force_volume_releases_pin():
    p = _player()
    p.set_force_volume(True)
    p.set_volume(50)
    assert p._volume == 100
    p.set_force_volume(False)
    p.set_volume(50)
    assert p._volume == 50


def test_force_volume_surfaces_in_snapshot():
    p = _player()
    snap = p.snapshot()
    assert snap.force_volume is False
    p.set_force_volume(True)
    snap = p.snapshot()
    assert snap.force_volume is True


def test_set_force_volume_bumps_seq_for_sse_clients():
    """Frontend SSE dedupe drops snapshots whose seq is unchanged.
    Toggling Force Volume must therefore bump seq so the slider's
    disabled state actually re-renders."""
    p = _player()
    seq_before = p._seq
    p.set_force_volume(True)
    assert p._seq > seq_before
    seq_after_on = p._seq
    p.set_force_volume(False)
    assert p._seq > seq_after_on


def test_set_volume_bumps_seq_even_when_clamped():
    """Even when Force Volume blocks the change, seq advances so
    clients re-fetch and see the still-pinned value."""
    p = _player()
    p.set_force_volume(True)
    seq_before = p._seq
    p.set_volume(50)
    assert p._seq > seq_before
    assert p._volume == 100
