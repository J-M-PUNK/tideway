"""Phase 2 of the AutoEQ work — parser + index + apply.

Test strategy:
- Parser: synthetic ParametricEQ.txt strings exercise every line
  shape (preamp, peaking, both shelves, OFF, comments, malformed).
- Index: a tmp-path fixture with a couple of profiles, exercising
  the directory walk, ID derivation, search, and lookup.
- Apply: a parsed profile compiles to an SOS matrix of the right
  shape, and the cascade response at characteristic frequencies
  matches what each band individually would contribute.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import sosfreqz  # type: ignore

from app.audio.autoeq.apply import profile_to_sos
from app.audio.autoeq.index import AutoEqIndex
from app.audio.autoeq.profiles import (
    AutoEqParseError,
    parse_profile_text,
)


SAMPLE_RATE = 48_000


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_minimal_profile():
    """Smallest legal profile: preamp + one peaking band. Parsed
    fields round-trip to the dataclass exactly."""
    text = """
    Preamp: -3.5 dB
    Filter 1: ON PK Fc 1000 Hz Gain 2.0 dB Q 1.41
    """
    p = parse_profile_text(
        text, profile_id="t/1", brand="Test", model="One", source="t"
    )
    assert p.preamp_db == -3.5
    assert len(p.bands) == 1
    band = p.bands[0]
    assert band.filter_type == "PK"
    assert band.freq_hz == 1000.0
    assert band.gain_db == 2.0
    assert band.q == 1.41


def test_parse_handles_all_three_filter_types():
    """A real AutoEQ profile mixes peaking + low/high shelves.
    All three must parse."""
    text = """
    Preamp: -6.5 dB
    Filter 1: ON LSC Fc 105 Hz Gain 6.5 dB Q 0.7
    Filter 2: ON PK Fc 200 Hz Gain -3.0 dB Q 1.41
    Filter 3: ON HSC Fc 8000 Hz Gain -2.0 dB Q 0.7
    """
    p = parse_profile_text(text)
    types = [b.filter_type for b in p.bands]
    assert types == ["LSC", "PK", "HSC"]


def test_parse_skips_off_filters_without_renumbering_bands():
    """`ON` / `OFF` is in the spec; OFF bands are dropped from
    the active set. The numbering on the line is metadata, not
    something we need to preserve."""
    text = """
    Preamp: 0 dB
    Filter 1: ON PK Fc 100 Hz Gain 1 dB Q 1
    Filter 2: OFF PK Fc 200 Hz Gain 99 dB Q 1
    Filter 3: ON PK Fc 400 Hz Gain 2 dB Q 1
    """
    p = parse_profile_text(text)
    assert [b.freq_hz for b in p.bands] == [100.0, 400.0]


def test_parse_skips_blank_lines_and_comments():
    """Real files have blank lines between sections; user-edited
    files might add comments. Both should be ignored."""
    text = """

    # this is a comment
    Preamp: 0 dB

    Filter 1: ON PK Fc 1000 Hz Gain 0 dB Q 1
    """
    p = parse_profile_text(text)
    assert p.preamp_db == 0.0
    assert len(p.bands) == 1


def test_parse_rejects_unknown_filter_type():
    """A typo'd type code (e.g. NOTCH) should fail loudly with a
    line number, not silently produce a flat profile."""
    text = "Filter 1: ON NOTCH Fc 100 Hz Gain 1 dB Q 1"
    with pytest.raises(AutoEqParseError, match="line 1.*unsupported filter type"):
        parse_profile_text(text)


def test_parse_rejects_garbled_line():
    """Anything that isn't preamp, filter, comment, or blank is an
    error. Catches typos like `Fitler` that would otherwise be
    silently dropped."""
    text = "this is not a valid line"
    with pytest.raises(AutoEqParseError, match="unrecognised line"):
        parse_profile_text(text)


def test_parse_accepts_equalizer_apo_format():
    """AutoEQ.app's "EqualizerAPO Parametric Eq" export prepends a
    Channel: header before the Filter / Preamp lines. Same parametric
    payload, just with the EqualizerAPO host's channel-selection
    semantics on top. We don't honour those semantics (Tideway always
    EQs all channels), but the file should import without error."""
    text = """\
