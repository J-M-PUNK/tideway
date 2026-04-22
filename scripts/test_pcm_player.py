#!/usr/bin/env python3
"""Phase 3 smoke test — exercises pause/resume/seek/volume/mute.

Loads the first favourite track, plays for a few seconds, pauses,
resumes, seeks to 50%, changes volume, mutes, unmutes, stops.
Every transition prints a snapshot so you can hear what happens.
"""
from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app.audio.player import PCMPlayer
from app.tidal_client import TidalClient


def wait(seconds: float, player: PCMPlayer, label: str) -> None:
    print(f"\n  --- {label} for {seconds:.1f}s ---")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.5)
        snap = player.snapshot()
        print(
            f"    state={snap.state:8s} "
            f"pos={snap.position_ms:6d}ms / {snap.duration_ms:6d}ms "
            f"vol={snap.volume} muted={snap.muted}"
        )
        if snap.state in ("ended", "error"):
            break


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
    track_id = str(track.id)
    title = getattr(track, "name", "?")
    artist = getattr(getattr(track, "artist", None), "name", "?")
    quality = sys.argv[1] if len(sys.argv) > 1 else "hi_res_lossless"
    print(f"Track: {title} — {artist}  (id={track_id})  quality={quality}")

    player = PCMPlayer(lambda: client.session)

    def listener(snap):
        print(f"  [evt] state={snap.state} seq={snap.seq} pos={snap.position_ms}ms")

    player.subscribe(listener)

    print("\nload + play …")
    snap = player.load(track_id, quality=quality)
    if snap.state == "error":
        print(f"  load failed: {snap.error}")
        return 3
    player.play()
    wait(5.0, player, "playing")

    print("\npause")
    player.pause()
    wait(2.0, player, "paused (should be silent)")

    print("\nresume")
    player.resume()
    wait(3.0, player, "resumed")

    print("\nseek to 50%")
    player.seek(0.5)
    wait(3.0, player, "after seek")

    print("\nvolume 30")
    player.set_volume(30)
    wait(2.0, player, "quiet")

    print("\nvolume 100")
    player.set_volume(100)
    wait(2.0, player, "full volume (bit-perfect)")

    print("\nmute")
    player.set_muted(True)
    wait(2.0, player, "muted")

    print("\nunmute")
    player.set_muted(False)
    wait(2.0, player, "unmuted")

    print("\nstop")
    player.stop()
    time.sleep(0.3)
    snap = player.snapshot()
    print(f"  final state={snap.state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
