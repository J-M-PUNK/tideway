#!/usr/bin/env python3
"""Phase 1 probe: can PyAV demux + decode Tidal DASH MPDs directly?

Logs into Tidal using the saved session, picks a track from the
user's favorites, resolves the DASH manifest at each quality tier,
writes the MPD to a temp file, and asks PyAV to decode ~5 seconds
of audio from it. Reports codec, sample rate, channels, and any
errors.

Outcome decides Phase 2 of the PyAV migration plan:
  - All tiers succeed -> feed MPDs to PyAV directly.
  - Any tier fails with "demuxer couldn't parse" or similar ->
    pre-assemble segments using tidalapi's manifest.urls list
    before handing them to PyAV.
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import time

# Make app.* importable when run directly from the scripts/ dir.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import av  # type: ignore
import tidalapi

from app.tidal_client import TidalClient


QUALITIES = ("low_96k", "low_320k", "high_lossless", "hi_res_lossless")


def resolve_mpd(client: TidalClient, track_id: int, quality: str):
    session = client.session
    try:
        override = tidalapi.Quality[quality]
    except KeyError as exc:
        raise RuntimeError(f"unknown quality {quality!r}") from exc
    original = session.config.quality
    session.config.quality = override
    try:
        track = session.track(track_id)
        stream = track.get_stream()
        manifest = stream.get_stream_manifest()
        raw = getattr(manifest, "manifest", None)
        if not raw:
            raise RuntimeError("empty manifest from Tidal")
        mpd_bytes = base64.b64decode(raw) if isinstance(raw, str) else raw
        return mpd_bytes, stream, manifest
    finally:
        session.config.quality = original


def probe_mpd(mpd_bytes: bytes, decode_seconds: float = 5.0) -> dict:
    """Write MPD to a temp file, open with PyAV, decode N seconds.

    PyAV inherits libav's DASH demuxer. If that works with Tidal's
    segment URLs (which are absolute https with embedded auth
    tokens), we're done — no per-segment assembly needed.
    """
    fd, path = tempfile.mkstemp(suffix=".mpd", prefix="probe-")
    with os.fdopen(fd, "wb") as f:
        f.write(mpd_bytes)
    result: dict = {"ok": False}
    started = time.monotonic()
    try:
        with av.open(path) as container:
            audio = next(
                (s for s in container.streams if s.type == "audio"), None
            )
            if audio is None:
                result["error"] = "no audio stream in container"
                return result
            cc = audio.codec_context
            result["codec"] = cc.name
            result["sample_rate"] = cc.sample_rate
            # PyAV 17 uses channel_layout; fall back to channels for older.
            channels = getattr(cc, "channels", None)
            if channels is None:
                layout = getattr(cc, "layout", None) or getattr(cc, "channel_layout", None)
                channels = getattr(layout, "nb_channels", None) or getattr(layout, "channels", None)
            result["channels"] = channels
            fmt = getattr(cc, "format", None)
            result["format"] = getattr(fmt, "name", None)
            frames = 0
            decoded = 0.0
            for frame in container.decode(audio):
                frames += 1
                if frame.sample_rate:
                    decoded = (frames * frame.samples) / frame.sample_rate
                if decoded >= decode_seconds:
                    break
            result["frames"] = frames
            result["decoded_seconds"] = round(decoded, 3)
            result["wall_clock_seconds"] = round(time.monotonic() - started, 3)
            result["ok"] = True
            return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def main() -> int:
    client = TidalClient()
    if not client.load_session():
        print("No saved Tidal session. Open the app once to log in.", file=sys.stderr)
        return 1

    favs = client.get_favorite_tracks()
    if not favs:
        print("No favorite tracks on this account — favourite any song to run the probe.", file=sys.stderr)
        return 2
    track = favs[0]
    track_id = int(track.id)
    track_title = getattr(track, "name", "?")
    artist = getattr(track, "artist", None)
    artist_name = getattr(artist, "name", "?") if artist else "?"
    print(f"Probe track: {track_title} — {artist_name} (id={track_id})")

    summary: list[tuple[str, bool, str]] = []
    for quality in QUALITIES:
        print(f"\n=== quality={quality} ===")
        try:
            mpd_bytes, _stream, manifest = resolve_mpd(client, track_id, quality)
        except Exception as exc:
            print(f"  resolve FAILED: {type(exc).__name__}: {exc}")
            summary.append((quality, False, "resolve failed"))
            continue
        codecs = getattr(manifest, "codecs", None)
        mime = getattr(manifest, "mime_type", None)
        print(
            f"  manifest: {len(mpd_bytes)} bytes  codecs={codecs!r}  "
            f"mime={mime!r}  encrypted={getattr(manifest, 'is_encrypted', '?')}"
        )
        r = probe_mpd(mpd_bytes)
        if r.get("ok"):
            print(
                f"  OK  codec={r['codec']}  rate={r['sample_rate']}  "
                f"channels={r['channels']}  format={r['format']}  "
                f"frames={r['frames']}  decoded={r['decoded_seconds']}s  "
                f"wall={r['wall_clock_seconds']}s"
            )
            summary.append((quality, True, r["codec"]))
        else:
            print(f"  FAIL  {r.get('error')}")
            summary.append((quality, False, r.get("error", "unknown")))

    print("\n--- summary ---")
    ok_count = 0
    for q, ok, note in summary:
        tag = "PASS" if ok else "FAIL"
        print(f"  {tag}  {q:<18}  {note}")
        if ok:
            ok_count += 1
    print(f"\n{ok_count}/{len(summary)} qualities decoded cleanly.")
    return 0 if ok_count == len(summary) else 3


if __name__ == "__main__":
    sys.exit(main())
