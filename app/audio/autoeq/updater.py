"""AutoEQ catalog updater — Phase 7 of the scope doc.

Fetches the full AutoEQ profile catalog from GitHub on user
demand and caches it in `user_data_dir()` so subsequent app
launches see the expanded set without going back to the network.

## Why fetch on demand instead of bundling 5,000 profiles

Bundling the whole AutoEQ catalog would balloon Tideway's
installer by ~30 MB of plain text. Phase 2 deliberately ships
~7 curated profiles for the out-of-the-box experience; Phase 7
gives users with niche headphones a one-click way to fill in
the rest.

## Layout

The cache dir mirrors the bundled-data layout exactly:

    user_data_dir/autoeq_cache/results/<source>/<brand-model>/
        <Brand Model> ParametricEQ.txt
        <Brand Model>.csv               (optional)

The Phase 2-6 index loader already knows that shape — Phase 7's
work is making the index walk BOTH directories at startup.

## Fetch strategy

- **Manifest:** one call to GitHub's Git Tree API to enumerate
  every `*ParametricEQ.txt` path under `results/`. ~30 KB JSON.
  Per-user; no auth required (60 req/hour limit is plenty for
  a manual "check for updates" workflow).
- **PEQ downloads:** small thread pool (3 workers) with jittered
  sleeps. raw.githubusercontent.com is a CDN and doesn't impose
  the same per-account rate limits as the API; the burst pacing
  is etiquette to avoid getting throttled mid-batch.
- **CSV downloads:** intentionally NOT bulk-fetched. Per-profile
  ~50 KB × 5,000 = 250 MB which is wasteful when only the
  profiles the user actually picks need a CSV (just for the FR
  graph in Phase 6). CSVs are best-effort lazy: when a user
  picks a profile that has no CSV in cache, we fetch one in the
  background and the graph degrades to post-EQ-only until then.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests

from app.paths import user_data_dir

from .index import default_data_dir

log = logging.getLogger(__name__)


# AutoEQ's Git Tree API. `recursive=1` returns the entire tree
# in one response. For AutoEQ that's ~25K entries, ~3 MB JSON —
# fine for a one-shot enumeration call.
_TREE_URL = (
    "https://api.github.com/repos/jaakkopasanen/AutoEq/"
    "git/trees/master?recursive=1"
)
_RAW_BASE = (
    "https://raw.githubusercontent.com/jaakkopasanen/AutoEq/master"
)
_USER_AGENT = "tideway-autoeq-updater/1.0"


@dataclass
class CatalogManifest:
    """Result of `fetch_manifest`. `profile_paths` is a list of
    repo-relative paths like
    `results/oratory1990/over-ear/Sennheiser HD 600/Sennheiser HD 600 ParametricEQ.txt`.

    Structured rather than raw-list so we can extend with CSV
    paths or measurement targets later without a breaking change.
    """

    profile_paths: list[str]
    fetched_at: int  # unix seconds


@dataclass
class DownloadResult:
    """Per-profile outcome for the bulk-download progress UI."""

    profile_id: str
    ok: bool
    reason: str = ""


def cache_dir() -> Path:
    """Where downloaded profiles live. Created on first write."""
    return user_data_dir() / "autoeq_cache" / "results"


def _http_get(url: str, timeout: float = 30.0) -> bytes:
    """GET via `requests` so the call goes through certifi's CA
    bundle. urllib's bundled-Python cert path fails SSL verification
    on real installs (and on Homebrew Python on macOS) because the
    OS-level cert store isn't always plumbed through to the embedded
    interpreter. `requests` ships its own CA file via certifi and
    works regardless. Same workaround `server._fetch_latest_release`
    uses for the v1.4.x update-check call.
    """
    resp = requests.get(
        url, headers={"User-Agent": _USER_AGENT}, timeout=timeout
    )
    resp.raise_for_status()
    return resp.content


def fetch_manifest() -> CatalogManifest:
    """Fetch the full list of `*ParametricEQ.txt` paths from
    AutoEQ's repo via GitHub's Tree API. One HTTP call.

    Raises on network / parse errors — the caller surfaces them
    as a 5xx response so the user can retry."""
    data = _http_get(_TREE_URL, timeout=30.0)
    parsed = json.loads(data.decode("utf-8"))
    tree = parsed.get("tree") or []
    if parsed.get("truncated"):
        # AutoEQ has historically been small enough to fit in
        # one tree response, but if that ever changes we'd need
        # to switch to the Contents API per directory.
        log.warning(
            "autoeq tree response truncated — manifest may be incomplete"
        )
    paths: list[str] = []
    for entry in tree:
        path = entry.get("path") or ""
        if entry.get("type") == "blob" and path.endswith("ParametricEQ.txt"):
            paths.append(path)
    return CatalogManifest(
        profile_paths=sorted(paths),
        fetched_at=int(time.time()),
    )


def _profile_id_from_path(path: str) -> str:
    """Convert a manifest path back into the index's profile_id
    format (`<source>/<brand-model>`). The manifest contains
    `results/<source>/<kind>/<brand-model>/<file>.txt`; we strip
    the leading `results/` and the kind tier (over-ear/in-ear)
    so IDs match what the index produces from the bundled data."""
    parts = path.split("/")
    # Drop `results/` (parts[0]) and the optional kind tier
    # (parts[2]). What remains: source / brand-model.
    if len(parts) >= 5 and parts[0] == "results":
        return f"{parts[1]}/{parts[3]}"
    # Fallback for unexpected layouts — still produces a stable id
    # but might not match the index's derivation. Caller logs.
    return path


def diff_manifest_against_disk(
    manifest: CatalogManifest, bundled_root: Path
) -> tuple[list[str], list[str]]:
    """Returns (already_on_disk_ids, missing_ids).

    "On disk" = present in either the bundled data root OR the
    cache dir. We don't re-download things the user already has.
    """
    cache_root = cache_dir()
    on_disk: set[str] = set()
    for root in (bundled_root, cache_root):
        if not root.exists():
            continue
        for txt in root.rglob("*ParametricEQ.txt"):
            try:
                rel = txt.relative_to(root)
                # rel = <source>/<brand-model>/<file>.txt
                parts = rel.parts
                if len(parts) >= 2:
                    on_disk.add(f"{parts[0]}/{parts[1]}")
            except ValueError:
                continue

    missing: list[str] = []
    already: list[str] = []
    seen_ids: set[str] = set()
    for path in manifest.profile_paths:
        pid = _profile_id_from_path(path)
        if pid in seen_ids:
            continue  # AutoEQ occasionally has duplicate paths
        seen_ids.add(pid)
        if pid in on_disk:
            already.append(pid)
        else:
            missing.append(pid)
    return already, missing


def _peq_url_for_path(path: str) -> str:
    """Build a raw.githubusercontent.com URL for a manifest
    entry. The path is repo-relative and may contain spaces /
    URL-special characters — quote per-segment to keep slashes
    intact."""
    quoted = "/".join(urllib.parse.quote(p) for p in path.split("/"))
    return f"{_RAW_BASE}/{quoted}"


def _csv_url_for_path(peq_path: str) -> str:
    """Sibling CSV path. AutoEQ's `<...>/<X> ParametricEQ.txt`
    lives next to `<...>/<X>.csv`."""
    csv_path = peq_path.replace(" ParametricEQ.txt", ".csv")
    return _peq_url_for_path(csv_path)


def _cache_target_for_path(peq_path: str) -> tuple[Path, Path]:
    """Where to write the PEQ + CSV in the cache dir.

    Layout matches the bundled-data layout (no kind tier) so the
    index loads both with the same logic.
    """
    parts = peq_path.split("/")
    # parts: ["results", source, kind, brand_model, "<X> ParametricEQ.txt"]
    if len(parts) < 5:
        raise ValueError(f"unexpected manifest path shape: {peq_path}")
    source = parts[1]
    brand_model = parts[3]
    filename = parts[4]
    target_dir = cache_dir() / source / brand_model
    peq_target = target_dir / filename
    csv_target = target_dir / filename.replace(" ParametricEQ.txt", ".csv")
    return peq_target, csv_target


def download_profiles(
    manifest_paths_by_id: dict[str, str],
    profile_ids: list[str],
    *,
    include_csv: bool = False,
    max_workers: int = 3,
    progress_cb=None,
) -> list[DownloadResult]:
    """Download the PEQ (and optionally CSV) files for the named
    profile_ids. Files land under `cache_dir()`.

    `manifest_paths_by_id` maps profile_id → manifest path; the
    caller builds it from `fetch_manifest` + `_profile_id_from_path`.

    Workers = 3 by default. raw.githubusercontent.com is a CDN
    so it doesn't impose the same per-account limits as the API,
    but burst-pacing is etiquette to avoid abuse-detection trips
    on a 5,000-profile bulk download. Per-profile failures are
    logged and surfaced individually rather than aborting the
    whole batch — one missing file shouldn't kill a 4,999-file
    download.
    """
    results: list[DownloadResult] = []
    results_lock = threading.Lock()
    done_count = [0]
    total = len(profile_ids)

    def _atomic_write(target: Path, data: bytes) -> None:
        """Write `data` to `target` such that no other process /
        future run can ever see a partially-written file. Crash
        mid-write leaves the prior state (or no file) — never a
        truncated one that subsequent `.exists()` checks treat as
        a complete cached profile and that the parser would then
        choke on later. Especially important for the bulk-catalog
        path that writes thousands of files in a row.
        """
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)

    def _one(pid: str) -> DownloadResult:
        path = manifest_paths_by_id.get(pid)
        if path is None:
            return DownloadResult(profile_id=pid, ok=False, reason="unknown id")
        try:
            peq_target, csv_target = _cache_target_for_path(path)
        except ValueError as exc:
            return DownloadResult(profile_id=pid, ok=False, reason=str(exc))

        peq_target.parent.mkdir(parents=True, exist_ok=True)

        if not peq_target.exists():
            try:
                _atomic_write(peq_target, _http_get(_peq_url_for_path(path)))
            except (OSError, requests.RequestException) as exc:
                return DownloadResult(
                    profile_id=pid, ok=False, reason=f"PEQ fetch failed: {exc}"
                )

        if include_csv and not csv_target.exists():
            try:
                _atomic_write(csv_target, _http_get(_csv_url_for_path(path)))
            except (OSError, requests.RequestException) as exc:
                # Non-fatal — CSV missing means no FR graph for
                # that profile, but the PEQ still works.
                log.debug("autoeq CSV fetch failed for %s: %s", pid, exc)

        return DownloadResult(profile_id=pid, ok=True)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        # Submit all jobs, then iterate via as_completed so progress
        # is reported in completion order — not submission order.
        # Pre-fix, the user saw "0/N" frozen for as long as the
        # first-submitted profile took, even when later workers had
        # already finished, because the loop blocked on
        # futures[0].result() before checking the others.
        futures = {pool.submit(_one, pid): pid for pid in profile_ids}
        for fut in as_completed(futures):
            res = fut.result()
            with results_lock:
                results.append(res)
                done_count[0] += 1
                if progress_cb is not None:
                    try:
                        progress_cb(done_count[0], total, res)
                    except Exception as exc:
                        # Caller's UI-update bug shouldn't kill the
                        # download. Log so the bug is debuggable, but
                        # keep working through the queue.
                        log.warning(
                            "autoeq progress callback raised: %s", exc
                        )
    return results


# ---------------------------------------------------------------------------
# Module-level state singleton — manifest cache + active-download progress.
#
# Held as a single dataclass under one lock so the
# /api/eq/check-updates → /api/eq/download-catalog →
# /api/eq/update-status flow can't race the manifest cache with
# the download counters. Code-review fix from the deploy PR.
# ---------------------------------------------------------------------------


class _UpdaterState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        # Manifest cache (populated by check_updates).
        self.manifest: Optional[CatalogManifest] = None
        self.missing_ids: list[str] = []
        self.already_ids: list[str] = []
        # Download progress (populated by start_download).
        self.running: bool = False
        self.started_at: float = 0.0
        self.total: int = 0
        self.done: int = 0
        self.succeeded: int = 0
        self.failed: int = 0
        self.last_error: str = ""

    def snapshot_progress(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "started_at": self.started_at,
                "total": self.total,
                "done": self.done,
                "succeeded": self.succeeded,
                "failed": self.failed,
                "last_error": self.last_error,
            }


_STATE = _UpdaterState()


def check_updates(bundled_root: Path) -> dict:
    """Fetch the AutoEQ manifest, diff against bundled + cache,
    and stash the result for the subsequent download call.

    Surfaces network errors as exceptions — callers translate to
    the right HTTP status. Diff results are also cached so the
    download endpoint doesn't have to re-fetch."""
    manifest = fetch_manifest()
    already, missing = diff_manifest_against_disk(manifest, bundled_root)
    with _STATE.lock:
        _STATE.manifest = manifest
        _STATE.missing_ids = missing
        _STATE.already_ids = already
    return {
        "ok": True,
        "total_in_catalog": len(manifest.profile_paths),
        "already_on_disk": len(already),
        "missing": len(missing),
        "fetched_at": manifest.fetched_at,
    }


