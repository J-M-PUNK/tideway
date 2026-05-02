import json
import os
import queue
import random
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import tidalapi

from app.http import SESSION
from app.paths import user_data_dir

# Where the downloader persists its pending-queue snapshot. Written on
# each add/clear, read once on restore(). Lives next to settings.json and
# tidal_session.json in the per-user data dir so a packaged build can
# actually write to it (the .app / Program Files dir is not writable).
QUEUE_STATE_FILE = user_data_dir() / "download_queue.json"

# Upper bound on worker threads — the Downloader spawns this many, but at
# most `settings.concurrent_downloads` run actual work at any moment. The
# user can slide the setting up or down at runtime without restart.
MAX_WORKER_THREADS = 10
DEFAULT_CONCURRENT_DOWNLOADS = 3


class _SharedRateLimiter:
    """Process-global thread-safe pacer used to bound TOTAL download
    throughput across all worker threads. Per-track `_RateLimiter`
    instances cap each stream individually; this one caps the sum.
    Without it, raising `concurrent_downloads` to 10 multiplies the
    per-track cap directly — defeating the throttle the moment a
    user wants more parallelism.

    Token-bucket style with a one-second burst. `set_rate` reconfigures
    live so the user can tune the per-track setting without restarting.
    """

    def __init__(self, bytes_per_sec: float):
        self._lock = threading.Lock()
        self.bytes_per_sec = max(0.0, bytes_per_sec)
        self._tokens = self.bytes_per_sec
        self._last = time.monotonic()

    def set_rate(self, bytes_per_sec: float) -> None:
        new_rate = max(0.0, bytes_per_sec)
        with self._lock:
            # Rate unchanged: don't wipe _tokens/_last. set_rate is
            # called per-track from every worker, so a same-rate
            # reset would let workers steal each other's accumulated
            # debt and briefly un-throttle the aggregate while a new
            # track was starting alongside in-flight ones.
            if new_rate == self.bytes_per_sec:
                return
            self.bytes_per_sec = new_rate
            self._tokens = new_rate
            self._last = time.monotonic()

    def consume(self, n: int) -> None:
        while True:
            with self._lock:
                if self.bytes_per_sec <= 0:
                    return
                now = time.monotonic()
                self._tokens = min(
                    self.bytes_per_sec,
                    self._tokens + (now - self._last) * self.bytes_per_sec,
                )
                self._last = now
                if n <= self._tokens:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
                self._tokens = 0
                wait = deficit / self.bytes_per_sec
            time.sleep(wait)


# Aggregate cap is per-track × 3 — generous enough that the typical
# 3-worker default sees no change, tight enough that cranking
# concurrent_downloads to 10 doesn't 10× the throughput. Updated by
# `_apply_aggregate_rate` whenever settings change.
_AGGREGATE_LIMITER = _SharedRateLimiter(0)


def _apply_aggregate_rate(per_track_mbps: int) -> None:
    """Recompute the aggregate cap from the current per-track setting."""
    if per_track_mbps and per_track_mbps > 0:
        _AGGREGATE_LIMITER.set_rate(per_track_mbps * 3 * 1_000_000)
    else:
        _AGGREGATE_LIMITER.set_rate(0)


# Smear consecutive bulk-enqueue calls across at least this much wall
# clock so a "queue 50 albums in 2 seconds" binge doesn't hit Tidal's
# CDN as a single burst. 3 s is short enough that users binging through
# their library don't notice; 50 albums then take ~2.5 minutes to
# enqueue instead of materializing instantly. The actual download
# throughput is governed by the per-track + aggregate limiters above.
_BULK_ENQUEUE_COOLDOWN_SEC = 3.0
_bulk_enqueue_lock = threading.Lock()
_last_bulk_enqueue_at: float = 0.0


def _wait_for_bulk_cooldown() -> None:
    global _last_bulk_enqueue_at
    with _bulk_enqueue_lock:
        now = time.monotonic()
        wait = (_last_bulk_enqueue_at + _BULK_ENQUEUE_COOLDOWN_SEC) - now
        _last_bulk_enqueue_at = now if wait <= 0 else now + wait
    if wait > 0:
        time.sleep(wait)


class _RateLimiter:
    """Per-chunk download pacer. Caps sustained throughput without
    banking debt across stalls — if the socket pauses, the next
    chunk is paced on its own merits instead of sleeping zero to
    "catch up" like a cumulative-bytes-over-cumulative-time scheme
    would. That stall-catchup behaviour would unmask the throttle
    right when Tidal's anomaly detector is most likely to notice.
    """

    def __init__(self, bytes_per_sec: float):
        self.bytes_per_sec = max(0.0, bytes_per_sec)
        self._last = time.monotonic()

    def consume(self, n: int) -> None:
        if self.bytes_per_sec <= 0:
            return
        now = time.monotonic()
        delta = now - self._last
        required = n / self.bytes_per_sec
        if delta < required:
            time.sleep(required - delta)
            self._last = time.monotonic()
        else:
            self._last = now
# Minimum progress delta between SSE updates; prevents broadcasting every
# 64KB chunk (~800 events per FLAC) while keeping the bar feeling live.
PROGRESS_UPDATE_THRESHOLD = 0.01
# Per-call retry budget for 429 responses before we give up and surface
# the failure. 3 attempts with a Retry-After-based sleep in between is
# enough to ride out a short throttling burst without stretching a
# single track download out for minutes.
RATE_LIMIT_MAX_ATTEMPTS = 3
# Safety cap on Retry-After — Tidal occasionally sends absurd values.
# 2 minutes is long enough that we're respecting the hint without
# letting a typo strand a worker for the rest of the day.
RATE_LIMIT_MAX_SLEEP = 120.0
# Fallback when the response doesn't set Retry-After. Exponential-ish
# to give the server a chance to catch its breath across successive
# 429s on the same worker.
RATE_LIMIT_DEFAULT_SLEEPS = (15.0, 30.0, 60.0)
# Rough per-track size estimates for the disk-space preflight. These
# are deliberately generous — we want to refuse obviously-doomed
# enqueues, not second-guess the user on a 5% overshoot. Quality names
# match tidalapi.Quality members.
_TRACK_SIZE_ESTIMATE_MB = {
    "low_96k": 3,
    "low_320k": 8,
    "high_lossless": 40,
    "hi_res_lossless": 90,
}
_DEFAULT_TRACK_SIZE_MB = 40  # when quality is None or unknown, assume Lossless.
# Free-space safety margin: keep this much space free on the drive
# *after* the estimated download lands. Prevents filling the disk to
# 0 bytes on small drives where the OS needs headroom.
_DISK_SAFETY_MARGIN_MB = 500


class ConcurrencyGate:
    """A resizable semaphore. Workers call `acquire()` before downloading
    and `release()` when done; only `limit` acquires can be outstanding at
    once. Calling `set_limit()` wakes any worker that now fits under the
    new cap. Under contraction, excess workers keep running until they
    finish their current item — we don't kill in-flight downloads.
    """

    def __init__(self, initial: int) -> None:
        self._limit = max(1, int(initial))
        self._active = 0
        self._cond = threading.Condition()

    def acquire(self) -> None:
        with self._cond:
            while self._active >= self._limit:
                self._cond.wait()
            self._active += 1

    def release(self) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()

    def set_limit(self, new_limit: int) -> None:
        new_limit = max(1, min(MAX_WORKER_THREADS, int(new_limit)))
        with self._cond:
            self._limit = new_limit
            # Wake potentially-blocked workers so any that now fit can run.
            self._cond.notify_all()


class DownloadStatus(Enum):
    PENDING = "Pending"
    FETCHING = "Fetching…"
    IN_PROGRESS = "Downloading"
    TAGGING = "Tagging…"
    COMPLETE = "Complete"
    FAILED = "Failed"


