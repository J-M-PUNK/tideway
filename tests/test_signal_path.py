"""Tests for the /api/player/signal-path endpoint's computed flags.

The endpoint composes several pieces of state (player snapshot,
ReplayGain resolver output, settings dataclass, output stream
state) into a single JSON payload the Signal Path dialog renders.
The interesting bit is the `bit_perfect` computation: it has a
specific rule (track loaded AND no DSP active AND exclusive output
AND no remote receiver) and a future refactor that subtly inverts
one term would mislead audiophile users about what's actually
happening to their audio. Lock the rule in.

Calls the endpoint handler directly as a plain Python function
rather than through TestClient — keeps the test independent of the
orjson dependency and exercises the pure composition logic without
HTTP overhead.
"""
from __future__ import annotations

import copy

import pytest


@pytest.fixture
def stub_player(monkeypatch):
    """Replace `_native_player()` with a stub whose surface is the
    five methods the signal-path endpoint reaches into. Each test
    mutates the returned object's `_state` dict to set up the
    scenario it cares about."""
    import server

    class _StubPlayer:
        def __init__(self):
            self._state = {
                "snapshot_state": "playing",
                "snapshot_track_id": "12345",
                "stream_info": _StubStreamInfo(
                    codec="FLAC",
                    sample_rate_hz=96000,
                    bit_depth=24,
                    audio_quality="HI_RES_LOSSLESS",
                ),
                "rg_mode": "off",
                "rg_applied_db": 0.0,
                "rg_track_gain_db": None,
                "rg_album_gain_db": None,
                "output_stream": {
                    "stream_open": True,
                    "sample_rate_hz": 96000,
                    "channels": 2,
                    "sd_dtype": "int32",
                    "device_id": "5",
                    "device_name": "RME ADI-2 Pro",
                    "external_output_active": False,
                },
            }

        def snapshot(self):
            s = self._state
            return _StubSnapshot(
                state=s["snapshot_state"],
                track_id=s["snapshot_track_id"],
                stream_info=s["stream_info"],
            )

        def replaygain_state(self):
            s = self._state
            return {
                "mode": s["rg_mode"],
                "applied_db": s["rg_applied_db"],
                "preamp_db": 0.0,
                "prevent_clipping": True,
                "track_gain_db": s["rg_track_gain_db"],
                "album_gain_db": s["rg_album_gain_db"],
            }

        def output_stream_state(self):
            return dict(self._state["output_stream"])

    stub = _StubPlayer()
    monkeypatch.setattr(server, "_native_player", lambda: stub)
    monkeypatch.setattr(server, "_require_local_access", lambda: None)

    # Snapshot + restore settings so tests can mutate freely.
    original = copy.deepcopy(server.settings)
    # Default settings for the happy-path bit-perfect scenario.
    server.settings.eq_mode = "off"
    server.settings.eq_bypass = False
    server.settings.eq_enabled = False
    server.settings.eq_bands = []
    server.settings.eq_parametric_bands = []
    server.settings.eq_preamp = None
    server.settings.eq_active_profile_id = ""
    server.settings.crossfeed_amount = 0
    server.settings.exclusive_mode = True
    server.settings.force_volume = False

    yield stub

    server.settings = original


class _StubSnapshot:
    def __init__(self, state, track_id, stream_info):
        self.state = state
        self.track_id = track_id
        self.stream_info = stream_info


class _StubStreamInfo:
    def __init__(self, codec, sample_rate_hz, bit_depth, audio_quality):
        self.codec = codec
        self.sample_rate_hz = sample_rate_hz
        self.bit_depth = bit_depth
        self.audio_quality = audio_quality


# --- bit_perfect rule ---------------------------------------------


def test_bit_perfect_when_nothing_touches_audio(stub_player):
    import server
    out = server.player_signal_path()
    assert out["bit_perfect"] is True
    assert out["track_loaded"] is True


def test_not_bit_perfect_when_idle(stub_player):
    """Idle = no track loaded. The claim "bit-perfect" requires an
    active path; without one, we can't make the claim."""
    import server
    stub_player._state["snapshot_state"] = "idle"
    stub_player._state["snapshot_track_id"] = None
    out = server.player_signal_path()
    assert out["bit_perfect"] is False
    assert out["track_loaded"] is False


def test_not_bit_perfect_without_exclusive_mode(stub_player):
    """Shared-mode WASAPI / CoreAudio can resample on the way to
    the DAC even when no Tideway stage touches the buffer. The
    badge tells the truth about that."""
    import server
    server.settings.exclusive_mode = False
    out = server.player_signal_path()
    assert out["bit_perfect"] is False