Channel: all
Preamp: -3.5 dB
Filter 1: ON PK Fc 200 Hz Gain -3.0 dB Q 1.4
Filter 2: ON LSC Fc 105 Hz Gain 6.0 dB Q 0.7
"""
    p = parse_profile_text(text)
    assert p.preamp_db == -3.5
    assert len(p.bands) == 2


def test_parse_skips_other_equalizer_apo_directives():
    """Other Equalizer APO directives (Device:, Include:, Stage:,
    Eval:) also appear in real-world configs, especially when users
    edited the file by hand or imported via a more elaborate APO
    plugin. None affect the parametric content; skip them rather
    than rejecting otherwise-valid files."""
    text = """\
Device: Speakers
Channel: all
Stage: pre-mix
Preamp: -2.0 dB
Filter 1: ON PK Fc 1000 Hz Gain 2 dB Q 1.4
"""
    p = parse_profile_text(text)
    assert p.preamp_db == -2.0
    assert len(p.bands) == 1


def test_parse_rejects_graphic_eq_format_with_helpful_message():
    """A user who exported AutoEQ.app's "Graphic EQ" instead of a
    parametric format gets a `25 -3.0; 31 -3.0; ...` payload that
    isn't parametric. The error should name the wrong format so the
    user knows which export option to switch to."""
    text = "GraphicEQ: 25 -3.0; 31 -3.0; 40 -2.5; 50 -2.0"
    with pytest.raises(AutoEqParseError, match="Graphic EQ"):
        parse_profile_text(text)


def test_parse_rejects_empty_file_with_helpful_message():
    """A file with only headers / comments and no Filter or Preamp
    lines means the user grabbed something that isn't a parametric
    EQ at all. Tell them which format to pick instead of returning
    a silently-empty profile that would no-op the audio."""
    text = """\