class _Cancelled(Exception):
    """Internal signal: a worker thread should abort the current download
    because the user pressed Cancel. Never surfaced to the UI — caught by
    _download's own handler, which cleans up the partial file and exits."""


@dataclass
class DownloadItem:
    item_id: str
    url: str
    title: str = "Fetching info…"
    artist: str = ""
    album: str = ""
    track_num: int = 0
    status: DownloadStatus = DownloadStatus.PENDING
    progress: float = 0.0
    error: Optional[str] = None
    quality: Optional[str] = None  # overrides session quality for this item
    file_path: Optional[str] = None  # final on-disk path once complete
    # Realtime download throughput in bytes per second, computed as a
    # rolling delta between SSE updates while the worker is in
    # IN_PROGRESS. Reset to 0.0 outside of that state — the UI only
    # renders this when the row's status reads "Downloading", so the
    # value being stale at COMPLETE / TAGGING wouldn't matter, but
    # we zero it anyway so the field never carries surprising data.
    speed_bps: float = 0.0
    # Extra metadata used by the filename template engine. Empty / zero
    # defaults are intentional — any of these may be unavailable from
    # tidalapi (older catalog entries, single-track submits without an
    # album lookup) and the template renderer treats missing values as
    # empty strings rather than crashing the download.
    album_artist: str = ""
    year: Optional[int] = None
    disc_num: int = 0
    track_explicit: bool = False
    album_explicit_flag: bool = False


