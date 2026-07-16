"""Tests for the MPRIS bridge (app/mpris.py).

The D-Bus service itself needs a session bus, which CI containers and
macOS dev machines don't have, so these tests cover everything up to
the bus edge: state mapping, metadata assembly, position
extrapolation, seek-fraction math, volume conversion, and the command
routing payloads. The interface classes are additionally
smoke-constructed when dbus-next is importable so a signature typo
fails in CI rather than on a user's desktop.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.mpris import (
    MprisBridge,
    build_metadata,
    playback_status,
    track_object_path,
)

dbus_next = pytest.importorskip("dbus_next", reason="dbus-next not installed")


def _snap(**overrides):
    base = dict(
        state="playing",
        track_id=156951,
        position_ms=10_000,
        duration_ms=200_000,
        volume=80,
        muted=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestMapping:
    def test_playback_status(self):
        assert playback_status("playing") == "Playing"
        assert playback_status("paused") == "Paused"
        assert playback_status("idle") == "Stopped"
        assert playback_status("") == "Stopped"

    def test_track_object_path(self):
        assert track_object_path(156951) == "/org/tideway/track/156951"
        # Non-alphanumerics fold to underscores so the path stays valid.
        assert track_object_path("a/b c.flac") == "/org/tideway/track/a_b_c_flac"
        assert (
            track_object_path(None)
            == "/org/mpris/MediaPlayer2/TrackList/NoTrack"
        )

    def test_build_metadata_full(self):
        md = build_metadata(
            42, "Sicko Mode", "Travis Scott", "Astroworld", 312_000, "https://x/a.jpg"
        )
        assert md["mpris:trackid"] == "/org/tideway/track/42"
        assert md["mpris:length"] == 312_000_000  # microseconds
        assert md["xesam:title"] == "Sicko Mode"
        assert md["xesam:artist"] == ["Travis Scott"]
        assert md["xesam:album"] == "Astroworld"
        assert md["mpris:artUrl"] == "https://x/a.jpg"

    def test_build_metadata_omits_empty_fields(self):
        md = build_metadata(None, "", "", "", 0, "")
        assert md == {
            "mpris:trackid": "/org/mpris/MediaPlayer2/TrackList/NoTrack"
        }


class TestBridgeState:
    def test_update_state_caches_and_position_extrapolates(self):
        bridge = MprisBridge()
        bridge.update_state(_snap(position_ms=10_000))
        # Frozen while paused / at the cached instant.
        assert bridge.current_position_us() >= 10_000_000
        # Playing: advances with the wall clock.
        bridge._position_at = time.monotonic() - 2.0
        pos = bridge.current_position_us()
        assert 11_500_000 < pos < 13_500_000

    def test_position_frozen_when_paused(self):
        bridge = MprisBridge()
        bridge.update_state(_snap(state="paused", position_ms=30_000))
        bridge._position_at = time.monotonic() - 5.0
        assert bridge.current_position_us() == 30_000_000

    def test_position_clamped_to_duration(self):
        bridge = MprisBridge()
        bridge.update_state(_snap(position_ms=199_500))
        bridge._position_at = time.monotonic() - 10.0
        assert bridge.current_position_us() == 200_000_000

    def test_volume_double_and_mute(self):
        bridge = MprisBridge()
        bridge.update_state(_snap(volume=80))
        assert bridge.volume_double() == pytest.approx(0.8)
        bridge.update_state(_snap(volume=80, muted=True))
        assert bridge.volume_double() == 0.0

    def test_metadata_variants_wrap_types(self):
        from dbus_next import Variant

        bridge = MprisBridge()
        bridge.update_state(_snap())
        bridge.update_metadata(
            title="Song",
            artist="Artist",
            album="Album",
            duration_ms=200_000,
            artwork_url="https://x/a.jpg",
        )
        md = bridge.metadata_variants()
        assert isinstance(md["mpris:trackid"], Variant)
        assert md["mpris:trackid"].signature == "o"
        assert md["mpris:length"].signature == "x"
        assert md["mpris:length"].value == 200_000_000
        assert md["xesam:artist"].signature == "as"
        assert md["xesam:artist"].value == ["Artist"]


class TestSeekDetection:
    def test_discontinuous_jump_flags_seek(self, monkeypatch):
        bridge = MprisBridge()
        emitted = []
        monkeypatch.setattr(
            bridge, "_emit", lambda changed, seeked: emitted.append(seeked)
        )
        bridge.update_state(_snap(position_ms=10_000))
        # Same track, position leaps 60s: a seek.
        bridge.update_state(_snap(position_ms=70_000))
        assert emitted == [False, True]

    def test_track_change_is_not_a_seek(self, monkeypatch):
        bridge = MprisBridge()
        emitted = []
        monkeypatch.setattr(
            bridge, "_emit", lambda changed, seeked: emitted.append(seeked)
        )
        bridge.update_state(_snap(track_id=1, position_ms=190_000))
        bridge.update_state(_snap(track_id=2, position_ms=0))
        assert emitted == [False, False]


class TestCommandRouting:
    def test_seek_relative_posts_fraction(self, monkeypatch):
        bridge = MprisBridge(base_url="http://127.0.0.1:1")
        posts = []
        monkeypatch.setattr(
            bridge, "post", lambda path, body=None: posts.append((path, body))
        )
        bridge.update_state(_snap(state="paused", position_ms=100_000))
        bridge.seek_relative_us(10_000_000)  # +10s
        assert posts == [("/api/player/seek", {"fraction": pytest.approx(0.55)})]

    def test_seek_absolute_clamps(self, monkeypatch):
        bridge = MprisBridge()
        posts = []
        monkeypatch.setattr(
            bridge, "post", lambda path, body=None: posts.append((path, body))
        )
        bridge.update_state(_snap(state="paused"))
        bridge.seek_absolute_us(999_000_000_000)
        assert posts == [("/api/player/seek", {"fraction": 1.0})]

    def test_seek_without_duration_is_dropped(self, monkeypatch):
        bridge = MprisBridge()
        posts = []
        monkeypatch.setattr(
            bridge, "post", lambda path, body=None: posts.append((path, body))
        )
        bridge.update_state(_snap(state="idle", duration_ms=0))
        bridge.seek_relative_us(10_000_000)
        assert posts == []

    def test_volume_write_converts_to_percent(self, monkeypatch):
        bridge = MprisBridge()
        posts = []
        monkeypatch.setattr(
            bridge, "post", lambda path, body=None: posts.append((path, body))
        )
        bridge.set_volume_double(0.35)
        bridge.set_volume_double(1.7)  # out-of-range writes clamp
        assert posts == [
            ("/api/player/volume", {"volume": 35}),
            ("/api/player/volume", {"volume": 100}),
        ]


class TestInterfaceConstruction:
    def test_interfaces_build_and_expose_mpris_members(self):
        """Constructing the ServiceInterfaces validates every dbus
        signature annotation via dbus-next's own introspection — a
        typo in a property type fails here, not on a user's desktop."""
        from app.mpris import _build_interfaces

        bridge = MprisBridge()
        bridge.update_state(_snap())
        root, player = _build_interfaces(bridge)
        assert root.name == "org.mpris.MediaPlayer2"
        assert player.name == "org.mpris.MediaPlayer2.Player"

        root_props = {p.name for p in root._get_properties(root)}
        assert {"Identity", "CanRaise", "DesktopEntry"} <= root_props
        player_props = {p.name for p in player._get_properties(player)}
        assert {
            "PlaybackStatus",
            "Metadata",
            "Position",
            "Volume",
            "CanSeek",
            "CanControl",
        } <= player_props
        methods = {m.name for m in player._get_methods(player)}
        assert {
            "Play",
            "Pause",
            "PlayPause",
            "Stop",
            "Next",
            "Previous",
            "Seek",
            "SetPosition",
        } <= methods

    def test_property_getters_read_bridge_state(self):
        from app.mpris import _build_interfaces

        bridge = MprisBridge()
        bridge.update_state(_snap(state="paused", volume=50))
        _, player = _build_interfaces(bridge)
        assert player.PlaybackStatus == "Paused"
        assert player.Volume == pytest.approx(0.5)
        assert player.CanControl is True