def start_download(
    on_complete: Optional[Callable[[], None]] = None,
) -> tuple[bool, str, int]:
    """Kick off a background download of every cached-as-missing
    profile. Returns (started, reason, missing_count). Idempotent
    while a download is in progress (returns started=False).
    `on_complete` runs after the worker finishes, on the worker
    thread — used by server.py to reload the index."""
    with _STATE.lock:
        if _STATE.running:
            return False, "already running", 0
        if _STATE.manifest is None:
            return False, "no manifest cached — call check_updates first", 0
        manifest = _STATE.manifest
        missing = list(_STATE.missing_ids)
        if not missing:
            return False, "everything already on disk", 0
        _STATE.running = True
        _STATE.started_at = time.time()
        _STATE.total = len(missing)
        _STATE.done = 0
        _STATE.succeeded = 0
        _STATE.failed = 0
        _STATE.last_error = ""

    paths_by_id: dict[str, str] = {}
    for path in manifest.profile_paths:
        pid = _profile_id_from_path(path)
        paths_by_id.setdefault(pid, path)

    def _progress(done: int, total: int, res: DownloadResult) -> None:
        with _STATE.lock:
            _STATE.done = done
            if res.ok:
                _STATE.succeeded += 1
            else:
                _STATE.failed += 1
                _STATE.last_error = res.reason

    def _run() -> None:
        try:
            download_profiles(
                paths_by_id,
                missing,
                include_csv=False,  # see module docstring
                max_workers=3,
                progress_cb=_progress,
            )
        except Exception as exc:
            log.exception("download_profiles failed: %s", exc)
            with _STATE.lock:
                _STATE.last_error = str(exc)
        finally:
            with _STATE.lock:
                _STATE.running = False
            if on_complete is not None:
                try:
                    on_complete()
                except Exception:
                    log.exception("autoeq updater on_complete callback failed")

    threading.Thread(target=_run, daemon=True, name="autoeq-update").start()
    return True, "", len(missing)


