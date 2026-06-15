"""Tests for the manual parametric EQ — the DSP builder, band
validation, legacy-curve migration, the response-curve helper, and
the `/api/player/eq` endpoints.

The biquad math itself is pinned in test_eq_shelf_filters.py; this
module covers the parametric *layer* on top: turning a user-editable
band list into an SOS cascade, rejecting out-of-range bands, and the
wire shape of the endpoints.
"""
from __future__ import annotations

import copy
import math

import numpy as np
import pytest

from app.audio.eq import (
    BAND_FREQUENCIES_HZ,
    BAND_Q,
    HIGH_SHELF,
    LOW_SHELF,
    MANUAL_GAIN_ABS_MAX_DB,
    MANUAL_MAX_BANDS,
    PEAKING,
    Equalizer,
    ParametricBand,
    _high_shelf_biquad,
    _low_shelf_biquad,
    _peaking_biquad,
    build_parametric_sos,
    default_parametric_bands,
    graphic_gains_to_parametric,
    manual_eq_alters_audio,
    manual_eq_config,
    parametric_band_from_dict,
    parametric_preset,
    parametric_presets,
    parse_parametric_bands,
)

SAMPLE_RATE = 48_000


# ---------------------------------------------------------------------------
# build_parametric_sos
# ---------------------------------------------------------------------------


def test_build_sos_row_count_matches_enabled_bands():
    bands = [
        ParametricBand(PEAKING, 100.0, 3.0, 1.0),
        ParametricBand(LOW_SHELF, 80.0, 2.0, 0.7, enabled=False),
        ParametricBand(HIGH_SHELF, 8000.0, -2.0, 0.7),
    ]
    sos = build_parametric_sos(bands, SAMPLE_RATE)
    # Disabled middle band is dropped → 2 sections.
    assert sos.shape == (2, 6)


def test_build_sos_empty_and_all_disabled_are_bypass():
    assert build_parametric_sos([], SAMPLE_RATE).shape == (0, 6)
    disabled = [ParametricBand(PEAKING, 1000.0, 6.0, 1.0, enabled=False)]
    assert build_parametric_sos(disabled, SAMPLE_RATE).shape == (0, 6)


def test_build_sos_skips_flat_bands_for_bit_perfect():
    # A flat (0 dB) band is a unity biquad — dropped so a flat layout
    # stays bit-perfect rather than running pointless sections.
    flat = [
        ParametricBand(LOW_SHELF, 105.0, 0.0, 0.7),
        ParametricBand(PEAKING, 1000.0, 0.0, 1.0),
    ]
    assert build_parametric_sos(flat, SAMPLE_RATE).shape == (0, 6)
    # One band shaped → exactly one section.
    flat[1].gain_db = 3.0
    assert build_parametric_sos(flat, SAMPLE_RATE).shape == (1, 6)


def test_default_bands_layout_has_shelves_on_the_ends():
    bands = default_parametric_bands()
    assert len(bands) == 6
    assert bands[0].filter_type == LOW_SHELF
    assert bands[-1].filter_type == HIGH_SHELF
    assert all(b.filter_type == PEAKING for b in bands[1:-1])
    # All flat → seeding the layout is bit-perfect.
    assert all(b.gain_db == 0.0 for b in bands)
    assert not manual_eq_alters_audio(bands)


def test_manual_eq_alters_audio_detects_nontrivial_gain():
    assert manual_eq_alters_audio([]) is False
    assert manual_eq_alters_audio(default_parametric_bands()) is False
    assert (
        manual_eq_alters_audio(
            [{"type": "PK", "freq": 1000, "gain": 2.0, "q": 1, "enabled": True}]
        )
        is True
    )
    # A disabled non-flat band doesn't count.
    assert (
        manual_eq_alters_audio(
            [{"type": "PK", "freq": 1000, "gain": 6.0, "q": 1, "enabled": False}]
        )
        is False
    )


def test_manual_eq_alters_audio_counts_preamp():
    # A non-zero preamp alters the audio even when every band is
    # flat — the engine installs a preamp-only stage for it, so the
    # signal-path badge must not claim bit-perfect.
    flat = default_parametric_bands()
    assert manual_eq_alters_audio(flat, preamp_db=-6.0) is True
    assert manual_eq_alters_audio(flat, preamp_db=0.0) is False
    assert manual_eq_alters_audio([], preamp_db=None) is False


