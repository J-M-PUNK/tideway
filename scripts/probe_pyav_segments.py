#!/usr/bin/env python3
"""Phase 1 probe (path 2): assemble Tidal DASH segments, feed PyAV.

Fetches the init segment + first N media segments from the URL list
tidalapi pre-resolves, concatenates the bytes, and hands the result
to PyAV as an in-memory fragmented MP4. If PyAV decodes it, Phase 2
can build a proper streaming SegmentReader on top of the same shape.
"""
from __future__ import annotations

import io
import os
import sys
import time

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import av  # type: ignore
import tidalapi

from app.tidal_client import TidalClient


QUALITIES = ("low_96k", "low_320k", "high_lossless", "hi_res_lossless")


def fetch_bytes(url: str) -> bytes:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.content


def probe_segments(urls: list[str], prefix_segments: int = 3, decode_seconds: float = 5.0) -> dict:
    """Concatenate urls[:1+prefix_segments] (init + N media segments)
    into a BytesIO and hand to PyAV. Decode up to decode_seconds of
    audio to confirm the pipeline works end-to-end.
    """
    if not urls:
        return {"ok": False, "error": "empty url list"}
    started = time.monotonic()
    take = urls[: 1 + prefix_segments]
    buf = io.BytesIO()
    for u in take:
        buf.write(fetch_bytes(u))
    total_bytes = buf.tell()
    buf.seek(0)
    fetch_time = time.monotonic() - started
    try:
        container = av.open(buf)
    except Exception as exc:
        return {"ok": False, "error": f"av.open: {type(exc).__name__}: {exc}"}
    try:
        audio = next((s for s in container.streams if s.type == "audio"), None)
        if audio is None:
            return {"ok": False, "error": "no audio stream"}
        cc = audio.codec_context
        channels = getattr(cc, "channels", None)
        if channels is None:
            layout = getattr(cc, "layout", None) or getattr(cc, "channel_layout", None)
            channels = getattr(layout, "nb_channels", None) or getattr(layout, "channels", None)
        fmt = getattr(cc, "format", None)
        result = {
            "ok": True,
            "codec": cc.name,
            "sample_rate": cc.sample_rate,
            "channels": channels,
            "format": getattr(fmt, "name", None),
            "total_bytes": total_bytes,
            "fetch_wall_s": round(fetch_time, 3),
        }
        frames = 0
        decoded = 0.0
        try:
            for frame in container.decode(audio):
                frames += 1
                if frame.sample_rate:
                    decoded = (frames * frame.samples) / frame.sample_rate
                if decoded >= decode_seconds:
                    break
        except Exception as exc:
            result["decode_error"] = f"{type(exc).__name__}: {exc}"
        result["frames"] = frames
        result["decoded_seconds"] = round(decoded, 3)
        result["wall_clock_seconds"] = round(time.monotonic() - started, 3)
        return result
    finally:
        container.close()


def main() -> int:
    client = TidalClient()
    if not client.load_session():
        print("no session; log in via the app first", file=sys.stderr)
        return 1
    favs = client.get_favorite_tracks()
    if not favs:
        print("no favorite tracks", file=sys.stderr)
        return 2
    track = favs[0]
    track_id = int(track.id)
    print(f"Probe track: {getattr(track, 'name', '?')} (id={track_id})")

    for quality in QUALITIES:
        print(f"\n=== quality={quality} ===")
        session = client.session
        try:
            session.config.quality = tidalapi.Quality[quality]
            tr = session.track(track_id)
            stream = tr.get_stream()
            manifest = stream.get_stream_manifest()
            urls = list(getattr(manifest, "urls", []) or [])
        except Exception as exc:
            print(f"  resolve FAILED: {type(exc).__name__}: {exc}")
            continue
        print(f"  segments: {len(urls)}, codec={getattr(manifest, 'codecs', '?')}")
        r = probe_segments(urls)
        if r.get("ok"):
            print(
                f"  OK  codec={r['codec']}  rate={r['sample_rate']}  "
                f"channels={r['channels']}  format={r['format']}  "
                f"frames={r['frames']}  decoded={r['decoded_seconds']}s  "
                f"fetch={r['fetch_wall_s']}s  total_wall={r['wall_clock_seconds']}s  "
                f"bytes={r['total_bytes']}"
            )
            if "decode_error" in r:
                print(f"  decode_error: {r['decode_error']}")
        else:
            print(f"  FAIL  {r.get('error')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