def status() -> dict:
    """Snapshot of progress state — safe to call frequently."""
    return _STATE.snapshot_progress()


@dataclass
class CatalogSearchHit:
    """One result from a manifest search. Frontend renders these as
    a download-this-headphone list — `display` is what to show, the
    rest is for the action handler.
    """

    profile_id: str
    source: str
    headphone: str  # what the user actually searches by — e.g. "Sennheiser HD 600"
    on_disk: bool


def _brand_model_from_path(path: str) -> Optional[tuple[str, str, str]]:
    """Pull (source, headphone-name, profile_id) out of a manifest
    path. Returns None for unexpected layouts. The headphone-name
    matches what the index's `_brand_model` derivation produces, so
    the search results align with the picker's display."""
    parts = path.split("/")
    if len(parts) < 5 or parts[0] != "results":
        return None
    source = parts[1]
    headphone = parts[3]
    return source, headphone, f"{source}/{headphone}"


def search_manifest(
    query: str, bundled_root: Path, limit: int = 20
) -> list[CatalogSearchHit]:
    """Fuzzy-match `query` against the cached manifest's headphone
    names. Returns up to `limit` ranked hits, each tagged with whether
    it's already on disk (so the UI can show "Already downloaded"
    badges instead of redundant download buttons).

    Empty query returns the first `limit` headphones alphabetically
    so the picker has something to show before the user types.

    Caller must have populated the manifest cache via `check_updates`
    first; we don't auto-fetch here because manifest fetches are slow
    enough (3 MB JSON from GitHub) that the search input shouldn't
    silently blow up into a network call.
    """
    with _STATE.lock:
        manifest = _STATE.manifest
        already_set = set(_STATE.already_ids)

    if manifest is None:
        return []

    cache_root = cache_dir()
    on_disk_now: set[str] = set(already_set)
    if cache_root.exists():
        for txt in cache_root.rglob("*ParametricEQ.txt"):
            try:
                rel = txt.relative_to(cache_root)
                rparts = rel.parts
                if len(rparts) >= 2:
                    on_disk_now.add(f"{rparts[0]}/{rparts[1]}")
            except ValueError:
                continue
    if bundled_root.exists():
        for txt in bundled_root.rglob("*ParametricEQ.txt"):
            try:
                rel = txt.relative_to(bundled_root)
                rparts = rel.parts
                if len(rparts) >= 2:
                    on_disk_now.add(f"{rparts[0]}/{rparts[1]}")
            except ValueError:
                continue

    candidates: list[CatalogSearchHit] = []
    seen: set[str] = set()
    for path in manifest.profile_paths:
        parsed = _brand_model_from_path(path)
        if parsed is None:
            continue
        source, headphone, pid = parsed
        if pid in seen:
            continue
        seen.add(pid)
        candidates.append(
            CatalogSearchHit(
                profile_id=pid,
                source=source,
                headphone=headphone,
                on_disk=pid in on_disk_now,
            )
        )

    q = query.strip()
    if not q:
        candidates.sort(key=lambda c: c.headphone.lower())
        return candidates[:limit]

    # Reuse the same rapidfuzz/substring split the bundled-index
    # search uses so results match the user's expectations across
    # the two pickers.
    try:
        from rapidfuzz import fuzz, process  # type: ignore
        scored = process.extract(
            q,
            [c.headphone for c in candidates],
            scorer=fuzz.WRatio,
            limit=limit,
        )
        return [candidates[idx] for _h, _score, idx in scored]
    except ImportError:
        lq = q.lower()
        ranked = [
            (c.headphone.lower().find(lq), c)
            for c in candidates
            if lq in c.headphone.lower()
        ]
        ranked.sort(key=lambda t: (t[0], t[1].headphone.lower()))
        return [c for _pos, c in ranked[:limit]]