def test_single_peaking_band_matches_biquad_helper():
    band = ParametricBand(PEAKING, 1000.0, 4.0, 1.4)
    sos = build_parametric_sos([band], SAMPLE_RATE)
    expected = _peaking_biquad(1000.0, 4.0, 1.4, SAMPLE_RATE)
    np.testing.assert_allclose(sos[0], expected, rtol=1e-6)


def test_shelf_band_dispatches_to_shelf_helper():
    band = ParametricBand(LOW_SHELF, 120.0, 5.0, 0.7)
    sos = build_parametric_sos([band], SAMPLE_RATE)
    expected = _low_shelf_biquad(120.0, 5.0, 0.7, SAMPLE_RATE)
    np.testing.assert_allclose(sos[0], expected, rtol=1e-6)


# ---------------------------------------------------------------------------
# Band validation
# ---------------------------------------------------------------------------


def test_band_from_dict_accepts_valid_and_defaults_enabled():
    band = parametric_band_from_dict(
        {"type": "pk", "freq": 1000, "gain": -3.0, "q": 1.2}
    )
    assert band.filter_type == PEAKING  # upcased
    assert band.enabled is True  # defaulted
    assert band.freq_hz == 1000.0


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "XX", "freq": 1000, "gain": 0, "q": 1},  # unknown type
        {"type": "PK", "freq": 5, "gain": 0, "q": 1},  # freq below 20 Hz
        {"type": "PK", "freq": 30000, "gain": 0, "q": 1},  # freq above 20 kHz
        {"type": "PK", "freq": 1000, "gain": 99, "q": 1},  # gain out of range
        {"type": "PK", "freq": 1000, "gain": 0, "q": 0},  # q must be > 0
        {"type": "PK", "freq": 1000, "gain": 0, "q": 99},  # q too high
        {"type": "PK", "freq": 1000, "gain": 0},  # missing q
    ],
)
def test_band_from_dict_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        parametric_band_from_dict(bad)


def test_band_from_dict_rejects_nan_gain():
    # abs(NaN) > MAX is False, so a naive `>` check would accept NaN
    # and poison persisted settings; the validator must reject it.
    with pytest.raises(ValueError):
        parametric_band_from_dict(
            {"type": "PK", "freq": 1000, "gain": float("nan"), "q": 1}
        )


def test_parse_bands_enforces_count_cap():
    one = {"type": "PK", "freq": 1000, "gain": 1, "q": 1}
    over = [one] * (MANUAL_MAX_BANDS + 1)
    with pytest.raises(ValueError):
        parse_parametric_bands(over)
    # Exactly the cap is fine.
    assert len(parse_parametric_bands([one] * MANUAL_MAX_BANDS)) == MANUAL_MAX_BANDS


# ---------------------------------------------------------------------------
# Legacy graphic-curve migration
# ---------------------------------------------------------------------------


def test_graphic_to_parametric_drops_flat_and_maps_nonflat():
    gains = [0.0] * len(BAND_FREQUENCIES_HZ)
    gains[0] = 6.0  # boost the lowest band only
    gains[4] = -3.0
    bands = graphic_gains_to_parametric(gains)
    assert len(bands) == 2
    assert all(b.filter_type == PEAKING for b in bands)
    assert all(b.q == BAND_Q for b in bands)
    assert bands[0].freq_hz == BAND_FREQUENCIES_HZ[0]
    assert bands[0].gain_db == 6.0
    assert bands[1].freq_hz == BAND_FREQUENCIES_HZ[4]


def test_graphic_to_parametric_all_flat_is_empty():
    assert graphic_gains_to_parametric([0.0] * len(BAND_FREQUENCIES_HZ)) == []


# The manual response curve is computed client-side now (the editor
# draws it live during a drag); its math is pinned in
# web/src/lib/eqCurve.test.ts.


# ---------------------------------------------------------------------------
# Engine behaviors
# ---------------------------------------------------------------------------


