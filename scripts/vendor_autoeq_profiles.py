"""Fetch a curated subset of AutoEQ ParametricEQ profiles into the
bundled-data directory.

Phase 2 ships with ~12 popular profiles so users see a useful
picker on first install. Phase 7 (the update mechanism) will add
the full 5,000-profile set without re-running this script.

The list below is hand-picked for breadth: a couple of audiophile
references, a couple of mainstream noise-cancellers, and a couple
of pro / studio favourites. Bias is toward over-ear models
because that's where AutoEQ corrections matter most; in-ears can
be added as users request them.

Usage:
    python scripts/vendor_autoeq_profiles.py

Re-run any time to refresh against AutoEQ's master branch.
Idempotent — overwrites existing files in place.
"""
from __future__ import annotations

import sys
import urllib.parse
import urllib.request
from pathlib import Path


REPO_BASE = (
    "https://raw.githubusercontent.com/jaakkopasanen/AutoEq/master/results"
)

# (source, type, brand_model_dir, filename_prefix)
# `filename_prefix` is what the .txt file is named under — usually
# the same as `brand_model_dir`, but AutoEQ occasionally renames
# files for variant tags (e.g. "(Treble Boost)").
CURATED: list[tuple[str, str, str, str]] = [
    # Audiophile staples — Sennheiser's HD 6XX line is the entry-
    # point for the open-back hobby and what AutoEQ corrections
    # demo most clearly.
    ("oratory1990", "over-ear", "Sennheiser HD 600", "Sennheiser HD 600"),
    ("oratory1990", "over-ear", "Sennheiser HD 650", "Sennheiser HD 650"),
    ("oratory1990", "over-ear", "Sennheiser HD 800 S", "Sennheiser HD 800 S"),
    # Mainstream noise-cancellers — what most users actually own.
    ("oratory1990", "over-ear", "Sony WH-1000XM4", "Sony WH-1000XM4"),
    ("oratory1990", "over-ear", "Apple AirPods Max", "Apple AirPods Max"),
    ("oratory1990", "over-ear", "Bose QuietComfort 45", "Bose QuietComfort 45"),
    # In-ear reference: ER4SR is the canonical neutral IEM and a
    # useful sanity check that the in-ear path works.
    ("oratory1990", "in-ear", "Etymotic ER4SR", "Etymotic ER4SR"),
    # Profiles I tried but couldn't find at predictable paths
    # (likely renamed or moved in AutoEQ's results layout):
    #   Audeze LCD-X 2021, Beyerdynamic DT 770/990 Pro,
    #   Focal Clear MG, Apple AirPods Pro 2.
    # Phase 7's update mechanism will pull the full ~5,000-profile
    # set from a published index instead of guessing paths.
]


def fetch_one(
    out_root: Path, source: str, kind: str, dir_name: str, file_prefix: str
) -> Path:
    """Download one profile into `<out_root>/<source>/<brand_model>/`.

    Drops the `<kind>` (over-ear / in-ear) directory level — our
    index doesn't need it for picking, and keeping the layout flat
    makes profile IDs shorter. The kind is implicitly captured by
    which subset of headphones we vendored."""
    filename = urllib.parse.quote(f"{file_prefix} ParametricEQ.txt")
    url = (
        f"{REPO_BASE}/{source}/{kind}/"
        f"{urllib.parse.quote(dir_name)}/{filename}"
    )
    target_dir = out_root / source / dir_name
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{file_prefix} ParametricEQ.txt"

    print(f"fetching {source}/{dir_name} ... ", end="", flush=True)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "tideway-autoeq-vendor/1.0"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read().decode("utf-8")
    except Exception as exc:
        print(f"FAIL ({exc})")
        raise
    target_path.write_text(data, encoding="utf-8")
    print(f"OK ({len(data)} bytes)")
    return target_path


def main() -> int:
    here = Path(__file__).resolve().parent
    out_root = here.parent / "app" / "audio" / "autoeq" / "data" / "results"
    print(f"writing to {out_root}")

    failures = 0
    for source, kind, dir_name, file_prefix in CURATED:
        try:
            fetch_one(out_root, source, kind, dir_name, file_prefix)
        except Exception:
            failures += 1

    print()
    print(f"done — {len(CURATED) - failures}/{len(CURATED)} profiles vendored")
    return 1 if failures > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