@dataclass
class HeadphoneSource:
    profile_id: str
    source: str
    on_disk: bool
    is_active: bool


@dataclass
class HeadphoneGroup:
    headphone: str
    sources: list[HeadphoneSource]
    installed_count: int
    total_count: int


# Lab names that are widely treated as the canonical AutoEQ source
# for a given headphone. Pinned to the top of each group's source
# list so the user's first instinct ("just give me the best one")
# lands on the source most reviewers cite. The order here is
# author-curated, not a quality judgement on the others.
_PREFERRED_SOURCES = (
    "oratory1990",
    "Crinacle",
    "Innerfidelity",
    "Headphone.com Legacy",
    "Rtings",
)


def _walk_on_disk(roots: list[Path]) -> set[str]:
    """Walk each root for `*ParametricEQ.txt` files, return the set
    of profile_ids that actually exist on disk. Profile_id format
    matches what `_profile_id_from_path` produces from manifest
    entries (`<source>/<headphone>`), so cross-reference against
    the manifest is a set lookup."""
    on_disk: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for txt in root.rglob("*ParametricEQ.txt"):
            try:
                rel = txt.relative_to(root)
                parts = rel.parts
                if len(parts) >= 2:
                    on_disk.add(f"{parts[0]}/{parts[1]}")
            except ValueError:
                continue
    return on_disk