class Downloader:
    def __init__(
        self,
        tidal_client,
        settings,
        on_add: Callable[[DownloadItem], None],
        on_update: Callable[[DownloadItem], None],
        on_remove: Optional[Callable[[str], None]] = None,
        on_file_ready: Optional[Callable[[str, Path], None]] = None,
    ):
        self.tidal = tidal_client
        self.settings = settings
        self.on_add = on_add
        self.on_update = on_update
        self.on_remove = on_remove or (lambda _: None)
        # Called with (tidal_track_id, final_path) when a track finishes
        # (including skip-existing). The server uses this to keep its local
        # index up to date without having to re-scan the output_dir.
        self.on_file_ready = on_file_ready or (lambda _id, _path: None)
        # _track_map is read/written from submit threads AND worker threads;
        # Python dict ops are atomic for single keys in CPython but multi-step
        # sequences (check-then-pop) aren't. Guard explicitly.
        self._track_map: Dict[str, Any] = {}
        self._track_map_lock = threading.Lock()
        self._work_queue: queue.Queue = queue.Queue()
        # Serializes any mutation of session.config.quality. Also serializes
        # reads of track.get_url() so a concurrent Settings PUT or preview
        # request can't swap quality mid-download. Exposed so the preview
        # endpoint can coordinate.
        self.quality_lock = threading.Lock()
        # Always spawn the full worker pool; the gate throttles how many
        # of them actually pull work at once.
        initial_limit = getattr(settings, "concurrent_downloads", DEFAULT_CONCURRENT_DOWNLOADS)
        self.gate = ConcurrencyGate(initial_limit)
        # Global pause — workers wait on this event after pulling an item
        # from the queue but before starting the download. Set = running,
        # clear = paused. In-flight downloads are not interrupted; pause
        # only blocks *new* items from starting.
        self._run_event = threading.Event()
        self._run_event.set()
        # Cancel support. `cancel(item_id)` adds the id here and removes
        # the row from the UI immediately; the worker checks this set at
        # function entry and between chunks so in-flight network/disk
        # work stops promptly. Guarded by its own lock so cancels from
        # the request thread don't race worker reads.
        self._cancelled_ids: set[str] = set()
        self._cancelled_lock = threading.Lock()
        # Shared rate-limit clock. When a worker gets a 429, it writes
        # the "don't try again until" monotonic timestamp here so its
        # siblings also back off instead of piling more 429s onto the
        # same bucket. Guarded by a lock so the write + notify is atomic.
        self._rate_limit_until: float = 0.0
        self._rate_limit_lock = threading.Lock()
        # Pending-queue snapshot for restore-after-restart. Only items
        # still in PENDING are persisted — an IN_PROGRESS download
        # streams through an HTTP connection that can't be resumed, so
        # remembering it wouldn't help. Keyed by item_id for O(1) update.
        self._pending_meta: Dict[str, dict] = {}
        self._pending_lock = threading.Lock()
        # Sweep any stale `.part` files left behind by a previous crash
        # before workers start — otherwise the user's output folder
        # slowly fills with orphans a skip-existing scan won't touch.
        self._sweep_orphan_parts()
        for _ in range(MAX_WORKER_THREADS):
            threading.Thread(target=self._worker_loop, daemon=True).start()

    # ------------------------------------------------------------------
    # _track_map helpers — always locked
    # ------------------------------------------------------------------

    def _track_map_put(self, item_id: str, pair) -> None:
        with self._track_map_lock:
            self._track_map[item_id] = pair

    def _track_map_get(self, item_id: str):
        with self._track_map_lock:
            return self._track_map.get(item_id, (None, None))

    def _track_map_pop(self, item_id: str) -> None:
        with self._track_map_lock:
            self._track_map.pop(item_id, None)

    def _track_map_has(self, item_id: str) -> bool:
        with self._track_map_lock:
            return item_id in self._track_map

    def submit(self, url: str, quality: Optional[str] = None):
        threading.Thread(target=self._expand_and_enqueue, args=(url, quality), daemon=True).start()

    def submit_object(self, obj, content_type: str, quality: Optional[str] = None):
        """Enqueue a tidalapi object directly — skips the URL fetch step."""
        threading.Thread(
            target=self._enqueue_object, args=(obj, content_type, quality), daemon=True
        ).start()

    def _enqueue_object(self, obj, content_type: str, quality: Optional[str] = None):
        import sys as _sys

        print(
            f"[downloader] _enqueue_object kind={content_type} "
            f"id={getattr(obj, 'id', '?')} quality={quality!r}",
            file=_sys.stderr,
            flush=True,
        )
        pairs: list[tuple]
        try:
            if content_type == "track":
                pairs = [(obj, getattr(obj, "album", None))]
            elif content_type == "album":
                tracks = self._call_with_auth_retry(obj.tracks)
                pairs = [(t, obj) for t in tracks]
            elif content_type == "playlist":
                tracks = self._call_with_auth_retry(obj.tracks)
                pairs = [(t, getattr(t, "album", None)) for t in tracks]
            else:
                print(
                    f"[downloader] _enqueue_object: unsupported kind {content_type!r}",
                    file=_sys.stderr,
                    flush=True,
                )
                return
        except Exception as exc:
            print(
                f"[downloader] _enqueue_object expand FAILED kind={content_type} "
                f"exc={exc!r}",
                file=_sys.stderr,
                flush=True,
            )
            # Surface the failure instead of swallowing silently — otherwise
            # the user clicks Download and nothing happens.
            self._surface_enqueue_failure(content_type, exc)
            return

        # Preflight: bail early if the drive clearly can't hold this.
        # Runs after the expand since we need the track count; the
        # failure row replaces the silence the user would otherwise get
        # when a huge playlist fills the disk mid-download.
        refusal = self._preflight_disk_space(len(pairs), quality)
        if refusal is not None:
            print(
                f"[downloader] _enqueue_object refused: {refusal}",
                file=_sys.stderr,
                flush=True,
            )
            self._surface_preflight_failure(refusal)
            return

        # Shuffle the work order so the CDN doesn't see a sequential
        # 1, 2, 3, … fetch pattern across the album/playlist. Real
        # users skip around; a strict in-order pull is a textbook
        # scrape signature.
        if len(pairs) > 1 and content_type in ("album", "playlist"):
            random.shuffle(pairs)

        # Smear back-to-back bulk enqueues across a small cooldown so
        # binge-collecting 50 albums doesn't slam the queue in one
        # tick. Single-track adds skip this — only album/playlist.
        if content_type in ("album", "playlist"):
            _wait_for_bulk_cooldown()

        print(
            f"[downloader] _enqueue_object enqueuing {len(pairs)} track(s)",
            file=_sys.stderr,
            flush=True,
        )
        for track, album_obj in pairs:
            item = DownloadItem(item_id=str(uuid.uuid4()), url="")
            _populate_item_from_track(item, track, album_obj, quality)
            self._track_map_put(item.item_id, (track, album_obj))
            self.on_add(item)
            # Persist so a restart can resume. Per-track records use
            # the track's own tidal id so each item is restorable on
            # its own — we don't re-expand the parent album/playlist
            # (restoring 20 individual track submits is safer than
            # re-expanding an album whose track list may have changed).
            tid = getattr(track, "id", None)
            if tid is not None:
                self._record_pending(
                    item.item_id,
                    {
                        "kind": "track",
                        "id": str(tid),
                        "quality": quality,
                        "title": item.title,
                        "artist": item.artist,
                        "album": item.album,
                    },
                )
            self._work_queue.put(item)

    def _call_with_auth_retry(self, fn, *args, **kwargs):
        """Call a Tidal-hitting function, retry once on 401 after forcing
        a token refresh. Used for `album.tracks()` / `playlist.tracks()`
        in the enqueue-expand path, which are separate API calls from the
        initial session.album/playlist lookup and can 401 on their own.
        tidalapi's built-in refresh only fires when the 401 body carries
        the exact string 'The token has expired.' — Tidal often doesn't,
        so we handle it ourselves.
        """
        import sys as _sys

        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not _looks_like_auth_error(exc):
                raise
            print(
                f"[downloader] auth error on {getattr(fn, '__name__', fn)!r}: "
                f"{exc!r} — forcing refresh",
                file=_sys.stderr,
                flush=True,
            )
            refresh = getattr(self.tidal, "force_refresh", None)
            if callable(refresh) and refresh():
                return fn(*args, **kwargs)
            raise

    def _surface_enqueue_failure(self, content_type: str, exc: Exception) -> None:
        placeholder = DownloadItem(item_id=str(uuid.uuid4()), url="")
        placeholder.title = f"Couldn't expand {content_type}"
        placeholder.status = DownloadStatus.FAILED
        placeholder.error = str(exc)
        self.on_add(placeholder)

    def _preflight_disk_space(
        self, track_count: int, quality: Optional[str]
    ) -> Optional[str]:
        """Refuse obviously-doomed enqueues before they start filling up
        the queue. Returns an error message string if the request likely
        won't fit on disk, or None if it's fine to proceed.

        Intentionally crude: we don't know real per-track size until
        we've fetched the manifest, so this is a generous estimate meant
        to catch the "5GB playlist on a 100MB free drive" case, not to
        second-guess on small overshoots. If disk_usage fails (network
        mount flakiness, permission error), we don't block — better to
        let the download itself surface the problem.
        """
        if track_count <= 0:
            return None
        per_track_mb = _TRACK_SIZE_ESTIMATE_MB.get(
            quality or "", _DEFAULT_TRACK_SIZE_MB
        )
        need_mb = track_count * per_track_mb + _DISK_SAFETY_MARGIN_MB
        # expanduser so `~/Music/...` measures the right filesystem
        # instead of measuring "." (which may be a different drive on
        # Windows entirely and would give a wildly wrong number).
        out_dir = Path(getattr(self.settings, "output_dir", ".")).expanduser()
        # disk_usage needs an existing path; walk up to the first one
        # that exists so a not-yet-created output dir doesn't throw.
        probe: Path = out_dir
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            free_bytes = shutil.disk_usage(str(probe)).free
        except Exception:
            return None  # unknown — don't block.
        free_mb = free_bytes // (1024 * 1024)
        if free_mb >= need_mb:
            return None
        return (
            f"Not enough disk space — need ~{need_mb} MB (estimate), "
            f"{free_mb} MB free on {probe}."
        )

    def _surface_preflight_failure(self, message: str) -> None:
        placeholder = DownloadItem(item_id=str(uuid.uuid4()), url="")
        placeholder.title = "Download refused"
        placeholder.status = DownloadStatus.FAILED
        placeholder.error = message
        self.on_add(placeholder)

    # ------------------------------------------------------------------

    def _expand_and_enqueue(self, url: str, quality: Optional[str] = None):
        placeholder = DownloadItem(item_id=str(uuid.uuid4()), url=url)
        self.on_add(placeholder)

        try:
            content_type, obj = self.tidal.fetch_url(url)
        except Exception as exc:
            placeholder.status = DownloadStatus.FAILED
            placeholder.error = str(exc)
            self.on_update(placeholder)
            return

        if content_type == "track":
            pairs = [(obj, getattr(obj, "album", None))]
        elif content_type == "album":
            pairs = [(t, obj) for t in obj.tracks()]
        elif content_type == "playlist":
            pairs = [(t, getattr(t, "album", None)) for t in obj.tracks()]
        else:
            placeholder.status = DownloadStatus.FAILED
            placeholder.error = f"Unsupported type: {content_type}"
            self.on_update(placeholder)
            return

        # Preflight disk space before committing any track to the queue.
        # If it fails we convert the placeholder into the refusal row so
        # the user sees a clear reason instead of silence.
        refusal = self._preflight_disk_space(len(pairs), quality)
        if refusal is not None:
            placeholder.status = DownloadStatus.FAILED
            placeholder.error = refusal
            placeholder.title = "Download refused"
            self.on_update(placeholder)
            return

        if len(pairs) == 1:
            track, album_obj = pairs[0]
            _populate_item_from_track(placeholder, track, album_obj, quality)
            self._track_map_put(placeholder.item_id, (track, album_obj))
            self.on_update(placeholder)
            tid = getattr(track, "id", None)
            if tid is not None:
                self._record_pending(
                    placeholder.item_id,
                    {
                        "kind": "track",
                        "id": str(tid),
                        "quality": quality,
                        "title": placeholder.title,
                        "artist": placeholder.artist,
                        "album": placeholder.album,
                    },
                )
            self._work_queue.put(placeholder)
        else:
            # Drop the placeholder entirely — the per-track items replace it.
            self.on_remove(placeholder.item_id)

            for track, album_obj in pairs:
                item = DownloadItem(item_id=str(uuid.uuid4()), url=url)
                _populate_item_from_track(item, track, album_obj, quality)
                self._track_map_put(item.item_id, (track, album_obj))
                self.on_add(item)
                tid = getattr(track, "id", None)
                if tid is not None:
                    self._record_pending(
                        item.item_id,
                        {
                            "kind": "track",
                            "id": str(tid),
                            "quality": quality,
                            "title": item.title,
                            "artist": item.artist,
                            "album": item.album,
                        },
                    )
                self._work_queue.put(item)

    def retry(self, item: DownloadItem, quality: Optional[str] = None) -> None:
        """Re-queue an existing item. Used by the 'Retry failed' button.

        Accepts an optional `quality` so the caller can bump a failed
        hi-res download down to Lossless without re-adding it by hand.
        """
        if not self._track_map_has(item.item_id):
            return
        if quality is not None:
            item.quality = quality
        item.status = DownloadStatus.PENDING
        item.progress = 0.0
        item.error = None
        self.on_update(item)
        self._work_queue.put(item)

    @property
    def paused(self) -> bool:
        return not self._run_event.is_set()

    def pause(self) -> None:
        self._run_event.clear()

    def resume(self) -> None:
        self._run_event.set()

    def cancel(self, item_id: str) -> None:
        """Cancel a pending or in-flight download. The row is removed
        from the UI immediately; any worker currently downloading this
        item notices the flag at the next chunk boundary and cleans up
        its partial file. Terminal items (Complete / Failed) are
        unaffected — use clear_completed for those.
        """
        with self._cancelled_lock:
            self._cancelled_ids.add(item_id)
        self._track_map_pop(item_id)
        # Also drop from persisted queue so a restart doesn't re-enqueue
        # something the user explicitly cancelled.
        self._clear_pending(item_id)
        self.on_remove(item_id)

    def _is_cancelled(self, item_id: str) -> bool:
        with self._cancelled_lock:
            return item_id in self._cancelled_ids

    def _check_cancel(self, item_id: str) -> None:
        if self._is_cancelled(item_id):
            raise _Cancelled()

    def _sweep_orphan_parts(self) -> None:
        """Remove stale `.part` files from the output directory.

        Crashes / cancels / 429 mid-stream leave these behind. They
        can't be resumed (no Range support on Tidal's streams) and
        skip-existing ignores them (suffix isn't in the audio
        allowlist), so they only ever accumulate. Clearing them on
        boot keeps the folder tidy.

        Best-effort: errors are swallowed. Non-existent output_dir is
        fine — nothing to sweep.
        """
        import sys as _sys

        # expanduser so a stored `~/Music/Tideway` actually resolves —
        # otherwise .exists() returns False and we sweep nothing. The
        # server-level sweep already handles this, but this one should
        # too in case the downloader is used standalone.
        out_dir = Path(getattr(self.settings, "output_dir", ".")).expanduser()
        if not out_dir.exists():
            return
        removed = 0
        try:
            for path in out_dir.rglob("*.part"):
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except Exception:
                    continue
        except Exception as exc:
            print(
                f"[downloader] sweep_orphan_parts: rglob failed: {exc!r}",
                file=_sys.stderr,
                flush=True,
            )
            return
        if removed:
            print(
                f"[downloader] sweep_orphan_parts: removed {removed} stale .part file(s)",
                file=_sys.stderr,
                flush=True,
            )

    def _record_pending(self, item_id: str, meta: dict) -> None:
        """Remember a pending item so restore() can re-enqueue it after
        a restart. Meta must be self-contained: kind, id, quality,
        title/artist/album for UI reconstruction."""
        with self._pending_lock:
            self._pending_meta[item_id] = meta
        self._persist_pending()

    def _clear_pending(self, item_id: str) -> None:
        """Drop an item from the persisted queue — called when it starts
        downloading, finishes, fails, or is cancelled."""
        with self._pending_lock:
            if item_id not in self._pending_meta:
                return
            self._pending_meta.pop(item_id, None)
        self._persist_pending()

    def _persist_pending(self) -> None:
        """Write the current pending snapshot to disk, atomically.

        Single writer call per change — no debounce since the write is
        cheap (tens of tracks at most) and the cadence (user clicks
        Download) is low. Atomic rename so a crash can't leave a
        half-written JSON file that would silently drop the queue on
        next boot.
        """
        import sys as _sys

        with self._pending_lock:
            snapshot = list(self._pending_meta.values())
        target = QUEUE_STATE_FILE
        # Pre-declare so the cleanup branch can check existence safely —
        # otherwise a failure inside mkstemp leaves tmp_path unbound and
        # the `except` block raises a NameError instead of cleaning up.
        tmp_path: Optional[str] = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=".download_queue.",
                suffix=".tmp",
                dir=str(target.parent),
            )
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(snapshot, f)
            os.replace(tmp_path, target)
        except Exception as exc:
            print(
                f"[downloader] _persist_pending: write failed: {exc!r}",
                file=_sys.stderr,
                flush=True,
            )
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def restore(self) -> int:
        """Re-enqueue pending items from the persisted snapshot.

        Called by the server after the Tidal session is ready —
        submit() spawns a thread that hits Tidal, so calling restore()
        before login would just fail every item. Returns the number of
        items resubmitted; 0 means there was nothing to restore (or
        the file was missing/corrupt, which we treat the same).
        """
        import sys as _sys

        if not QUEUE_STATE_FILE.exists():
            return 0
        try:
            with open(QUEUE_STATE_FILE) as f:
                snapshot = json.load(f)
        except Exception as exc:
            print(
                f"[downloader] restore: couldn't read {QUEUE_STATE_FILE}: {exc!r}",
                file=_sys.stderr,
                flush=True,
            )
            return 0
        if not isinstance(snapshot, list):
            return 0
        count = 0
        for entry in snapshot:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            obj_id = entry.get("id")
            quality = entry.get("quality")
            if not kind or not obj_id:
                continue
            # Convert to a canonical Tidal URL and re-submit. Going
            # through the URL path means the existing fetch_url/expand
            # logic handles track vs album vs playlist uniformly.
            if kind == "playlist":
                url = f"https://tidal.com/browse/playlist/{obj_id}"
            else:
                url = f"https://tidal.com/browse/{kind}/{obj_id}"
            try:
                self.submit(url, quality=quality)
                count += 1
            except Exception as exc:
                print(
                    f"[downloader] restore: submit failed for {kind}/{obj_id}: {exc!r}",
                    file=_sys.stderr,
                    flush=True,
                )
        # Clear the file — the new submits will repopulate it as they
        # enqueue. If login isn't actually complete and submits silently
        # fail inside the expand thread, those items are just lost; the
        # alternative (keep the file) risks infinite-loop resubmits on
        # every restart for an item that will never succeed.
        try:
            QUEUE_STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        print(
            f"[downloader] restore: resubmitted {count} pending item(s)",
            file=_sys.stderr,
            flush=True,
        )
        return count

    def _wait_for_rate_limit(self, item_id: Optional[str] = None) -> None:
        """Block until the shared rate-limit deadline passes.

        Workers call this before any network-touching step. Sleeping in
        short slices instead of one `time.sleep(remaining)` keeps cancel
        responsive — a user who hits Cancel while the queue is parked on
        a 429 still sees the row disappear within a second.
        """
        while True:
            with self._rate_limit_lock:
                remaining = self._rate_limit_until - time.monotonic()
            if remaining <= 0:
                return
            if item_id is not None and self._is_cancelled(item_id):
                raise _Cancelled()
            time.sleep(min(1.0, remaining))

    def _note_rate_limit(self, exc: Exception, attempt: int) -> None:
        """Record a 429 so every worker backs off.

        We take the *later* of the existing deadline and the new one so
        overlapping 429s from sibling workers don't keep resetting the
        clock to the earliest value.
        """
        import sys as _sys

        retry_after = _extract_retry_after(exc)
        if retry_after is None:
            # Index into the fallback table, clamped to its last entry.
            idx = min(attempt, len(RATE_LIMIT_DEFAULT_SLEEPS) - 1)
            retry_after = RATE_LIMIT_DEFAULT_SLEEPS[idx]
        retry_after = min(retry_after, RATE_LIMIT_MAX_SLEEP)
        deadline = time.monotonic() + retry_after
        with self._rate_limit_lock:
            if deadline > self._rate_limit_until:
                self._rate_limit_until = deadline
        print(
            f"[downloader] rate-limited (attempt {attempt + 1}) — "
            f"backing off {retry_after:.1f}s",
            file=_sys.stderr,
            flush=True,
        )

    def _publish_update(self, item: DownloadItem) -> None:
        """Worker-side update hook. Swallows updates for items the user
        has cancelled so a progress tick that fires after the remove
        event doesn't resurrect the row in the broker's snapshot."""
        if self._is_cancelled(item.item_id):
            return
        self.on_update(item)

    def _worker_loop(self):
        while True:
            item = self._work_queue.get()
            # Fast-path cancel: if the user cancelled while the item was
            # sitting in the queue, drop it without even acquiring the
            # gate — otherwise a backlog of cancelled items would still
            # serialize through the concurrency limiter.
            if self._is_cancelled(item.item_id):
                with self._cancelled_lock:
                    self._cancelled_ids.discard(item.item_id)
                continue
            # Honor a pause before we acquire the gate — otherwise a
            # pause would still let `concurrent_downloads` items start
            # simultaneously after a resume. Re-check after each wake in
            # case someone else holds the gate full.
            self._run_event.wait()
            self.gate.acquire()
            # Clear the persisted pending record up front — once we
            # start a download the item transitions to IN_PROGRESS and
            # isn't resumable anyway. If we crash mid-download, the
            # restart shouldn't re-enqueue an item the user can still
            # see sitting as FAILED in the UI (once the catch-all
            # handler marks it so).
            self._clear_pending(item.item_id)
            try:
                self._download(item)
            except Exception as exc:
                # _download has its own try/except for the body of the
                # download, but a few setup steps (debug prints, lookups)
                # run *before* that try block. An exception there used
                # to escape this loop with no `except`, killing the
                # worker thread and leaving every subsequent download
                # stuck in PENDING forever. The triggering case in the
                # field was a UnicodeEncodeError on a `print(... title=
                # !r ...)` for a track with non-locale-codepage chars
                # in the title (#7, #36, #70). Catch here so a single
                # bad item fails cleanly without taking the worker down.
                import sys as _sys
                import traceback as _tb
                try:
                    print(
                        f"[downloader] worker caught escaped exception "
                        f"id={item.item_id[:8]}: {type(exc).__name__}",
                        file=_sys.stderr,
                        flush=True,
                    )
                    _tb.print_exc(file=_sys.stderr)
                except Exception:
                    # Logging itself can fail (the original exception
                    # might *be* a print/encoding failure). Don't let
                    # that re-kill the worker.
                    pass
                try:
                    item.status = DownloadStatus.FAILED
                    item.error = f"{type(exc).__name__}: {exc}"
                    item.speed_bps = 0.0
                    self._publish_update(item)
                except Exception:
                    pass
            finally:
                self.gate.release()

    def _download(self, item: DownloadItem):
        import sys as _sys
        import traceback as _tb

        print(
            f"[downloader] _download START id={item.item_id[:8]} "
            f"title={item.title!r} quality={item.quality!r}",
            file=_sys.stderr,
            flush=True,
        )
        tmp_path: Optional[Path] = None
        # Snapshot settings once at the top so a concurrent Settings PUT
        # that swaps self.settings mid-download can't tear reads of
        # output_dir / filename_template / create_album_folders across
        # `_find_existing` and `_build_path`. Without this, a user flipping
        # create_album_folders between the skip-existing check and the
        # write would scan one tree but write into another.
        s = self.settings
        try:
            self._check_cancel(item.item_id)
            track, album_obj = self._track_map_get(item.item_id)
            if track is None:
                raise RuntimeError("Track reference lost")

            # Skip-existing: if any audio file with the same stem already
            # lives at the destination, treat the item as complete.
            if getattr(s, "skip_existing", True):
                existing = _find_existing(item, s)
                if existing is not None:
                    item.progress = 1.0
                    item.status = DownloadStatus.COMPLETE
                    # Note, not error — UI treats error as a failure banner.
                    item.error = None
                    item.file_path = str(existing)
                    self._publish_update(item)
                    tid = getattr(track, "id", None)
                    if tid is not None:
                        self.on_file_ready(str(tid), existing)
                    self._track_map_pop(item.item_id)
                    return

            item.status = DownloadStatus.IN_PROGRESS
            item.progress = 0.0
            self._publish_update(item)

            print(
                f"[downloader] _download id={item.item_id[:8]} fetching stream URL "
                f"track_id={getattr(track, 'id', '?')} quality={item.quality!r}",
                file=_sys.stderr,
                flush=True,
            )
            urls, ext_hint, codec = self._fetch_stream_sources(
                track, item.quality, item_id=item.item_id
            )
            if not urls:
                raise RuntimeError("Tidal returned no stream URLs")
            print(
                f"[downloader] _download id={item.item_id[:8]} got "
                f"{len(urls)} URL(s) ext_hint={ext_hint!r} codec={codec!r}",
                file=_sys.stderr,
                flush=True,
            )

            # Tidal's hi-res FLAC ships as fragmented MP4 segments — the
            # codec inside is FLAC but the container is MP4, so tidalapi's
            # file_extension hint says ".m4a". Detect that case and plan
            # to remux into a native .flac container after the download.
            # The user gets the right extension and standard FLAC framing
            # (which most players prefer over FLAC-in-MP4 even though
            # both are bit-identical audio).
            needs_flac_remux = codec == "FLAC" and ext_hint != ".flac"

            # For the device-code path we can't know the final extension
            # until the first response's Content-Type arrives. For PKCE
            # the manifest gives us a reliable hint up front. Open the
            # first URL to resolve the extension, then write it + every
            # remaining URL sequentially into the same .part file.
            #
            # Park here if a sibling worker just hit a 429 — no sense
            # opening another connection straight into the same throttle.
            self._wait_for_rate_limit(item.item_id)
            first_resp_cm = SESSION.get(urls[0], stream=True, timeout=60)
            first_resp = first_resp_cm.__enter__()
            try:
                first_resp.raise_for_status()
                if needs_flac_remux:
                    # We know the final file is .flac (after remux); the
                    # download itself lands in an .mp4.part intermediate.
                    ext = ".flac"
                    out_path = _build_path(item, s, ext)
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path = out_path.with_suffix(".mp4.part")
                else:
                    ext = ext_hint or _ext_from_response(first_resp)
                    out_path = _build_path(item, s, ext)
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path = out_path.with_suffix(out_path.suffix + ".part")

                # Progress tracking. For multi-URL DASH downloads we
                # don't know total bytes up front, so we treat each URL
                # as an equal slice of the progress bar. Within a URL
                # with known Content-Length we interpolate.
                total_urls = len(urls)
                first_len = int(first_resp.headers.get("Content-Length", 0))
                last_published = 0.0
                # Realtime throughput tracking. We don't measure on
                # every chunk because that's noisy and would require a
                # high-frequency timer; instead we compute the delta
                # between successive SSE publishes (~every
                # PROGRESS_UPDATE_THRESHOLD of progress, typically 4-8
                # times per track). That cadence is a good fit for the
                # UI's display refresh — fast enough to feel live, slow
                # enough that the number doesn't jitter.
                bytes_total = 0
                speed_window_bytes = 0
                speed_window_start = time.monotonic()

                def _bump(url_idx: int, inner: float) -> None:
                    nonlocal last_published, speed_window_bytes, speed_window_start
                    item.progress = min(0.999, (url_idx + inner) / total_urls)
                    if item.progress - last_published >= PROGRESS_UPDATE_THRESHOLD:
                        now = time.monotonic()
                        elapsed = now - speed_window_start
                        # Guard against div-by-tiny on a fast burst.
                        # 50ms is plenty — at 64KB chunks even 100MB/s
                        # downloads only take 0.5ms per chunk, so a sub-
                        # 50ms window means we got several chunks back-
                        # to-back from a connection-pool warm path.
                        if elapsed >= 0.05:
                            delta = bytes_total - speed_window_bytes
                            item.speed_bps = max(0.0, delta / elapsed)
                            speed_window_bytes = bytes_total
                            speed_window_start = now
                        last_published = item.progress
                        self._publish_update(item)

                # Rate-limit per-track fetch so the CDN pattern looks
                # like aggressive prefetch rather than bulk scrape. 0
                # (or missing) means unlimited — backward-compatible
                # with older settings blobs. The shared aggregate
                # limiter (process-global) caps the sum across all
                # workers; we reconfigure it here in case the user
                # adjusted the setting since the last download.
                rate_mbps = getattr(self.settings, "download_rate_limit_mbps", 0) or 0
                _apply_aggregate_rate(rate_mbps)
                limiter = _RateLimiter(rate_mbps * 1_000_000) if rate_mbps > 0 else None

                with open(tmp_path, "wb") as f:
                    # Write the first URL we already opened.
                    got = 0
                    for chunk in first_resp.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        self._check_cancel(item.item_id)
                        f.write(chunk)
                        got += len(chunk)
                        bytes_total += len(chunk)
                        _AGGREGATE_LIMITER.consume(len(chunk))
                        if limiter is not None:
                            limiter.consume(len(chunk))
                        inner = (got / first_len) if first_len else 0.5
                        _bump(0, inner)
                    first_resp_cm.__exit__(None, None, None)
                    first_resp_cm = None

                    # Then every remaining URL concatenated into the
                    # same file — for DASH hi-res these are per-segment
                    # binary chunks that form a valid FLAC once joined.
                    for i, url in enumerate(urls[1:], start=1):
                        self._check_cancel(item.item_id)
                        with SESSION.get(url, stream=True, timeout=60) as resp:
                            resp.raise_for_status()
                            seg_len = int(resp.headers.get("Content-Length", 0))
                            seg_got = 0
                            for chunk in resp.iter_content(chunk_size=65536):
                                if not chunk:
                                    continue
                                self._check_cancel(item.item_id)
                                f.write(chunk)
                                seg_got += len(chunk)
                                bytes_total += len(chunk)
                                _AGGREGATE_LIMITER.consume(len(chunk))
                                if limiter is not None:
                                    limiter.consume(len(chunk))
                                inner = (seg_got / seg_len) if seg_len else 0.5
                                _bump(i, inner)
            finally:
                if first_resp_cm is not None:
                    try:
                        first_resp_cm.__exit__(None, None, None)
                    except Exception:
                        pass

            if needs_flac_remux:
                # Strip the FLAC stream out of the MP4 container into a
                # native .flac file. Stream-copy via PyAV — no decode /
                # encode, output is bit-identical to what Tidal sent.
                # Lands in a .flac.part intermediate so the atomic rename
                # below still gives skip-existing an all-or-nothing view.
                flac_part = out_path.with_suffix(".flac.part")
                try:
                    _remux_mp4_to_flac(tmp_path, flac_part)
                except Exception as exc:
                    # Clean up both intermediates so a retry starts fresh.
                    for stray in (tmp_path, flac_part):
                        try:
                            stray.unlink(missing_ok=True)
                        except Exception:
                            pass
                    raise RuntimeError(
                        f"FLAC remux failed: {type(exc).__name__}: {exc}"
                    ) from exc
                # Drop the MP4 intermediate; the .flac.part now holds the
                # canonical bytes and is what we atomic-rename into place.
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                tmp_path = flac_part

            # Atomic rename — the next skip-existing scan sees a complete file
            # or nothing at all.
            tmp_path.replace(out_path)
            tmp_path = None

            item.progress = 1.0
            item.status = DownloadStatus.TAGGING
            # Zero the throughput readout — the download phase is over,
            # tagging doesn't transfer bytes, and a stale "12 MB/s" on
            # a row that's no longer downloading would be misleading.
            item.speed_bps = 0.0
            self._publish_update(item)

            # Tagging is best-effort after the atomic rename. If we
            # let a tag/cover failure bubble up to FAILED, the next
            # retry would hit skip_existing on the already-complete
            # audio file and silently decline to re-download, leaving
            # the user with an untagged track and a stuck FAILED row.
            from app.metadata import fetch_cover_art, tag_file
            tag_error: Optional[str] = None
            try:
                cover = fetch_cover_art(album_obj or getattr(track, "album", None))
            except Exception as exc:
                cover = None
                tag_error = f"cover fetch failed: {exc}"
            try:
                tag_file(out_path, track, cover)
            except Exception as exc:
                if not tag_error:
                    tag_error = f"tagging failed: {exc}"

            item.file_path = str(out_path)
            item.status = DownloadStatus.COMPLETE
            if tag_error:
                item.error = tag_error
                print(
                    f"[downloader] tag/cover warning id={item.item_id[:8]} "
                    f"title={item.title!r}: {tag_error}",
                    file=_sys.stderr,
                    flush=True,
                )
            self._publish_update(item)
            tid = getattr(track, "id", None)
            if tid is not None:
                self.on_file_ready(str(tid), out_path)

        except _Cancelled:
            # User cancelled — already removed from the broker's snapshot
            # in cancel(). Just drop the partial and bail silently.
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
            self._track_map_pop(item.item_id)
            with self._cancelled_lock:
                self._cancelled_ids.discard(item.item_id)
            return
        except Exception as exc:
            # Cancel that races with a network error shouldn't leave a
            # FAILED row behind — treat it as a cancel.
            if self._is_cancelled(item.item_id):
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                self._track_map_pop(item.item_id)
                with self._cancelled_lock:
                    self._cancelled_ids.discard(item.item_id)
                return
            # If a mid-stream 429 killed this item, make sure sibling
            # workers also back off — otherwise the next worker pulls an
            # item and gets throttled right away. We still fail the
            # current item (partial download can't be resumed cleanly
            # without Range support), but the queue as a whole pauses.
            if _looks_like_rate_limit(exc):
                self._note_rate_limit(exc, 0)
            print(
                f"[downloader] _download FAILED id={item.item_id[:8]} "
                f"title={item.title!r} exc={exc!r}",
                file=_sys.stderr,
                flush=True,
            )
            _tb.print_exc(file=_sys.stderr)
            item.status = DownloadStatus.FAILED
            item.error = str(exc)
            item.speed_bps = 0.0
            self._publish_update(item)
            # Clean up a partial file so the next attempt starts fresh and
            # skip-existing can't be fooled by it.
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
            # Keep track_map entry so retry() still works for failed items.
            return

        print(
            f"[downloader] _download DONE id={item.item_id[:8]} title={item.title!r}",
            file=_sys.stderr,
            flush=True,
        )
        # Drop the cached tidalapi reference on success so long sessions
        # don't grow unbounded.
        self._track_map_pop(item.item_id)

    def _fetch_stream_sources(
        self, track, quality: Optional[str], item_id: Optional[str] = None
    ) -> tuple[list[str], Optional[str], Optional[str]]:
        """Fetch the list of URLs we need to download, a file-extension
        hint, and the audio codec. Handles both session types:

        * Device-code sessions use `track.get_url()` — a single streamable
          URL. One entry in the returned list, no extension or codec hint.
        * PKCE sessions can't call get_url (tidalapi raises URLNotAvailable
          immediately). They use `track.get_stream()` which returns a
          manifest (MPEG-DASH or BTS) whose `urls` is a list of segment
          URLs. For DASH hi-res content that list may have dozens of
          short segments which we concatenate into one FLAC file. The
          manifest also carries a file_extension hint and a codec name.

        The codec is the source of truth for what's actually inside the
        bytes — tidalapi's file_extension hint looks at the URL string
        and labels DASH FLAC segments as `.m4a` (because the segments
        are fragmented MP4 containers). The downloader uses the codec
        to override that hint when needed and remux to native FLAC.

        Both paths retry once on auth error with a forced token refresh
        since tidalapi's built-in refresh triggers only on a very
        specific error message Tidal doesn't always send.
        """
        override: Optional[tidalapi.Quality] = None
        if quality:
            try:
                override = tidalapi.Quality[quality]
            except KeyError:
                override = None

        def _call() -> tuple[list[str], Optional[str], Optional[str]]:
            with self.quality_lock:
                original = self.tidal.session.config.quality
                try:
                    if override is not None:
                        self.tidal.session.config.quality = override
                    if getattr(self.tidal.session, "is_pkce", False):
                        # PKCE path: manifest-based stream.
                        stream = track.get_stream()
                        manifest = stream.get_stream_manifest()
                        if getattr(manifest, "is_encrypted", False):
                            # Encrypted streams would need per-segment
                            # decryption keys we don't have. Refuse
                            # loudly rather than write a corrupt file.
                            raise RuntimeError(
                                "Tidal returned an encrypted stream we can't decrypt"
                            )
                        ext_hint = getattr(manifest, "file_extension", None)
                        codec = getattr(manifest, "codecs", None)
                        codec = codec.upper() if isinstance(codec, str) else None
                        return (list(manifest.urls or []), ext_hint, codec)
                    # Device-code path: single direct URL.
                    return ([track.get_url()], None, None)
                finally:
                    if override is not None:
                        self.tidal.session.config.quality = original

        import sys as _sys

        # Outer loop handles 429 backoff; auth-error retry stays one-shot
        # and nested below since a refresh either fixes the token or it
        # doesn't — no point looping on it.
        last_exc: Optional[Exception] = None
        for attempt in range(RATE_LIMIT_MAX_ATTEMPTS):
            # Pass item_id through so a user cancel during a rate-limit
            # sleep interrupts the wait promptly — without it, cancel
            # would only be honored after the full Retry-After elapsed.
            self._wait_for_rate_limit(item_id)
            try:
                return _call()
            except Exception as exc:
                last_exc = exc
                if _looks_like_rate_limit(exc):
                    self._note_rate_limit(exc, attempt)
                    continue
                print(
                    f"[downloader] _fetch_stream_sources FAILED track_id="
                    f"{getattr(track, 'id', '?')} quality_override={quality!r} "
                    f"exc={exc!r}",
                    file=_sys.stderr,
                    flush=True,
                )
                if _looks_like_auth_error(exc):
                    refresh = getattr(self.tidal, "force_refresh", None)
                    if callable(refresh) and refresh():
                        print(
                            "[downloader] _fetch_stream_sources retrying after refresh",
                            file=_sys.stderr,
                            flush=True,
                        )
                        return _call()
                raise
        # Exhausted the rate-limit retry budget — surface the last 429.
        assert last_exc is not None
        raise last_exc

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _remux_mp4_to_flac(mp4_path: Path, flac_path: Path) -> None:
    """Stream-copy a FLAC audio stream out of an MP4 container into a
    native .flac file.

    No decode, no encode — libav reads packets out of the MP4 demuxer
    and hands them straight to the FLAC muxer. Output bytes are
    bit-identical to the FLAC frames Tidal sent; the only thing that
    changes is the container around them.

    Same PyAV idiom as `app.video_downloader._remux_hls_to_mp4`. PyAV
    is already a hard dependency of the audio engine, so no new
    install requirements.

    Raises on any failure — the caller cleans up the intermediates and
    surfaces the error to the user.
    """
    import av  # local import: keeps module load cheap if remux is never needed

    input_container = av.open(str(mp4_path))
    try:
        if not input_container.streams.audio:
            raise RuntimeError("MP4 input has no audio stream")
        in_stream = input_container.streams.audio[0]
        output_container = av.open(str(flac_path), mode="w", format="flac")
        try:
            out_stream = output_container.add_stream_from_template(in_stream)
            for packet in input_container.demux(in_stream):
                # Flush packets from libav have no DTS — skip them, same
                # as the video remux path.
                if packet.dts is None:
                    continue
                packet.stream = out_stream
                output_container.mux(packet)
        finally:
            output_container.close()
    finally:
        input_container.close()