def test_not_bit_perfect_with_crossfeed_active(stub_player):
    import server
    server.settings.crossfeed_amount = 30
    out = server.player_signal_path()
    assert out["bit_perfect"] is False
    assert out["crossfeed"]["active"] is True


def test_not_bit_perfect_with_eq_active(stub_player):
    import server
    server.settings.eq_mode = "manual"
    server.settings.eq_enabled = True
    server.settings.eq_parametric_bands = [
        {"type": "PK", "freq": 1000.0, "gain": 1.0, "q": 1.0, "enabled": True}
    ]
    out = server.player_signal_path()
    assert out["bit_perfect"] is False
    assert out["eq"]["active"] is True


def test_flat_manual_bands_stay_bit_perfect(stub_player):
    """A seeded-but-flat parametric layout (all 0 dB) doesn't touch the
    audio, so it must not flip the bit-perfect badge off."""
    import server

    server.settings.eq_mode = "manual"
    server.settings.eq_enabled = True
    server.settings.eq_parametric_bands = [
        {"type": "LSC", "freq": 105.0, "gain": 0.0, "q": 0.7, "enabled": True},
        {"type": "PK", "freq": 1000.0, "gain": 0.0, "q": 1.0, "enabled": True},
        {"type": "HSC", "freq": 10000.0, "gain": 0.0, "q": 0.7, "enabled": True},
    ]
    out = server.player_signal_path()
    assert out["eq"]["active"] is False
    assert out["bit_perfect"] is True


def test_eq_bypass_keeps_bit_perfect(stub_player):
    """A bypassed EQ doesn't touch the audio even though mode != off."""
    import server
    server.settings.eq_mode = "manual"
    server.settings.eq_enabled = True
    server.settings.eq_parametric_bands = [
        {"type": "PK", "freq": 1000.0, "gain": 1.0, "q": 1.0, "enabled": True}
    ]
    server.settings.eq_bypass = True
    out = server.player_signal_path()
    assert out["bit_perfect"] is True
    assert out["eq"]["active"] is False


def test_not_bit_perfect_with_replaygain_applying(stub_player):
    import server
    stub_player._state["rg_mode"] = "track"
    stub_player._state["rg_applied_db"] = -3.5
    stub_player._state["rg_track_gain_db"] = -3.5
    out = server.player_signal_path()
    assert out["bit_perfect"] is False
    assert out["replaygain"]["active"] is True


def test_replaygain_mode_but_no_tags_still_bit_perfect(stub_player):
    """User has RG mode set, but the active track has no tags so the
    resolver returns 0 dB → stage is inert and we're still bit-perfect."""
    import server
    stub_player._state["rg_mode"] = "track"
    stub_player._state["rg_applied_db"] = 0.0
    stub_player._state["rg_track_gain_db"] = None
    out = server.player_signal_path()
    assert out["bit_perfect"] is True
    assert out["replaygain"]["active"] is False
    assert out["replaygain"]["tags_present"] is False


def test_external_output_breaks_bit_perfect(stub_player):
    """Remote receiver as the active sink — we can't claim bit-perfect
    about a pipeline we don't control."""
    import server
    stub_player._state["output_stream"]["external_output_active"] = True
    out = server.player_signal_path()
    assert out["bit_perfect"] is False
    assert out["output"]["external_output_active"] is True


# --- output stage detail ------------------------------------------


def test_output_includes_device_name_and_format(stub_player):
    import server
    out = server.player_signal_path()
    assert out["output"]["device_name"] == "RME ADI-2 Pro"
    assert out["output"]["sample_rate_hz"] == 96000
    assert out["output"]["bit_depth"] == 32  # int32 → 32-bit container
    assert out["output"]["channels"] == 2


def test_int16_output_reports_16_bit(stub_player):
    import server
    stub_player._state["output_stream"]["sd_dtype"] = "int16"
    out = server.player_signal_path()
    assert out["output"]["bit_depth"] == 16


def test_float32_output_reports_32_bit(stub_player):
    import server
    stub_player._state["output_stream"]["sd_dtype"] = "float32"
    out = server.player_signal_path()
    assert out["output"]["bit_depth"] == 32


# --- replaygain panel ---------------------------------------------


def test_replaygain_tags_present_flag(stub_player):
    import server
    stub_player._state["rg_track_gain_db"] = -2.0
    stub_player._state["rg_album_gain_db"] = None
    out = server.player_signal_path()
    assert out["replaygain"]["tags_present"] is True


def test_replaygain_tags_absent_flag(stub_player):
    import server
    stub_player._state["rg_track_gain_db"] = None
    stub_player._state["rg_album_gain_db"] = None
    out = server.player_signal_path()
    assert out["replaygain"]["tags_present"] is False
