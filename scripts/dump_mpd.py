#!/usr/bin/env python3
"""Dump a Tidal MPD to stdout + list the segment URLs tidalapi pre-resolves.

Used once to understand why libav's DASH demuxer rejects these MPDs
and what shape the segment-assembly fallback needs to take.
"""
from __future__ import annotations

import base64
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import tidalapi

from app.tidal_client import TidalClient


def main() -> int:
    client = TidalClient()
    if not client.load_session():
        print("no session", file=sys.stderr)
        return 1
    favs = client.get_favorite_tracks()
    if not favs:
        print("no favs", file=sys.stderr)
        return 2
    track = favs[0]
    track_id = int(track.id)
    quality = sys.argv[1] if len(sys.argv) > 1 else "high_lossless"
    session = client.session
    session.config.quality = tidalapi.Quality[quality]
    tr = session.track(track_id)
    stream = tr.get_stream()
    manifest = stream.get_stream_manifest()
    raw = manifest.manifest
    mpd_bytes = base64.b64decode(raw) if isinstance(raw, str) else raw
    print("=== MPD ===")
    print(mpd_bytes.decode("utf-8", errors="replace"))
    print("\n=== manifest attrs ===")
    for attr in ("codecs", "mime_type", "is_encrypted", "file_extension",
                 "sample_rate", "bit_depth", "encryption_type", "encryption_key"):
        print(f"  {attr}: {getattr(manifest, attr, '<missing>')!r}")
    print("\n=== urls ===")
    urls = list(getattr(manifest, "urls", []) or [])
    print(f"  count: {len(urls)}")
    for i, u in enumerate(urls[:3]):
        print(f"  [{i}] {u[:160]}{'...' if len(u) > 160 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
