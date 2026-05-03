"""Phase 7 — catalog updater logic.

Network code (manifest fetch, raw downloads) isn't exercised
here — those require either real GitHub access or a heavy mock
layer that doesn't catch much. The decision-heavy bits (path
parsing, diff against disk, cache layout derivation) are pure
and worth pinning.
"""
from __future__ import annotations

from pathlib import Path

from app.audio.autoeq.updater import (
    CatalogManifest,
    _cache_target_for_path,
    _profile_id_from_path,
    diff_manifest_against_disk,
)


# ---------------------------------------------------------------------------
# Path → profile_id
# ---------------------------------------------------------------------------


def test_profile_id_strips_results_prefix_and_kind_tier():
    """AutoEQ's manifest paths have a `results/<source>/<kind>/...`
    layout. The id we want is `<source>/<headphone>` so it matches
    what the bundled-data index produces."""
    pid = _profile_id_from_path(
        "results/oratory1990/over-ear/Sennheiser HD 600/"
        "Sennheiser HD 600 ParametricEQ.txt"
    )
    assert pid == "oratory1990/Sennheiser HD 600"


def test_profile_id_handles_in_ear_kind():
    pid = _profile_id_from_path(
        "results/oratory1990/in-ear/Etymotic ER4SR/"
        "Etymotic ER4SR ParametricEQ.txt"
    )
    assert pid == "oratory1990/Etymotic ER4SR"


def test_profile_id_falls_back_for_unexpected_layout():
    """Defensive: an unexpected manifest path doesn't crash; we
    just return the raw path. Caller treats unknown ids as
    'skip with a log line.'"""
    pid = _profile_id_from_path("results/something.txt")
    assert pid == "results/something.txt"


# ---------------------------------------------------------------------------
# Cache-target derivation
# ---------------------------------------------------------------------------


def test_cache_target_layout_matches_bundled():
    """Downloaded files land at
    cache_root/<source>/<brand_model>/<filename> — same shape
    the bundled data uses, so the index walks both with one
    layout rule."""
    peq, csv = _cache_target_for_path(
        "results/oratory1990/over-ear/Sony WH-1000XM4/"
        "Sony WH-1000XM4 ParametricEQ.txt"
    )
    assert peq.parent.name == "Sony WH-1000XM4"
    assert peq.parent.parent.name == "oratory1990"
    assert peq.name == "Sony WH-1000XM4 ParametricEQ.txt"
    # CSV sibling.
    assert csv.parent == peq.parent
    assert csv.name == "Sony WH-1000XM4.csv"


# ---------------------------------------------------------------------------
# Manifest diff against disk
# ---------------------------------------------------------------------------


def _write_stub_profile(root: Path, source: str, brand_model: str) -> None:
    """Write a minimal ParametricEQ.txt at the layout the index
    expects so `diff_manifest_against_disk` can find it."""
    target = root / source / brand_model
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{brand_model} ParametricEQ.txt").write_text(
        "Preamp: 0 dB\nFilter 1: ON PK Fc 1000 Hz Gain 0 dB Q 1\n",
        encoding="utf-8",
    )


def test_diff_marks_existing_as_already_and_missing_as_missing(tmp_path):
    bundled = tmp_path / "bundled"
    _write_stub_profile(bundled, "oratory1990", "Sennheiser HD 600")
    # Note: cache_dir is the user_data_dir cache, which we don't
    # control here — diff falls back to bundled-only when the
    # cache dir doesn't exist (typical fresh-install state).

    manifest = CatalogManifest(
        profile_paths=[
            "results/oratory1990/over-ear/Sennheiser HD 600/"
            "Sennheiser HD 600 ParametricEQ.txt",
            "results/oratory1990/over-ear/Sony WH-1000XM4/"
            "Sony WH-1000XM4 ParametricEQ.txt",
        ],
        fetched_at=0,
    )
    already, missing = diff_manifest_against_disk(manifest, bundled)
    assert already == ["oratory1990/Sennheiser HD 600"]
    assert missing == ["oratory1990/Sony WH-1000XM4"]


def test_diff_dedupes_repeated_manifest_entries(tmp_path):
    """AutoEQ's manifest occasionally has duplicate paths (renames,
    historical mirrors). The diff should not produce duplicate
    ids — caller would otherwise download the same profile twice."""
    bundled = tmp_path / "bundled"
    bundled.mkdir()

    same_path = (
        "results/oratory1990/over-ear/Some Headphone/"
        "Some Headphone ParametricEQ.txt"
    )
    manifest = CatalogManifest(
        profile_paths=[same_path, same_path],
        fetched_at=0,
    )
    already, missing = diff_manifest_against_disk(manifest, bundled)
    assert already == []
    assert missing == ["oratory1990/Some Headphone"]


def test_diff_handles_missing_bundled_root(tmp_path):
    """A fresh-install or test machine where the bundled root
    doesn't exist yet should still diff cleanly — everything is
    'missing,' nothing 'already.'"""
    nonexistent = tmp_path / "does-not-exist"
    manifest = CatalogManifest(
        profile_paths=[
            "results/oratory1990/over-ear/X Y/X Y ParametricEQ.txt",
        ],
        fetched_at=0,
    )
    already, missing = diff_manifest_against_disk(manifest, nonexistent)
    assert already == []
    assert missing == ["oratory1990/X Y"]
