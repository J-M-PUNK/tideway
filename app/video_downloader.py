"""Save a Tidal music video to disk.

Standalone from the main track downloader — videos are rare requests
and wiring them into the item-status machinery would add complexity
for a feature most users hit a handful of times. Instead we keep a
tiny in-memory dict of in-flight jobs keyed by video_id; the UI polls
`status()` to render busy / done / error.

Mechanism: ffmpeg remux of the HLS manifest. Tidal delivers a master
.m3u8; ffmpeg picks the highest variant (or whichever `quality` resolves
to), concatenates the TS segments, and writes an .mp4 with `-c copy`
(no re-encode, so the output is the pristine stream). Requires ffmpeg
on PATH — documented in README, same requirement as audio concat.
"""
from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)


@dataclass
class VideoJob:
    video_id: int
    state: str  # "running" | "done" | "error"
    title: str
    artist: str
    output_path: Optional[str] = None
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    # Last-parsed ffmpeg progress (0.0..1.0). Read from stderr's
    # `out_time_ms=` line in `-progress pipe:2` mode. Null until the
    # first progress line arrives.
    progress: Optional[float] = None


_jobs: dict[int, VideoJob] = {}
_jobs_lock = threading.Lock()


def status(video_id: int) -> Optional[dict]:
    with _jobs_lock:
        job = _jobs.get(video_id)
        if job is None:
            return None
        return {
            "video_id": job.video_id,
            "state": job.state,
            "title": job.title,
            "artist": job.artist,
            "output_path": job.output_path,
            "error": job.error,
            "progress": job.progress,
        }


def list_all() -> list[dict]:
    with _jobs_lock:
        return [
            {
                "video_id": j.video_id,
                "state": j.state,
                "title": j.title,
                "artist": j.artist,
                "output_path": j.output_path,
                "error": j.error,
                "progress": j.progress,
            }
            for j in _jobs.values()
        ]


def start(
    *,
    video_id: int,
    manifest_url: str,
    title: str,
    artist: str,
    output_dir: Path,
    duration_s: Optional[float] = None,
    on_done: Optional[Callable[[VideoJob], None]] = None,
) -> VideoJob:
    """Kick off a background ffmpeg job and return the initial state.

    Idempotent per `video_id`: if a job is already running or done, we
    return that existing record rather than starting a second one.
    Errors on the previous job do NOT block a retry — the user can
    click again to re-attempt.
    """
    # Opportunistic GC — drop terminal jobs older than 24h before
    # adding a new one. Keeps _jobs bounded over long-running sessions
    # without a separate reaper thread.
    _prune(24 * 3600)
    with _jobs_lock:
        existing = _jobs.get(video_id)
        if existing is not None and existing.state in ("running", "done"):
            return existing
        job = VideoJob(
            video_id=video_id, state="running", title=title, artist=artist
        )
        _jobs[video_id] = job

    t = threading.Thread(
        target=_run_job,
        args=(job, manifest_url, output_dir, duration_s, on_done),
        daemon=True,
        name=f"video-dl-{video_id}",
    )
    t.start()
    return job


def _safe_filename(name: str) -> str:
    """Strip characters macOS / Windows filesystems choke on."""
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad else c for c in name).strip()
    return out or "video"