def test_preamp_only_curve_still_attenuates():
    """Flat bands + non-zero preamp must install a preamp-only stage
    — the old engine cleared everything (preamp included) when the
    cascade compiled empty, silently ignoring the user's preamp."""
    eq = Equalizer(sample_rate=SAMPLE_RATE, channels=2)
    sos = build_parametric_sos(default_parametric_bands(), SAMPLE_RATE)
    assert sos.shape == (0, 6)  # all-flat layout → no biquads
    eq.set_sos(sos, preamp_db=-6.0)
    assert eq.is_active()
    samples = np.ones((64, 2), dtype=np.float32)
    eq.apply(samples)
    expected = 10.0 ** (-6.0 / 20.0)
    np.testing.assert_allclose(samples, expected, rtol=1e-5)

    # Unity preamp + empty cascade is a true bypass.
    eq.set_sos(sos, preamp_db=None)
    assert not eq.is_active()


@pytest.mark.parametrize(
    "builder", [_low_shelf_biquad, _high_shelf_biquad]
)
def test_shelf_biquads_stay_stable_at_extreme_gain_and_q(builder):
    """Extreme gain/slope combos drive the RBJ shelf alpha's sqrt
    argument negative; with a floor of exactly 0 the poles land ON
    the unit circle and the filter rings forever. The positive floor
    must keep every pole strictly inside."""
    for gain_db, q in [(23.0, 2.0), (24.0, 10.0), (-24.0, 10.0), (9.5, 10.0)]:
        row = builder(1000.0, gain_db, q, SAMPLE_RATE)
        # Denominator polynomial is [1, a1, a2] (a0 normalised to 1).
        poles = np.roots([1.0, float(row[4]), float(row[5])])
        assert np.all(np.abs(poles) < 1.0), (
            f"unstable shelf: gain={gain_db} q={q} |poles|={np.abs(poles)}"
        )


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def test_parametric_presets_serialize_and_flat_preset_is_empty():
    presets = parametric_presets()
    by_name = {p["name"]: p for p in presets}
    assert by_name["Flat"]["bands"] == []  # all-flat preset → no bands
    bass = by_name["Bass Boost"]["bands"]
    assert bass and all(b["type"] == PEAKING for b in bass)


def test_parametric_preset_unknown_index_is_empty():
    assert parametric_preset(9999) == []


def test_manual_config_exposes_types_and_ranges():
    cfg = manual_eq_config()
    assert set(cfg["filter_types"]) == {PEAKING, LOW_SHELF, HIGH_SHELF}
    assert cfg["freq_min"] < cfg["freq_max"]
    assert cfg["max_bands"] == MANUAL_MAX_BANDS


# ---------------------------------------------------------------------------
# Settings migration
# ---------------------------------------------------------------------------


def test_settings_migration_converts_legacy_curve_and_consumes_it():
    from app.settings import Settings, _migrate_eq_to_parametric

    s = Settings()
    s.eq_bands = [0.0] * len(BAND_FREQUENCIES_HZ)
    s.eq_bands[0] = 4.0
    changed = _migrate_eq_to_parametric(s)
    assert changed is True
    assert len(s.eq_parametric_bands) == 1
    assert s.eq_parametric_bands[0]["type"] == PEAKING
    assert s.eq_parametric_bands[0]["gain"] == 4.0
    # The legacy curve is consumed — otherwise emptying the
    # parametric list later would resurrect it on the next launch.
    assert s.eq_bands == []
    # And therefore a user who empties their parametric bands stays
    # empty across the next load.
    s.eq_parametric_bands = []
    assert _migrate_eq_to_parametric(s) is False
    assert s.eq_parametric_bands == []


def test_settings_migration_idempotent_and_consumes_flat():
    from app.settings import Settings, _migrate_eq_to_parametric

    # Already has parametric bands → no-op.
    s = Settings()
    s.eq_bands = [4.0] + [0.0] * (len(BAND_FREQUENCIES_HZ) - 1)
    s.eq_parametric_bands = [
        {"type": "PK", "freq": 100.0, "gain": 1.0, "q": 1.0, "enabled": True}
    ]
    assert _migrate_eq_to_parametric(s) is False

    # Flat legacy curve → no parametric bands, but the legacy list is
    # still consumed so the check doesn't re-run every load.
    flat = Settings()
    flat.eq_bands = [0.0] * len(BAND_FREQUENCIES_HZ)
    assert _migrate_eq_to_parametric(flat) is True
    assert flat.eq_parametric_bands == []
    assert flat.eq_bands == []