def search_headphones(
    query: str,
    bundled_root: Path,
    active_profile_id: str,
    limit: int = 20,
) -> list[HeadphoneGroup]:
    """Group manifest entries by headphone name and return the top
    `limit` groups matching `query`. Each group lists every source
    that has measured this headphone, with on_disk and is_active
    flags so the UI can render per-source actions (Use this /
    Download / Active now) without an extra round trip.

    Sources within a group are stable-sorted: preferred sources
    (oratory1990, Crinacle, Innerfidelity, ...) first in their
    canonical order, then everything else alphabetically.

    The search is over headphone names only — source labels are
    intentionally excluded because users search for "HD 600", not
    for the lab that measured it.
    """
    with _STATE.lock:
        manifest = _STATE.manifest

    if manifest is None:
        return []

    on_disk = _walk_on_disk([bundled_root, cache_dir()])

    # Group manifest entries by headphone name.
    groups: dict[str, list[HeadphoneSource]] = {}
    for path in manifest.profile_paths:
        parsed = _brand_model_from_path(path)
        if parsed is None:
            continue
        source, headphone, pid = parsed
        groups.setdefault(headphone, []).append(
            HeadphoneSource(
                profile_id=pid,
                source=source,
                on_disk=pid in on_disk,
                is_active=pid == active_profile_id,
            )
        )

    # Stable-sort sources within each group: preferred-then-rest.
    pref_index = {name: i for i, name in enumerate(_PREFERRED_SOURCES)}
    for sources in groups.values():
        sources.sort(
            key=lambda s: (
                pref_index.get(s.source, len(_PREFERRED_SOURCES)),
                s.source.lower(),
            )
        )

    # Build a flat list of HeadphoneGroup for ranking.
    candidates = [
        HeadphoneGroup(
            headphone=name,
            sources=sources,
            installed_count=sum(1 for s in sources if s.on_disk),
            total_count=len(sources),
        )
        for name, sources in groups.items()
    ]

    q = query.strip()
    if not q:
        # Empty query: prefer popular headphones (those with the most
        # sources, since AutoEQ has more measurements for popular
        # models) then alphabetical.
        candidates.sort(
            key=lambda g: (-g.total_count, g.headphone.lower())
        )
        return candidates[:limit]

    try:
        from rapidfuzz import fuzz, process  # type: ignore
        scored = process.extract(
            q,
            [g.headphone for g in candidates],
            scorer=fuzz.WRatio,
            limit=limit,
        )
        return [candidates[idx] for _h, _score, idx in scored]
    except ImportError:
        lq = q.lower()
        ranked = [
            (g.headphone.lower().find(lq), g)
            for g in candidates
            if lq in g.headphone.lower()
        ]
        ranked.sort(key=lambda t: (t[0], t[1].headphone.lower()))
        return [g for _pos, g in ranked[:limit]]