def _hls_duration_seconds(
    manifest_url: str, _depth: int = 0
) -> Optional[float]:
    """Return the total duration of an HLS stream by summing EXTINFs.

    Tidal serves either a master playlist (with `#EXT-X-STREAM-INF`
    pointers to variant playlists) or a media playlist directly. We
    handle both: for a master, pick the first variant and recurse;
    for a media playlist, sum every `#EXTINF:<seconds>` line.

    `tidalapi.Video` doesn't expose a duration field, so without this
    the progress fraction can't be computed and the UI stays stuck at
    an indeterminate spinner. Parsing the manifest is cheap (<10 KB
    text fetch) and runs before ffmpeg even starts.

    `_depth` bounds recursion at 3. A legitimate HLS chain is at
    most master → variant (depth 1); anything deeper is a malformed
    or malicious playlist and we give up rather than stack-overflow.
    """
    if _depth > 3:
        log.debug("hls duration probe: recursion cap hit at %s", manifest_url)
        return None
    try:
        with urllib.request.urlopen(manifest_url, timeout=5) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.debug("hls duration probe failed: %s", exc)
        return None
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    # Master playlist — pick the first variant and recurse once.
    if any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines):
        for i, ln in enumerate(lines):
            if ln.startswith("#EXT-X-STREAM-INF"):
                # The variant URI is on the *next* non-empty line.
                for j in range(i + 1, len(lines)):
                    uri = lines[j]
                    if uri.startswith("#"):
                        continue
                    variant_url = urllib.parse.urljoin(manifest_url, uri)
                    return _hls_duration_seconds(variant_url, _depth + 1)
        return None
    # Media playlist — sum segment durations. Negative / zero EXTINFs
    # are either spec violations or intentional noise; skip them so
    # a forged playlist can't yield a negative progress fraction.
    total = 0.0
    for ln in lines:
        if ln.startswith("#EXTINF:"):
            # Format: #EXTINF:<duration>,<optional title>
            rest = ln.split(":", 1)[1]
            num = rest.split(",", 1)[0].strip()
            try:
                secs = float(num)
            except ValueError:
                continue
            if secs > 0:
                total += secs
    return total if total > 0 else None


def _find_ffmpeg() -> Optional[str]:
    """Locate an ffmpeg executable that this process can actually run.

    Lookup order:
      1. Bundled binary inside the packaged .app / .exe (written by
         PyInstaller from `vendor/ffmpeg/<os>/` during build). This
         is the shipping path — end users never have to install
         anything.
      2. `shutil.which("ffmpeg")` — respects PATH. Fast path for
         dev runs where the terminal's PATH includes Homebrew /
         system install dirs.
      3. Known install locations on each platform. Covers GUI-
         launched .apps on macOS that inherit a minimal PATH
         (typically just `/usr/bin:/bin:/usr/sbin:/sbin`) and can't
         see Homebrew's `/opt/homebrew/bin` even when ffmpeg is
         installed.
    """
    # (1) Bundled binary. PyInstaller sets sys._MEIPASS to the
    # runtime directory where datas/binaries are extracted.
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            exe_name = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
            bundled = Path(meipass) / "ffmpeg" / exe_name
            if bundled.is_file():
                return str(bundled)
    # (2) PATH.
    resolved = shutil.which("ffmpeg")
    if resolved:
        return resolved
    # (3) Well-known install dirs.
    candidates: list[str] = []
    if sys.platform == "darwin":
        candidates = [
            "/opt/homebrew/bin/ffmpeg",  # Apple Silicon Homebrew
            "/usr/local/bin/ffmpeg",  # Intel Homebrew
            "/opt/local/bin/ffmpeg",  # MacPorts
        ]
    elif sys.platform.startswith("win"):
        candidates = [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        ]
    else:
        candidates = ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]
    for path in candidates:
        if Path(path).is_file():
            return path
    return None