def test_settings_migration_clamps_out_of_range_legacy_gains():
    # The legacy POST accepted arbitrary floats; an unclamped
    # migrated gain would fail validation on every subsequent apply
    # and permanently wedge the user's EQ.
    from app.settings import Settings, _migrate_eq_to_parametric

    s = Settings()
    s.eq_bands = [30.0] + [0.0] * (len(BAND_FREQUENCIES_HZ) - 1)
    assert _migrate_eq_to_parametric(s) is True
    assert s.eq_parametric_bands[0]["gain"] == MANUAL_GAIN_ABS_MAX_DB
    # The migrated band passes the validator every apply path uses.
    parse_parametric_bands(s.eq_parametric_bands)


# ---------------------------------------------------------------------------
# Endpoints — called directly as plain functions (no TestClient/orjson).
# ---------------------------------------------------------------------------


@pytest.fixture
def eq_server(monkeypatch, tmp_path):
    """server module with local-access gated off, settings persisted
    to a tmp file, and `_native_player` replaced by a stub that just
    records the last applied bands."""
    import app.settings as _settings_mod
    import server

    monkeypatch.setattr(server, "_require_local_access", lambda: None)
    monkeypatch.setattr(
        _settings_mod, "SETTINGS_FILE", tmp_path / "settings.json"
    )

    class _StubPlayer:
        def __init__(self):
            self.applied = None
            self.applied_preamp = "unset"

        def apply_equalizer(self, bands, preamp=None):
            self.applied = bands
            self.applied_preamp = preamp

        def apply_equalizer_preset(self, idx):
            from app.audio.eq import parametric_preset

            return [b.to_dict() for b in parametric_preset(idx)]

    stub = _StubPlayer()
    monkeypatch.setattr(server, "_native_player", lambda: stub)

    original = copy.deepcopy(server.settings)
    server.settings.eq_mode = "manual"
    server.settings.eq_enabled = False
    server.settings.eq_parametric_bands = []
    server.settings.eq_preamp = None
    yield server, stub
    # Restore the module-level settings object's contents.
    for f in original.__dataclass_fields__:
        setattr(server.settings, f, getattr(original, f))


def test_get_eq_returns_config_presets_and_defaults(eq_server):
    server, _ = eq_server
    out = server.player_eq_state()
    assert "config" in out and "presets" in out
    assert out["bands"] == []
    assert set(out["config"]["filter_types"]) == {PEAKING, LOW_SHELF, HIGH_SHELF}
    # Default seed layout: six bands, shelves on the ends.
    assert len(out["default_bands"]) == 6
    assert out["default_bands"][0]["type"] == LOW_SHELF
    assert out["default_bands"][-1]["type"] == HIGH_SHELF


def test_post_eq_persists_and_applies_when_enabled(eq_server):
    server, stub = eq_server
    server.settings.eq_enabled = True
    req = server._PlayerEqRequest(
        bands=[
            server._ParametricBandModel(
                type="PK", freq=1000.0, gain=4.0, q=1.2
            )
        ],
        preamp=-2.0,
    )
    out = server.player_eq_set(req)
    assert out["ok"] is True
    assert server.settings.eq_parametric_bands[0]["freq"] == 1000.0
    # Enabled → pushed to the engine.
    assert stub.applied is not None and len(stub.applied) == 1
    assert stub.applied_preamp == -2.0


def test_post_eq_disabled_does_not_apply(eq_server):
    server, stub = eq_server
    server.settings.eq_enabled = False
    req = server._PlayerEqRequest(
        bands=[server._ParametricBandModel(type="PK", freq=500.0, gain=2.0, q=1.0)],
        preamp=None,
    )
    server.player_eq_set(req)
    # Persisted but not applied (EQ master gate off).
    assert server.settings.eq_parametric_bands
    assert stub.applied is None


def test_post_eq_rejects_bad_band_with_400(eq_server):
    from fastapi import HTTPException

    server, _ = eq_server
    req = server._PlayerEqRequest(
        bands=[server._ParametricBandModel(type="PK", freq=1000.0, gain=0.0, q=0.0)],
        preamp=None,
    )
    with pytest.raises(HTTPException) as exc:
        server.player_eq_set(req)
    assert exc.value.status_code == 400
