"""Tests for the local-output silencer wiring between CastManager
and PCMPlayer.

When a Cast session opens, the manager flips the global PCMPlayer's
`_external_output_active` flag so the audio callback writes silence
to the local sounddevice output. PCM still flows to the Cast
encoder (the tap happens before the silencer in callback order),
so the receiver gets full-amplitude audio while the Mac speakers
stay quiet.

These tests cover the wiring, not the audio callback itself —
that path is exercised in production playback, not unit tests.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.audio.cast import CastDevice, CastManager


def _device() -> CastDevice:
    return CastDevice(
        id="uuid:test",
        friendly_name="Test Speaker",
        model_name="Test",
        manufacturer="Test",
        cast_type="audio",
        host="192.168.1.50",
        port=8009,
    )


class TestSilencerWiring:
    def test_set_local_silencer_stores_callback(self):
        mgr = CastManager()
        cb = MagicMock()
        mgr.set_local_silencer(cb)
        assert mgr._local_silencer is cb

    def test_set_local_silencer_can_be_unwired(self):
        """Tests need to be able to clear the silencer between
        runs; passing None should clear the slot rather than crash."""
        mgr = CastManager()
        mgr.set_local_silencer(MagicMock())
        mgr.set_local_silencer(None)
        assert mgr._local_silencer is None


class TestSilencerInvocation:
    """End-to-end: manager.disconnect() with a manually-attached
    session should fire the silencer with False so local audio
    comes back. We bypass the connect() path entirely (which would
    need a live Cast device) and assert just the disconnect flow."""

    def test_disconnect_fires_silencer_false(self):
        from app.audio.cast import _SessionState

        mgr = CastManager()
        cb = MagicMock()
        mgr.set_local_silencer(cb)

        # Manually attach a fake session so disconnect has work to do.
        session = _SessionState(device=_device(), cast=MagicMock())
        mgr._session = session

        mgr.disconnect()
        cb.assert_called_once_with(False)

    def test_disconnect_with_no_silencer_doesnt_crash(self):
        """If the silencer was never wired (early-startup edge case
        before server.py runs the lifespan hook), disconnect should
        still work."""
        from app.audio.cast import _SessionState

        mgr = CastManager()
        # No set_local_silencer call.
        session = _SessionState(device=_device(), cast=MagicMock())
        mgr._session = session
        mgr.disconnect()  # should not raise

    def test_disconnect_with_no_session_doesnt_fire_silencer(self):
        """Disconnect on an idle manager shouldn't cause a spurious
        silencer call. The flag should already be False; firing it
        with False again would be harmless but wasteful."""
        mgr = CastManager()
        cb = MagicMock()
        mgr.set_local_silencer(cb)
        mgr.disconnect()  # no session
        cb.assert_not_called()

    def test_silencer_failure_doesnt_break_disconnect(self):
        """A buggy silencer raising shouldn't prevent the rest of
        disconnect from running. The encoder / HTTP server / Cast
        teardown is more important than the local-mute flip."""
        from app.audio.cast import _SessionState

        mgr = CastManager()

        def _bad(active):
            raise RuntimeError("silencer broke")

        mgr.set_local_silencer(_bad)
        session = _SessionState(device=_device(), cast=MagicMock())
        mgr._session = session
        mgr.disconnect()  # should not raise


class TestPoliteDisconnect:
    """Pin the contract that disconnecting a session sends Stop
    to the Cast device before tearing down the connection. Without
    this the device would just go silent (HTTP stream dies, no
    explicit signal), which leaves it sitting on a stale 'now
    playing' state until the next user action elsewhere clears it.

    Also pins that stop_discovery() (the FastAPI shutdown hook
    target) routes to disconnect first, before zeroconf teardown."""

    def test_disconnect_sends_stop_to_device(self):
        from app.audio.cast import _SessionState

        mgr = CastManager()
        cast = MagicMock()
        session = _SessionState(device=_device(), cast=cast)
        mgr._session = session
        mgr.disconnect()
        # Verify mc.stop() was called on the way out.
        cast.media_controller.stop.assert_called_once()
        # And the connection itself was dropped.
        cast.disconnect.assert_called_once()

    def test_stop_discovery_calls_disconnect_first(self):
        """The FastAPI shutdown hook calls stop_discovery, which
        must drain any active Cast session before zeroconf tears
        down. Otherwise the underlying mDNS sockets disappear from
        under pychromecast's feet mid-disconnect."""
        from app.audio.cast import _SessionState

        mgr = CastManager()
        cast = MagicMock()
        session = _SessionState(device=_device(), cast=cast)
        mgr._session = session
        # No browser set up — we're testing only the disconnect
        # path here. stop_discovery should still fire it cleanly
        # even when there's nothing to actually un-browse.
        mgr.stop_discovery()
        cast.media_controller.stop.assert_called_once()
        assert mgr._session is None


class TestPlayerFlag:
    """The setter on PCMPlayer that the silencer ultimately calls."""

    def test_set_external_output_active_toggles_flag(self):
        """We construct a bare PCMPlayer (skipping __init__'s
        sounddevice work) so the flag can be tested without
        touching the audio engine. The setter is the only thing
        being verified here."""
        from app.audio.player import PCMPlayer

        # Bypass __init__ — we only need the lock + flag for this
        # test. PCMPlayer's setter takes self._lock and writes the
        # field; nothing else matters.
        import threading

        player = PCMPlayer.__new__(PCMPlayer)
        player._lock = threading.Lock()
        player._external_output_active = False
        player._seq = 0

        player.set_external_output_active(True)
        assert player._external_output_active is True

        player.set_external_output_active(False)
        assert player._external_output_active is False

    def test_set_external_output_active_idempotent(self):
        """Setting the same value twice is fine; both calls are
        legal and the flag should hold its value."""
        from app.audio.player import PCMPlayer
        import threading

        player = PCMPlayer.__new__(PCMPlayer)
        player._lock = threading.Lock()
        player._external_output_active = False
        player._seq = 0

        player.set_external_output_active(True)
        player.set_external_output_active(True)
        assert player._external_output_active is True