# Some comment
Channel: all
"""
    with pytest.raises(AutoEqParseError, match="EqualizerAPO Parametric Eq"):
        parse_profile_text(text)


def test_parse_real_file_shape():
    """Smoke-test against a real AutoEQ-shaped ParametricEQ.txt — a
    snapshot of oratory1990's HD 600 PEQ as of AutoEQ master. We
    don't ship bundled profiles anymore, so this can't read off
    disk; the inline copy serves as a "real-world fixture" so
    parser regressions on actual production input get caught
    without depending on disk state."""
    text = (
        "Preamp: -6.3 dB\n"
        "Filter 1: ON LSC Fc 105 Hz Gain 6.5 dB Q 0.70\n"
        "Filter 2: ON PK Fc 125 Hz Gain -2.7 dB Q 0.55\n"
        "Filter 3: ON PK Fc 8445 Hz Gain 3.3 dB Q 1.61\n"
        "Filter 4: ON PK Fc 522 Hz Gain 0.7 dB Q 1.02\n"
        "Filter 5: ON PK Fc 1298 Hz Gain -1.2 dB Q 2.14\n"
        "Filter 6: ON HSC Fc 10000 Hz Gain -3.1 dB Q 0.70\n"
        "Filter 7: ON PK Fc 3158 Hz Gain -1.8 dB Q 3.67\n"
        "Filter 8: ON PK Fc 2166 Hz Gain 0.9 dB Q 3.32\n"
        "Filter 9: ON PK Fc 6639 Hz Gain 2.2 dB Q 5.82\n"
        "Filter 10: ON PK Fc 5433 Hz Gain -1.2 dB Q 5.70\n"
    )
    p = parse_profile_text(text)
    assert p.preamp_db < 0  # real profiles always need some headroom
    assert len(p.bands) >= 5
    for band in p.bands:
        assert band.freq_hz > 0
        assert band.q > 0


# ---------------------------------------------------------------------------
# Index — directory walk + search
# ---------------------------------------------------------------------------


def _write_profile(
    root: Path, source: str, brand_model: str, contents: str
) -> None:
    """Write a stub profile under the conventional layout."""
    target = root / source / brand_model
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{brand_model} ParametricEQ.txt").write_text(
        contents, encoding="utf-8"
    )


def test_index_loads_from_directory(tmp_path):
    """Two profiles, both load, IDs derived from the path."""
    body = "Preamp: -1 dB\nFilter 1: ON PK Fc 1000 Hz Gain 1 dB Q 1\n"
    _write_profile(tmp_path, "oratory1990", "Sennheiser HD 600", body)
    _write_profile(tmp_path, "oratory1990", "Sony WH-1000XM4", body)

    idx = AutoEqIndex()
    loaded = idx.load_directory(tmp_path)

    assert loaded == 2
    assert idx.count() == 2
    assert idx.get("oratory1990/Sennheiser HD 600") is not None
    assert idx.get("oratory1990/Sony WH-1000XM4") is not None


def test_index_skips_malformed_files_without_killing_the_load(tmp_path):
    """One bad file shouldn't take the whole load down — log and
    move on. AutoEQ has shipped occasional bad files historically."""
    good = "Preamp: 0 dB\nFilter 1: ON PK Fc 1000 Hz Gain 1 dB Q 1\n"
    _write_profile(tmp_path, "oratory1990", "Good Headphone", good)
    _write_profile(tmp_path, "oratory1990", "Broken", "this is garbage\n")

    idx = AutoEqIndex()
    loaded = idx.load_directory(tmp_path)

    assert loaded == 1
    assert idx.get("oratory1990/Good Headphone") is not None
    assert idx.get("oratory1990/Broken") is None


def test_index_search_finds_by_substring(tmp_path):
    """User types `hd 600`, gets the Sennheiser back. The match
    scope is brand + model concatenated, case-insensitive."""
    body = "Preamp: 0 dB\nFilter 1: ON PK Fc 1000 Hz Gain 1 dB Q 1\n"
    _write_profile(tmp_path, "oratory1990", "Sennheiser HD 600", body)
    _write_profile(tmp_path, "oratory1990", "Sony WH-1000XM4", body)

    idx = AutoEqIndex()
    idx.load_directory(tmp_path)

    results = idx.search("hd 600", limit=5)
    assert len(results) >= 1
    assert any("HD 600" in r.model for r in results)


def test_index_search_empty_query_returns_first_n_alphabetical(tmp_path):
    """Empty search seeds the picker — useful for first paint
    before the user types anything. Order is alphabetical by
    brand + model concatenated so it's deterministic."""
    body = "Preamp: 0 dB\nFilter 1: ON PK Fc 1000 Hz Gain 1 dB Q 1\n"
    _write_profile(tmp_path, "oratory1990", "Zebra Phones", body)
    _write_profile(tmp_path, "oratory1990", "Apple Headphones", body)

    idx = AutoEqIndex()
    idx.load_directory(tmp_path)

    results = idx.search("", limit=5)
    assert len(results) == 2
    assert results[0].brand == "Apple"
    assert results[1].brand == "Zebra"


def test_index_load_directory_handles_missing_root(tmp_path):
    """Calling load_directory on a non-existent path should warn
    and return 0, not raise. PyInstaller bundles can race the
    setup and we'd rather degrade than crash startup."""
    idx = AutoEqIndex()
    loaded = idx.load_directory(tmp_path / "does-not-exist")
    assert loaded == 0
    assert idx.count() == 0


# ---------------------------------------------------------------------------
# Apply — profile → SOS shape + cascade response
# ---------------------------------------------------------------------------


def test_profile_to_sos_returns_correct_shape():
    text = """
    Preamp: -1 dB
    Filter 1: ON LSC Fc 100 Hz Gain 4 dB Q 0.7
    Filter 2: ON PK Fc 1000 Hz Gain 2 dB Q 1.0
    Filter 3: ON HSC Fc 8000 Hz Gain -3 dB Q 0.7
    """
    profile = parse_profile_text(text)
    sos = profile_to_sos(profile, SAMPLE_RATE)
    assert sos.shape == (3, 6)
    assert sos.dtype == np.float32


def test_profile_to_sos_empty_for_no_band_profile():
    """A profile with only a preamp (no bands) compiles to an
    empty SOS. Caller decides whether that's a bypass case."""
    profile = parse_profile_text("Preamp: -1 dB")
    sos = profile_to_sos(profile, SAMPLE_RATE)
    assert sos.shape == (0, 6)


