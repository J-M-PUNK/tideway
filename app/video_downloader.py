"""Save a Tidal music video to disk.

Standalone from the main track downloader — videos are rare
requests and wiring them into the item-status machinery would add
complexity for a feature most users hit a handful of times.
Instead we keep a tiny in-memory dict of in-flight jobs keyed by
video_id; the UI polls `status()` to render busy / done / error.

Mechanism: HLS → MP4 remux via PyAV (same libav under the hood as
ffmpeg). Tidal delivers a master .m3u8; libav's HLS demuxer picks
the highest variant and decodes the segment stream into packets.
We mux those packets straight into an MP4 with no re-encode — so
the output is bit-identical to what Tidal sent — after running
the `aac_adtstoasc` bitstream filter on audio (HLS ships AAC
with ADTS headers; MP4 needs them stripped).
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import av  # type: ignore
from av.bitstream import BitStreamFilterContext  # type: ignore

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
    # Last computed remux progress (0.0..1.0). Updates as packets
    # are muxed. Null until the first packet's PTS arrives.
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
    """Kick off a background remux job and return the initial state.

    Idempotent per `video_id`: if a job is already running or done,
    we return that existing record rather than starting a second
    one. Errors on the previous job do NOT block a retry — the user
    can click again to re-attempt.
    """
    # Opportunistic GC — drop terminal jobs older than 24h before
    # adding a new one. Keeps _jobs bounded over long-running
    # sessions without a separate reaper thread.
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

    `tidalapi.Video` doesn't expose a duration field, so without
    this the progress fraction can't be computed and the UI stays
    stuck at an indeterminate spinner. Parsing the manifest is
    cheap (<10 KB text fetch) and runs before remuxing starts.

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
    # Media playlist — sum segment durations. Negative / zero
    # EXTINFs are either spec violations or intentional noise; skip
    # them so a forged playlist can't yield a negative progress
    # fraction.
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


def _remux_hls_to_mp4(
    manifest_url: str,
    target_path: Path,
    duration_s: Optional[float],
    job: VideoJob,
) -> None:
    """Remux an HLS manifest into an MP4 without re-encoding.

    libav's HLS demuxer handles master / variant resolution + TS
    segment fetch. We hand it each demuxed packet and mux it into
    the output container verbatim (packet-level copy — the output
    is bit-identical to Tidal's stream). Audio runs through the
    `aac_adtstoasc` bitstream filter so ADTS-headered AAC packets
    become ASC-headered, which is what MP4 requires.

    Progress is computed from the current DTS relative to the
    pre-probed manifest duration and updated at 1% granularity to
    avoid lock contention on every packet.
    """
    input_container = av.open(manifest_url)
    output_container = av.open(str(target_path), mode="w", format="mp4")

    # index-in-input → stream-in-output
    stream_map: dict[int, object] = {}
    # index-in-input → BSF (only for AAC audio)
    bsf_map: dict[int, BitStreamFilterContext] = {}

    try:
        for in_stream in input_container.streams:
            out_stream = output_container.add_stream_from_template(in_stream)
            stream_map[in_stream.index] = out_stream
            codec_name = getattr(
                getattr(in_stream, "codec_context", None), "name", None
            )
            if codec_name == "aac":
                bsf_map[in_stream.index] = BitStreamFilterContext(
                    "aac_adtstoasc", in_stream
                )

        last_reported_progress = 0.0
        for packet in input_container.demux():
            if packet.dts is None:
                # Flush packets from libav — skip.
                continue
            idx = packet.stream.index
            out_stream = stream_map.get(idx)
            if out_stream is None:
                continue

            if idx in bsf_map:
                for filtered in bsf_map[idx].filter(packet):
                    filtered.stream = out_stream
                    output_container.mux(filtered)
            else:
                packet.stream = out_stream
                output_container.mux(packet)

            if duration_s and packet.pts is not None:
                try:
                    pts_s = float(packet.pts * packet.time_base)
                except Exception:
                    pts_s = 0.0
                fraction = min(1.0, max(0.0, pts_s / duration_s))
                if fraction - last_reported_progress >= 0.01:
                    with _jobs_lock:
                        job.progress = fraction
                    last_reported_progress = fraction

        # Drain each bitstream filter by sending a None packet.
        for idx, bsf in bsf_map.items():
            out_stream = stream_map[idx]
            for filtered in bsf.filter(None):
                filtered.stream = out_stream
                output_container.mux(filtered)
    finally:
        try:
            output_container.close()
        except Exception:
            pass
        try:
            input_container.close()
        except Exception:
            pass


def _run_job(
    job: VideoJob,
    manifest_url: str,
    output_dir: Path,
    duration_s: Optional[float],
    on_done: Optional[Callable[[VideoJob], None]],
) -> None:
    try:
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

        _remux_hls_to_mp4(manifest_url, target, duration_s, job)

        with _jobs_lock:
            job.state = "done"
            job.output_path = str(target)
            job.progress = 1.0
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


# Opportunistic GC called from `start()` — drops terminal jobs
# older than max_age_s. Keeps _jobs bounded over long app sessions
# without a dedicated reaper thread.
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
