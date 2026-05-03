"""Frequency-response computation for the Phase 6 graph.

Returns three log-spaced curves at a chosen number of points:

  - `raw_db`      — the headphone's measured frequency response.
  - `target_db`   — the target curve AutoEQ aimed at.
  - `post_eq_db`  — predicted response after applying the user's
                    active cascade (profile bands + tilt shelves).

Inputs:
  - The active `AutoEqProfile` (Phase 2).
  - The user's active `TiltConfig` (Phase 5).
  - The headphone's raw measurement CSV (vendored alongside the
    ParametricEQ.txt files in `data/results/...`).
  - A target sample rate to evaluate the cascade at — picks the
    player's current rate so the graph reflects the audio path
    the user is actually hearing.

Computation cost: parsing the ~3,000-point CSV + interpolating
to ~512 log-spaced points + a single `scipy.signal.sosfreqz`
call. All sub-millisecond. Endpoint is safe to call from the
tilt-slider's onChange handler at slider-drag rates.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from scipy.signal import sosfreqz  # type: ignore

from .apply import TiltConfig, cascade_with_tilt
from .profiles import AutoEqProfile
from .updater import cache_dir, fetch_csv_for_profile_async

log = logging.getLogger(__name__)


@dataclass
class FrequencyResponse:
    """Bundle of arrays the API returns to the frontend graph.

    All four arrays have identical length. `raw_db` and
    `target_db` are None when the headphone's CSV isn't
    available — Phase 7's update channel will populate more.
    """

    frequencies_hz: list[float]
    raw_db: Optional[list[float]]
    target_db: Optional[list[float]]
    post_eq_db: list[float]


def log_frequency_grid(
    points: int = 512,
    f_min: float = 20.0,
    f_max: float = 20_000.0,
) -> np.ndarray:
    """Geometric series from `f_min` to `f_max` — the natural
    spacing for an audio frequency-response chart."""
    return np.logspace(np.log10(f_min), np.log10(f_max), int(points))


def _measurement_csv_path(profile: AutoEqProfile, data_root: Path) -> Optional[Path]:
    """Look for the headphone's CSV next to its ParametricEQ.txt
    in EITHER the bundled data dir or the cache dir (Phase 7's
    download destination). AutoEQ ships CSVs as
    `<Brand> <Model>.csv`.

    Searching both roots — bundled first, cache as fallback —
    means a profile downloaded via the catalog updater renders
    its FR graph as soon as `fetch_csv_for_profile` lands the
    sibling file."""
    if not profile.brand or not profile.model:
        return None
    headphone_dir = f"{profile.brand} {profile.model}"
    csv_name = f"{profile.brand} {profile.model}.csv"
    for root in (data_root, cache_dir()):
        candidate = root / profile.source / headphone_dir / csv_name
        if candidate.exists():
            return candidate
    return None


_REQUIRED_CSV_COLUMNS = ("frequency", "raw", "target")


def _read_measurement(
    path: Path,
) -> Optional[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Parse an AutoEQ measurement CSV. Required columns are
    `frequency`, `raw`, and `target` — all three must be present
    for the graph to render the three-curve overlay.

    Strict on column presence (returns None when a column is
    missing rather than silently producing zeros). Caller
    handles None by degrading the graph to post-EQ only.

    Strict-on-columns is a deploy-PR cleanup of the earlier
    inconsistent behavior (frequency was strict, raw/target were
    silent fallbacks); silent zero-fill for a missing column
    rendered as a flat line that looked like a "real" measurement
    and was actively misleading.
    """
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            field_names = reader.fieldnames or []
            for required in _REQUIRED_CSV_COLUMNS:
                if required not in field_names:
                    return None
            freqs: list[float] = []
            raws: list[float] = []
            targets: list[float] = []
            for row in reader:
                try:
                    freqs.append(float(row["frequency"]))
                    raws.append(float(row["raw"]))
                    targets.append(float(row["target"]))
                except (ValueError, KeyError):
                    # Skip a malformed row but keep parsing — a
                    # missing column was caught by the header
                    # check above; this path is for non-numeric
                    # values inside an otherwise-valid row.
                    continue
        if not freqs:
            return None
        return np.asarray(freqs), np.asarray(raws), np.asarray(targets)
    except (OSError, csv.Error) as exc:
        # File missing, permissions denied, or malformed CSV
        # framing past what the per-row except catches. Anything
        # else (e.g. a bug inside this function) should propagate
        # so the test suite catches it instead of silently rendering
        # an empty graph.
        log.debug("autoeq measurement read failed for %s: %s", path, exc)
        return None


