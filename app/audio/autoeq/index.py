"""In-memory profile index — load every ParametricEQ.txt under a
directory, expose listing + fuzzy-search + by-id lookup.

We load the small metadata + bands at startup (one disk walk
across ~5 KB of files per profile, even ~5,000 profiles costs
<1 second cold). Search uses substring matching over the
brand + model fields; if `rapidfuzz` is installed the search is
fuzzy-tolerant (typos / slight wording differences). When it's
not installed we fall back to a case-insensitive substring scan
which is slower but always correct.

Profile IDs are derived from the filesystem path under the data
directory: `<source>/<brand>/<model>` with the file's basename
trimmed. That gives stable, human-readable IDs for the API
without depending on a separately-published manifest.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from .profiles import (
    AutoEqParseError,
    AutoEqProfile,
    parse_profile_text,
)

log = logging.getLogger(__name__)


# `rapidfuzz` is the standard fast fuzzy-search library — cheap
# install, used elsewhere in the codebase. If the import ever
# breaks we degrade to substring search rather than crash startup.
try:
    from rapidfuzz import fuzz, process  # type: ignore

    _HAVE_RAPIDFUZZ = True
except Exception:
    _HAVE_RAPIDFUZZ = False


class AutoEqIndex:
    """Loaded set of profiles + search facets."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._profiles: dict[str, AutoEqProfile] = {}

    # --- loading ----------------------------------------------------

    def load_directory(self, root: Path) -> int:
        """Walk `root` recursively, parsing every `*ParametricEQ.txt`
        file. Returns the number of profiles successfully loaded.

        Layout convention: `<root>/<source>/<brand>/<model>/<file>.txt`.
        Files that don't fit that depth (e.g. a stray top-level
        text file) are loaded with empty source/brand fields and
        their basename as the model. Malformed files are logged
        and skipped — they shouldn't kill startup just because
        AutoEQ shipped one bad file.
        """
        return self.load_directories([root])

    def load_directories(self, roots: list[Path]) -> int:
        """Walk every directory in `roots` and load every
        `*ParametricEQ.txt` found. Later roots override earlier
        ones for any duplicate profile id — Phase 7 uses this so
        the user's downloaded `cache_dir` takes precedence over
        the bundled snapshot when both contain the same profile.

        Returns the count of successfully-loaded profiles after
        the merge."""
        with self._lock:
            self._profiles.clear()
            total_seen = 0
            for root in roots:
                if not root.exists():
                    log.debug("autoeq: data directory missing: %s", root)
                    continue
                count = 0
                for txt_path in sorted(root.rglob("*ParametricEQ.txt")):
                    try:
                        profile = self._load_one(txt_path, root)
                    except AutoEqParseError as exc:
                        log.warning("autoeq: skipping %s — %s", txt_path, exc)
                        continue
                    except Exception as exc:
                        log.warning(
                            "autoeq: unexpected error loading %s: %s",
                            txt_path,
                            exc,
                        )
                        continue
                    # Later roots override earlier ones — `cache`
                    # wins over `bundled` when both have the same
                    # profile_id.
                    self._profiles[profile.profile_id] = profile
                    count += 1
                total_seen += count
                log.info(
                    "autoeq: loaded %d profile(s) from %s", count, root
                )
        return len(self._profiles)

    def _load_one(self, path: Path, root: Path) -> AutoEqProfile:
        """Read one ParametricEQ.txt and tag it with metadata
        derived from its directory layout.

        Expected layout: `<root>/<source>/<headphone-dir>/<file>.txt`.
        Some vendor snapshots add a kind tier (over-ear / in-ear)
        between source and headphone-dir; we tolerate that by
        treating the kind dir as opaque and just picking the
        deepest non-file directory as the headphone name.

        Brand vs model split: AutoEQ's directories are named
        `<Brand> <Model>` (e.g. "Sennheiser HD 600", "Sony
        WH-1000XM4"). Splitting on the first space gives the
        right brand for every case I've seen. Multi-word brands
        ("1More", "Final Audio") would lose the second word, but
        none of those are in the curated starter set; if/when
        they enter, we revisit with an explicit brand list.
        """
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(root)
        parts = rel.parts

        # The headphone directory is the parent of the .txt file —
        # the deepest non-file segment regardless of how many
        # category levels precede it.
        headphone_dir = parts[-2] if len(parts) >= 2 else path.stem
        # The source is the topmost segment; everything between
        # source and headphone_dir we treat as opaque categorisation.
        source = parts[0] if len(parts) >= 2 else ""

        # Brand vs model split — first space wins.
        if " " in headphone_dir:
            brand, model = headphone_dir.split(" ", 1)
        else:
            brand = headphone_dir
            model = ""

        # Stable id: source + headphone directory. Round-trips
        # cleanly through JSON / URL query params.
        profile_id = "/".join(
            [p for p in (source, headphone_dir) if p]
        ).replace("\\", "/")
        if not profile_id:
            profile_id = path.stem

        return parse_profile_text(
            text,
            profile_id=profile_id,
            brand=brand,
            model=model,
            source=source,
        )

    # --- accessors --------------------------------------------------

    def get(self, profile_id: str) -> Optional[AutoEqProfile]:
        with self._lock:
            return self._profiles.get(profile_id)

    def count(self) -> int:
        with self._lock:
            return len(self._profiles)

    def search(
        self, query: str, limit: int = 50
    ) -> list[AutoEqProfile]:
        """Return up to `limit` profiles matching `query`. An empty
        query returns the first `limit` profiles in alphabetical
        order — useful for seeding the picker on first paint.

        Match scope: brand + model concatenated. Source intentionally
        excluded; users search for a headphone, not a measurement
        rig."""
        with self._lock:
            all_profiles = list(self._profiles.values())

        q = query.strip()
        if not q:
            sorted_profiles = sorted(
                all_profiles,
                key=lambda p: f"{p.brand} {p.model}".lower(),
            )
            return sorted_profiles[:limit]

        candidates = [(p, f"{p.brand} {p.model}") for p in all_profiles]

        if _HAVE_RAPIDFUZZ:
            # `process.extract` returns (haystack, score, idx) tuples
            # ranked by descending score. WRatio handles partial /
            # token-level matches gracefully ("hd 600" matches
            # "Sennheiser HD 600 (2003)").
            results = process.extract(
                q,
                [c[1] for c in candidates],
                scorer=fuzz.WRatio,
                limit=limit,
            )
            return [candidates[idx][0] for _h, _score, idx in results]

        # Fallback: case-insensitive substring rank. Less forgiving
        # than fuzz but always correct.
        lq = q.lower()
        scored = [
            (haystack.lower().find(lq), profile)
            for profile, haystack in candidates
            if lq in haystack.lower()
        ]
        scored.sort(key=lambda t: (t[0], t[1].brand.lower(), t[1].model.lower()))
        return [profile for _pos, profile in scored[:limit]]


# Module-level singleton. The audio engine + server.py share this
# one index so loads happen once per process.
INDEX = AutoEqIndex()


def default_data_dir() -> Path:
    """Bundled-data location: the package's `data/results` dir.
    Distinct from the on-disk update path Phase 7 will introduce
    for fetching newer profiles after install."""
    return Path(__file__).resolve().parent / "data" / "results"