def _looks_like_rate_limit(exc: Exception) -> bool:
    """Detect a 429 Too Many Requests. Same dual-path logic as
    `_looks_like_auth_error` — HTTPError carries the response, tidalapi
    sometimes re-raises with the code in the message string."""
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True
    msg = str(exc)
    return "429" in msg or "Too Many Requests" in msg


def _extract_retry_after(exc: Exception) -> Optional[float]:
    """Pull a Retry-After header off a 429 response if present.
    Supports both numeric-seconds and HTTP-date forms, though Tidal only
    ever sends seconds in practice. Returns None if we can't read one —
    callers fall back to a default backoff table."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    headers = getattr(resp, "headers", None) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    try:
        # Clamp to 0 — a misconfigured proxy could send a negative value
        # and we don't want to treat "wait -5 seconds" as "fire now" via
        # some later sign-flip. Spec says non-negative integer only.
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    # HTTP-date form — rare from Tidal but spec-allowed. Fall back to
    # requests' own utility rather than parsing it ourselves.
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone

        dt = parsedate_to_datetime(raw)
        if dt is None:
            return None
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


def _looks_like_auth_error(exc: Exception) -> bool:
    """Best-effort detection of a Tidal 401/auth error so we can trigger
    a token refresh and retry. `requests.HTTPError` carries the response
    with a status code; tidalapi sometimes re-raises as plain RuntimeError
    whose string includes "401". Either path is recognized here.
    """
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) in (401, 403):
        return True
    msg = str(exc)
    return "401" in msg or "Unauthorized" in msg


def _populate_item_from_track(
    item: DownloadItem, track, album_obj, quality: Optional[str]
) -> None:
    """Single source of truth for setting a DownloadItem's metadata
    from tidalapi objects. Called from every enqueue path so adding a
    new template token only needs one wiring change here, not three."""
    resolved_album = album_obj or getattr(track, "album", None)
    item.title = track.name
    item.artist = _artist_names(track)
    item.album = _album_name(resolved_album)
    item.track_num = getattr(track, "track_num", 0)
    item.quality = quality
    item.album_artist = _album_artist_name(resolved_album, track)
    item.year = _album_year(resolved_album)
    item.disc_num = getattr(track, "volume_num", 0) or 0
    item.track_explicit = bool(getattr(track, "explicit", False))
    item.album_explicit_flag = bool(getattr(resolved_album, "explicit", False))


def _artist_names(track) -> str:
    try:
        return ", ".join(a.name for a in track.artists)
    except Exception:
        pass
    try:
        return track.artist.name
    except Exception:
        return ""


def _album_name(album_obj) -> str:
    try:
        return album_obj.name
    except Exception:
        return ""


def _album_artist_name(album_obj, track) -> str:
    """Return the album-level artist (single name), falling back to the
    track's primary artist when the album object doesn't carry one.

    This matters most for compilations / VA releases / collabs, where
    the track artist is just a contributor and the album artist is the
    canonical credit (e.g. "Various Artists" or "DJ Khaled" while
    individual tracks list a different artist). Without a separate
    album-artist token, library scanners group every contributor's
    track under its own artist tree instead of one album folder.
    """
    try:
        if album_obj is not None and getattr(album_obj, "artist", None):
            name = getattr(album_obj.artist, "name", None)
            if name:
                return name
    except Exception:
        pass
    try:
        if track is not None and getattr(track, "artist", None):
            return getattr(track.artist, "name", "") or ""
    except Exception:
        pass
    return ""


def _album_year(album_obj) -> Optional[int]:
    """Year an album was released, parsed off whichever date field
    tidalapi populated. `release_date` is the editorial release date;
    `tidal_release_date` is when Tidal first hosted the stream — for
    most catalog music these match, for back-catalog reissues the
    editorial date is what users want in their folder names."""
    if album_obj is None:
        return None
    for attr in ("release_date", "tidal_release_date"):
        try:
            dt = getattr(album_obj, attr, None)
            if dt is not None and getattr(dt, "year", None):
                return int(dt.year)
        except Exception:
            continue
    return None


def _ext_from_response(resp) -> str:
    ct = resp.headers.get("Content-Type", "").lower()
    if "flac" in ct:
        return ".flac"
    if "mp4" in ct or "m4a" in ct or "aac" in ct:
        return ".m4a"
    if "mpeg" in ct or "mp3" in ct:
        return ".mp3"
    url = resp.url.lower().split("?")[0]
    for ext in (".flac", ".m4a", ".mp3", ".mp4"):
        if url.endswith(ext):
            return ext
    return ".flac"


_WIN_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_MAX_SEGMENT = 180  # well under 255 to leave room for extensions and ancestors


def _sanitize_segment(name: str) -> str:
    """Make a single path segment safe on macOS, Linux, and Windows."""
    if not name:
        return "_"
    # Strip forbidden chars + control bytes.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    # Windows: trailing dots/spaces are stripped by the shell, which breaks
    # round-tripping. Strip them ourselves.
    name = name.rstrip(". ")
    # Windows: certain stems are reserved regardless of extension.
    stem = name.split(".", 1)[0].upper()
    if stem in _WIN_RESERVED:
        name = f"_{name}"
    # Hard length cap (bytes ≤ 255 on most filesystems).
    if len(name.encode("utf-8", errors="ignore")) > _MAX_SEGMENT:
        name = name.encode("utf-8")[:_MAX_SEGMENT].decode("utf-8", errors="ignore").rstrip()
    return name or "_"


class _SafeTokens(dict):
    """str.format_map dict that returns the literal "{key}" for any
    token the template references but the renderer doesn't supply.
    Keeps a typo'd token (e.g. "{albmu}") visible in the output
    filename so the user notices instead of silently dropping the
    request, and never crashes the download with a KeyError."""

    def __missing__(self, key):  # type: ignore[override]
        return "{" + key + "}"


def _explicit_marker(flag: bool) -> str:
    """Render an explicit-content flag as a path-safe marker. Leading
    space so users can write `{title}{explicit}` without an extra
    separator and still get readable output. Empty when the track or
    album isn't flagged so non-explicit items don't carry a trailing
    space."""
    return " [E]" if flag else ""


def _render_template(template: str, item: DownloadItem) -> str:
    """Interpolate the user's filename_template with sanitized token
    values. Each token's VALUE is sanitized for path-unsafe chars
    before rendering — that's how we keep an album literally named
    "AC/DC" from creating a phantom subdirectory. The rendered string
    may still contain `/` from the TEMPLATE itself; the caller treats
    those as intentional directory separators.

    Unknown tokens render as the literal `{key}` (see _SafeTokens) so
    the user sees a wonky filename and fixes their template instead of
    the download just dying with a KeyError mid-batch.
    """
    year_str = "" if item.year is None else str(item.year)
    return template.format_map(
        _SafeTokens(
            title=_sanitize_segment(item.title),
            track_title=_sanitize_segment(item.title),
            artist=_sanitize_segment(item.artist),
            album=_sanitize_segment(item.album),
            album_title=_sanitize_segment(item.album),
            album_artist=_sanitize_segment(item.album_artist or item.artist),
            track_num=str(item.track_num).zfill(2),
            disc_num=str(item.disc_num or 1).zfill(2) if item.disc_num else "01",
            year=year_str,
            explicit=_explicit_marker(item.track_explicit),
            album_explicit=_explicit_marker(item.album_explicit_flag),
        )
    )


def _split_template_path(rendered: str) -> list[str]:
    """Split a rendered template into path segments. Forward slash is
    the documented separator; backslash is also accepted because
    Windows users naturally type it and there's no scenario where a
    literal `\\` should appear inside a single filename segment.

    Empty segments (from leading slashes or `//` typos) are dropped so
    the resulting Path doesn't accidentally root itself or carry a
    no-op `.` segment."""
    pieces = re.split(r"[/\\]+", rendered)
    return [p for p in pieces if p]


def _template_has_separator(template: str) -> bool:
    """Does the user's template define its own directory structure? If
    so, `create_album_folders` becomes a no-op — the template is in
    charge. Detected by checking whether any `/` or `\\` appears
    *outside* a token (so `{album}` containing slashes through user
    data doesn't accidentally enable directory mode at the template
    level — that's handled per-segment after rendering)."""
    stripped = re.sub(r"\{[^{}]*\}", "", template)
    return "/" in stripped or "\\" in stripped


def _build_path(item: DownloadItem, settings, ext: str) -> Path:
    """Render the user's filename template into an absolute output
    path under settings.output_dir.

    Two-stage sanitization, same intent as before but extended to
    cope with templates that contain `/` separators:

    1. Per-token values are sanitized BEFORE rendering so a literal
       slash inside any tidalapi field collapses to `_`, never
       escaping into the path structure.
    2. The rendered string is split on `/` (and `\\`) into segments;
       each segment is sanitized AGAIN to catch anything weird the
       template's literal text introduced (control bytes, trailing
       dots/spaces, Windows-reserved stems).

    `create_album_folders` is a backward-compat shortcut for users who
    haven't customized their template — it prepends an album folder
    only when the template is single-segment. Once the user adopts a
    template with `/`, the template controls structure and the toggle
    is silent (avoid double-nesting like
    `output_dir/AlbumName/AlbumName/Track.flac`).
    """
    rendered = _render_template(settings.filename_template, item)
    segments = _split_template_path(rendered)
    # Render produced nothing usable (template was all-whitespace or
    # all-empty-tokens). Fall back to a stable identifier so the
    # download lands somewhere instead of crashing on a zero-length
    # filename — `_` is what _sanitize_segment would have produced for
    # an empty string anyway.
    if not segments:
        segments = ["_"]
    safe_segments = [_sanitize_segment(s) for s in segments]

    base = Path(settings.output_dir)
    if (
        settings.create_album_folders
        and item.album
        and not _template_has_separator(settings.filename_template)
    ):
        base = base / _sanitize_segment(item.album)

    *dirs, last = safe_segments
    final = base
    for d in dirs:
        final = final / d
    final = final / (last + ext)

    # Hard containment check: after all the sanitization, the resolved
    # path must still live under output_dir. If it somehow doesn't, a
    # future regression introduced a vector we missed — fail loudly
    # rather than silently write outside the sandbox.
    try:
        root = Path(settings.output_dir).resolve()
        resolved = final.resolve()
        if resolved != root and root not in resolved.parents:
            raise RuntimeError(f"Resolved path escaped output_dir: {final}")
    except RuntimeError:
        raise
    except Exception:
        # resolve() can fail on not-yet-created paths on some FSes;
        # fall through — the parent mkdir will surface real issues.
        pass
    return final


def _find_existing(item: DownloadItem, settings) -> Optional[Path]:
    """Return the path of an already-downloaded file for this item, if any."""
    candidate = _build_path(item, settings, ".flac")
    parent = candidate.parent
    stem = candidate.stem
    if not parent.exists():
        return None
    try:
        for child in parent.iterdir():
            if child.is_file() and child.stem == stem and child.suffix.lower() in (".flac", ".m4a", ".mp3", ".mp4"):
                return child
    except Exception:
        return None
    return None