def test_cascade_with_flat_tilt_matches_profile_to_sos():
    """A TiltConfig with all zeros should produce the same SOS as
    `profile_to_sos`. Phase 5 guarantee: zero-tilt audio path is
    identical to Phase 2-4 behavior, so users who never touch the
    tilt sliders aren't running through extra biquads."""
    from app.audio.autoeq.apply import TiltConfig, cascade_with_tilt

    text = """
    Preamp: -1 dB
    Filter 1: ON LSC Fc 100 Hz Gain 4 dB Q 0.7
    Filter 2: ON PK Fc 1000 Hz Gain 2 dB Q 1.0
    """
    profile = parse_profile_text(text)
    sos_old = profile_to_sos(profile, SAMPLE_RATE)
    sos_new, preamp = cascade_with_tilt(profile, SAMPLE_RATE, TiltConfig())
    np.testing.assert_array_equal(sos_old, sos_new)
    assert preamp == pytest.approx(profile.preamp_db)


def test_cascade_with_tilt_appends_shelves_when_nonzero():
    """A non-flat tilt adds extra biquads to the cascade. Shape
    grows by 1 per nonzero shelf (preamp offset doesn't add a
    biquad — it only adjusts the master preamp)."""
    from app.audio.autoeq.apply import TiltConfig, cascade_with_tilt

    text = """
    Preamp: -1 dB
    Filter 1: ON PK Fc 1000 Hz Gain 1 dB Q 1.0
    """
    profile = parse_profile_text(text)
    # Bass only.
    sos, preamp = cascade_with_tilt(
        profile, SAMPLE_RATE, TiltConfig(bass_db=4.0)
    )
    assert sos.shape == (2, 6)
    assert preamp == pytest.approx(profile.preamp_db)
    # Bass + treble.
    sos, _ = cascade_with_tilt(
        profile, SAMPLE_RATE, TiltConfig(bass_db=4.0, treble_db=-3.0)
    )
    assert sos.shape == (3, 6)
    # Preamp offset rolled into total — no shelf biquads added.
    sos, preamp = cascade_with_tilt(
        profile, SAMPLE_RATE, TiltConfig(preamp_offset_db=-2.0)
    )
    assert sos.shape == (1, 6)
    assert preamp == pytest.approx(profile.preamp_db + (-2.0))


def test_cascade_tilt_response_settles_to_expected_db():
    """Apply a +6 dB bass tilt over a flat-ish profile and verify
    the cascade's response below the shelf corner is ~+6 dB."""
    from app.audio.autoeq.apply import TiltConfig, cascade_with_tilt

    text = "Preamp: 0 dB\nFilter 1: ON PK Fc 1000 Hz Gain 0 dB Q 1.0"
    profile = parse_profile_text(text)
    sos, _ = cascade_with_tilt(
        profile, SAMPLE_RATE, TiltConfig(bass_db=6.0)
    )

    def cascade_db(freq_hz: float) -> float:
        _, h = sosfreqz(sos, worN=np.array([freq_hz]), fs=SAMPLE_RATE)
        return 20.0 * math.log10(max(abs(h[0]), 1e-12))

    # Well below the 80 Hz tilt corner — bass shelf dominates.
    assert abs(cascade_db(20.0) - 6.0) < 0.5


def test_profile_cascade_response_at_characteristic_frequencies():
    """Build a tiny profile (LSC + HSC) and verify the cascade's
    magnitude response settles to the expected dB values where
    each shelf dominates. Catches mistakes in the dispatch /
    cascade builder that would land bands at the wrong frequency
    or apply the wrong type."""
    text = """
    Preamp: 0 dB
    Filter 1: ON LSC Fc 100 Hz Gain 4 dB Q 0.7
    Filter 2: ON HSC Fc 8000 Hz Gain -3 dB Q 0.7
    """
    profile = parse_profile_text(text)
    sos = profile_to_sos(profile, SAMPLE_RATE)

    def cascade_db(freq_hz: float) -> float:
        _, h = sosfreqz(sos, worN=np.array([freq_hz]), fs=SAMPLE_RATE)
        return 20.0 * math.log10(max(abs(h[0]), 1e-12))

    # Deep below the LSC corner — the +4 dB shelf dominates.
    assert abs(cascade_db(20.0) - 4.0) < 0.5
    # Above the HSC corner — the -3 dB shelf dominates.
    assert abs(cascade_db(20000.0) - (-3.0)) < 0.5