def download_one(profile_id: str, *, include_csv: bool = True) -> DownloadResult:
    """Synchronous single-profile download. Used by the user-clicks-
    one-headphone flow; blocks for ~1-3 s while the PEQ + optional
    CSV come down. Caller is responsible for reloading the index
    afterward so the profile shows up in the picker."""
    with _STATE.lock:
        manifest = _STATE.manifest
    if manifest is None:
        return DownloadResult(
            profile_id=profile_id,
            ok=False,
            reason="no manifest cached — call check_updates first",
        )

    paths_by_id: dict[str, str] = {}
    for path in manifest.profile_paths:
        parsed = _brand_model_from_path(path)
        if parsed is None:
            continue
        _src, _name, pid = parsed
        paths_by_id.setdefault(pid, path)

    if profile_id not in paths_by_id:
        return DownloadResult(
            profile_id=profile_id, ok=False, reason="profile id not in manifest"
        )

    results = download_profiles(
        paths_by_id, [profile_id], include_csv=include_csv, max_workers=1
    )
    return results[0] if results else DownloadResult(
        profile_id=profile_id, ok=False, reason="no result"
    )


def fetch_csv_for_profile(profile_id: str) -> Optional[Path]:
    """Best-effort lazy CSV fetch. Used by the FR-graph endpoint
    when a profile is loaded but its CSV isn't yet on disk —
    typical for profiles downloaded via the bulk catalog updater
    (which intentionally skips CSVs to keep the download size
    sane).

    Order of operations:
      1. If the CSV is already on disk (bundled OR cache),
         return its path without a network call.
      2. If the manifest is in cache (user has clicked
         "Check for updates" this session), use it to find the
         CSV's repo path and download it. ~50 KB.
      3. Otherwise return None — caller renders the graph in
         post-EQ-only mode.

    Idempotent: re-running on a cached CSV is a fast no-op.
    """
    # Step 1: check if the CSV is already on disk anywhere.
    on_disk = _existing_csv_path(profile_id)
    if on_disk is not None:
        return on_disk

    # Step 2: need the manifest to know the source path.
    with _STATE.lock:
        manifest = _STATE.manifest
    if manifest is None:
        return None

    target_path = None
    for path in manifest.profile_paths:
        if _profile_id_from_path(path) == profile_id:
            target_path = path
            break
    if target_path is None:
        return None

    try:
        _, csv_target = _cache_target_for_path(target_path)
    except ValueError:
        return None

    csv_target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Atomic write — same reason as the bulk-download path. A
        # half-written CSV that subsequent calls would treat as
        # cached would render the FR graph as garbage instead of
        # degrading to post-EQ-only.
        tmp = csv_target.with_suffix(csv_target.suffix + ".part")
        tmp.write_bytes(_http_get(_csv_url_for_path(target_path)))
        tmp.replace(csv_target)
        return csv_target
    except (OSError, requests.RequestException) as exc:
        log.debug("autoeq lazy CSV fetch for %s failed: %s", profile_id, exc)
        return None


