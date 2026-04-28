"""Tests for the macOS Now Playing bridge.

The bridge calls into PyObjC's MediaPlayer framework, which only
exists on macOS. Tests run on every platform — pytest is part of CI
that has Linux runners — so the tests focus on the platform-gated
no-op paths and the metadata mirroring logic that doesn't depend
on Cocoa at all.

End-to-end "does the menu bar actually show the track" is manual
QA on macOS only.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.audio import macos_now_playing


class _FakeSnapshot:
    """Minimal duck-typed PlayerSnapshot for the bridge's
    update_state() input."""

    def __init__(
        self,
        state: str = "playing",
        position_ms: int = 0,
        duration_ms: int = 0,
    ):
        self.state = state
        self.position_ms = position_ms
        self.duration_ms = duration_ms


def test_start_is_noop_on_non_macos():
    bridge = macos_now_playing.MacOSNowPlayingBridge("http://localhost:8000")
    with patch.object(macos_now_playing.sys, "platform", "linux"):
        bridge.start()
    # Subsequent operations must not raise even though start did
    # nothing — on non-darwin we just want a silent fallback.
    bridge.update_state(_FakeSnapshot())
    bridge.update_metadata(title="x", artist="y")
    bridge.clear()
    # Nothing crashed; no assertions on visible state since there
    # isn't any on non-darwin.


def test_start_is_noop_when_mediaplayer_import_fails():
    """On a stripped pyinstaller bundle or older PyObjC, the
    MediaPlayer framework may not be importable. The bridge must
    log once and degrade rather than crashing the server."""
    bridge = macos_now_playing.MacOSNowPlayingBridge("http://localhost:8000")
    with patch.object(macos_now_playing.sys, "platform", "darwin"):
        # Force the import to fail.
        with patch.dict("sys.modules", {"MediaPlayer": None}):
            bridge.start()
    bridge.update_state(_FakeSnapshot())
    bridge.update_metadata(title="x", artist="y")
    # No crash, no enabled state.
    assert bridge._enabled is False


def test_set_base_url_strips_trailing_slash():
    bridge = macos_now_playing.MacOSNowPlayingBridge("")
    bridge.set_base_url("http://example.com:1234/")
    assert bridge._base_url == "http://example.com:1234"


def test_update_state_caches_when_disabled():
    """When the bridge isn't enabled, update_state should cache
    nothing — no point because there's no Cocoa target to push to,
    and we don't want stale state to leak if the bridge enables
    later in the session (currently it can't, but future code
    might)."""
    bridge = macos_now_playing.MacOSNowPlayingBridge("")
    # _enabled stays False because start() was never called.
    bridge.update_state(_FakeSnapshot(state="playing", position_ms=42_000))
    assert bridge._state == "idle"  # default, not the snapshot's value
    assert bridge._position_ms == 0


def test_update_metadata_caches_when_enabled(monkeypatch):
    """With the bridge enabled (mocked Cocoa target), update_metadata
    must mirror the values into the bridge's instance state and call
    the Cocoa setNowPlayingInfo_ method. Real PyObjC isn't loaded —
    we only verify the metadata caching here, since the Cocoa call
    is platform-specific."""
    bridge = macos_now_playing.MacOSNowPlayingBridge("")
    # Pretend start() succeeded.
    bridge._enabled = True
    bridge._info_center = MagicMock()
    bridge._command_center = MagicMock()
    # Also stub out the import inside _push() so the test runs on
    # any platform.
    fake_mp = MagicMock()
    with patch.dict("sys.modules", {"MediaPlayer": fake_mp}):
        bridge.update_metadata(
            title="DUSTCUTTER",
            artist="Quadeca",
            album="SCRAPYARD",
            duration_ms=164_000,
        )
    assert bridge._title == "DUSTCUTTER"
    assert bridge._artist == "Quadeca"
    assert bridge._album == "SCRAPYARD"
    assert bridge._duration_ms == 164_000
    bridge._info_center.setNowPlayingInfo_.assert_called()


def test_update_state_caches_when_enabled():
    """Symmetric to update_metadata — when enabled, update_state
    mirrors snapshot fields into bridge state."""
    bridge = macos_now_playing.MacOSNowPlayingBridge("")
    bridge._enabled = True
    bridge._info_center = MagicMock()
    fake_mp = MagicMock()
    with patch.dict("sys.modules", {"MediaPlayer": fake_mp}):
        bridge.update_state(
            _FakeSnapshot(state="paused", position_ms=12_345, duration_ms=180_000)
        )
    assert bridge._state == "paused"
    assert bridge._position_ms == 12_345
    assert bridge._duration_ms == 180_000


def test_clear_resets_cached_state():
    bridge = macos_now_playing.MacOSNowPlayingBridge("")
    bridge._enabled = True
    bridge._info_center = MagicMock()
    bridge._title = "old"
    bridge._artist = "stale"
    bridge._state = "playing"
    bridge._position_ms = 99
    fake_mp = MagicMock()
    with patch.dict("sys.modules", {"MediaPlayer": fake_mp}):
        bridge.clear()
    assert bridge._title == ""
    assert bridge._artist == ""
    assert bridge._state == "idle"
    assert bridge._position_ms == 0