def compute_response(
    profile: Optional[AutoEqProfile],
    tilt: TiltConfig,
    sample_rate: int,
    data_root: Path,
    points: int = 512,
) -> FrequencyResponse:
    """Build the three-curve response payload for the graph.

    When `profile` is None — e.g. user is in profile mode but
    hasn't picked one yet — `post_eq_db` is all zeros (flat,
    unity-gain pass-through), `raw_db` and `target_db` are None.
    """
    grid = log_frequency_grid(points=points)
    freqs_list = [float(f) for f in grid]

    if profile is None:
        return FrequencyResponse(
            frequencies_hz=freqs_list,
            raw_db=None,
            target_db=None,
            post_eq_db=[0.0] * len(freqs_list),
        )

    # Cascade response in dB at each grid point.
    sos, total_preamp_db = cascade_with_tilt(profile, sample_rate, tilt)
    if sos.size == 0:
        cascade_db = np.zeros_like(grid)
    else:
        _, h = sosfreqz(sos, worN=grid, fs=sample_rate)
        # Add the master preamp (linear scalar applied once before
        # the biquads). Magnitudes multiply → dB add.
        magnitude = np.abs(h)
        # Avoid log(0) for any band that lands at the Nyquist edge.
        cascade_db = 20.0 * np.log10(np.maximum(magnitude, 1e-12))
        cascade_db = cascade_db + total_preamp_db

    # Raw + target — interpolate onto the same grid if available.
    csv_path = _measurement_csv_path(profile, data_root)
    if csv_path is None:
        # CSV missing — kick off a best-effort lazy fetch in the
        # background. If the user has run "Check for updates"
        # this session, the manifest cache has the AutoEQ repo
        # path and the fetcher can grab the CSV. By the time the
        # graph debounces another response request (~80ms after
        # the next tilt-slider drag), the CSV is on disk and the
        # graph upgrades to the full three-curve view.
        fetch_csv_for_profile_async(profile.profile_id)
        raw_db_list: Optional[list[float]] = None
        target_db_list: Optional[list[float]] = None
        post_eq = cascade_db  # No raw to add to → post-EQ is just
        # the cascade. Caller can label this as "EQ response" when
        # raw is missing.
    else:
        parsed = _read_measurement(csv_path)
        if parsed is None:
            raw_db_list = None
            target_db_list = None
            post_eq = cascade_db
        else:
            csv_f, csv_raw, csv_target = parsed
            # `np.interp` is linear; we interpolate in log-frequency
            # space so the curve shape on the chart's log-x axis is
            # the right interpolation.
            log_f_grid = np.log10(grid)
            log_f_csv = np.log10(np.maximum(csv_f, 1e-9))
            raw_interp = np.interp(log_f_grid, log_f_csv, csv_raw)
            target_interp = np.interp(log_f_grid, log_f_csv, csv_target)
            raw_db_list = [float(v) for v in raw_interp]
            target_db_list = [float(v) for v in target_interp]
            post_eq = raw_interp + cascade_db

    return FrequencyResponse(
        frequencies_hz=freqs_list,
        raw_db=raw_db_list,
        target_db=target_db_list,
        post_eq_db=[float(v) for v in post_eq],
    )