def fetch_csv_for_profile_async(profile_id: str) -> None:
    """Fire-and-forget version of `fetch_csv_for_profile`. Used
    by the FR-graph endpoint so high-frequency response calls
    aren't blocked on a network fetch — by the time the user's
    next slider drag fires another response request, the CSV is
    likely already on disk."""
    if _existing_csv_path(profile_id) is not None:
        return  # already on disk, no thread needed
    with _STATE.lock:
        if _STATE.manifest is None:
            return  # nothing we can do without the manifest
    threading.Thread(
        target=fetch_csv_for_profile,
        args=(profile_id,),
        daemon=True,
        name="autoeq-csv-lazy",
    ).start()


def _existing_csv_path(profile_id: str) -> Optional[Path]:
    """Return the on-disk CSV path for `profile_id` if one exists
    in either the bundled data dir or the cache dir, else None.

    Walks both directories looking for `<source>/<headphone>/<headphone>.csv`
    matching the profile_id's `<source>/<headphone>` shape."""
    parts = profile_id.split("/")
    if len(parts) != 2:
        return None
    source, headphone = parts
    csv_name = f"{headphone}.csv"
    candidates = [
        default_data_dir() / source / headphone / csv_name,
        cache_dir() / source / headphone / csv_name,
    ]
    for cand in candidates:
        if cand.exists():
            return cand
    return None


# Legacy alias kept for any external callers that imported the
# v1 stub function name. Routes through the implemented version.
def fetch_csv_lazy(profile_id: str) -> Optional[Path]:
    """Backwards-compat alias for `fetch_csv_for_profile`."""
    return fetch_csv_for_profile(profile_id)