def _run_job(
    job: VideoJob,
    manifest_url: str,
    output_dir: Path,
    duration_s: Optional[float],
    on_done: Optional[Callable[[VideoJob], None]],
) -> None:
    try:
        # Resolve ffmpeg's absolute path up front. Packaged macOS
        # apps don't inherit the shell's PATH, so "ffmpeg" alone
        # fails even when Homebrew has installed it at
        # /opt/homebrew/bin. _find_ffmpeg covers that and the other
        # common install locations.
        ffmpeg_path = _find_ffmpeg()
        if ffmpeg_path is None:
            raise FileNotFoundError("ffmpeg")
        # Duration from the manifest if tidalapi didn't give us one.
        # Without this, the progress fraction can never compute and
        # the UI stays at an indeterminate spinner.
        if duration_s is None or duration_s <= 0:
            duration_s = _hls_duration_seconds(manifest_url)
        output_dir.mkdir(parents=True, exist_ok=True)
        fname = _safe_filename(f"{job.artist} - {job.title}") + ".mp4"
        target = output_dir / fname
        # Avoid clobbering an existing file. If the filename already
        # exists, append a numeric suffix.
        if target.exists():
            stem, suffix = target.stem, target.suffix
            n = 2
            while (output_dir / f"{stem} ({n}){suffix}").exists():
                n += 1
            target = output_dir / f"{stem} ({n}){suffix}"
        # ffmpeg command. -c copy: no re-encode (fastest, lossless).
        # -bsf:a aac_adtstoasc: fix the AAC container glitch HLS
        # commonly produces when muxing into MP4 (otherwise QuickTime
        # may refuse to open the file). -progress pipe:2 sends
        # machine-readable progress to stderr for us to parse.
        cmd = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:2",
            "-nostats",
            "-i",
            manifest_url,
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            "-y",
            str(target),
        ]
        log.info("starting ffmpeg: %s", shlex.join(cmd))
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        # Read progress lines. ffmpeg emits blocks like:
        #   out_time_us=12345000
        #   out_time_ms=12345           (milliseconds in newer ffmpeg,
        #                                microseconds in older — the
        #                                naming has been a long-standing
        #                                bug, so we prefer out_time_us
        #                                which is consistently µs.)
        #   ...
        #   progress=continue
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.strip()
            if not line.startswith("out_time_us=") or duration_s is None:
                continue
            try:
                out_us = int(line.split("=", 1)[1])
                fraction = min(1.0, max(0.0, out_us / 1_000_000 / duration_s))
            except (ValueError, ZeroDivisionError):
                continue
            with _jobs_lock:
                job.progress = fraction
        rc = proc.wait()
        if rc != 0:
            # Surface a useful error — the generic "ffmpeg failed" is
            # useless. ffmpeg already printed lines to stderr before
            # the pipe drained; we don't have a tail here but the
            # exit code rules out a few common causes.
            raise RuntimeError(
                f"ffmpeg exited with code {rc} — check that ffmpeg is "
                f"installed on PATH"
            )
        with _jobs_lock:
            job.state = "done"
            job.output_path = str(target)
            job.progress = 1.0
    except FileNotFoundError:
        with _jobs_lock:
            job.state = "error"
            if sys.platform == "darwin":
                job.error = (
                    "ffmpeg not installed. Install via Homebrew "
                    "(`brew install ffmpeg`) and retry — no restart "
                    "needed."
                )
            elif sys.platform.startswith("win"):
                job.error = (
                    "ffmpeg not installed. Download from "
                    "https://ffmpeg.org/download.html, put ffmpeg.exe "
                    "on your PATH, and retry."
                )
            else:
                job.error = (
                    "ffmpeg not installed. Install via your package "
                    "manager (apt: `sudo apt install ffmpeg`) and retry."
                )
    except Exception as exc:
        log.exception("video download failed")
        with _jobs_lock:
            job.state = "error"
            job.error = str(exc)
    finally:
        if on_done is not None:
            try:
                on_done(job)
            except Exception:
                log.exception("video-download on_done callback raised")


# Opportunistic GC called from `start()` — drops terminal jobs older
# than max_age_s. Keeps _jobs bounded over long app sessions without
# a dedicated reaper thread.
def _prune(max_age_s: float = 3600) -> None:
    now = time.time()
    with _jobs_lock:
        stale = [
            vid
            for vid, j in _jobs.items()
            if j.state in ("done", "error") and (now - j.started_at) > max_age_s
        ]
        for vid in stale:
            _jobs.pop(vid, None)


__all__ = ["VideoJob", "start", "status", "list_all"]
