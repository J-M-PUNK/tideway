#!/usr/bin/env python3
"""Phase 4 smoke test — gapless transition via preload + swap.

Plays favourite #0. 3 seconds in, preloads favourite #1. Seeks the
current track to within 6 seconds of the end. Waits for the natural
transition and reports whether the sample rate matched (gapless
swap inside the callback) or differed (bridge with a ~50ms reopen).
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
    last_track = None
    while time.monotonic() < deadline:
        time.sleep(0.5)
        snap = player.snapshot()
        marker = ""
        if snap.track_id != last_track:
            marker = " <<< TRACK CHANGE"
            last_track = snap.track_id
        print(
            f"    state={snap.state:8s} "
            f"track={snap.track_id} "
            f"pos={snap.position_ms:6d}ms / {snap.duration_ms:6d}ms"
            f"{marker}"
        )


def main() -> int:
    client = TidalClient()
    if not client.load_session():
        print("no session; log in via the app first", file=sys.stderr)
        return 1
    favs = client.get_favorite_tracks()
    if len(favs) < 2:
        print("need at least 2 favourite tracks", file=sys.stderr)
        return 2
    t0, t1 = favs[0], favs[1]
    quality = sys.argv[1] if len(sys.argv) > 1 else "hi_res_lossless"
    print(f"Track A: {getattr(t0, 'name', '?')} ({t0.id})  "
          f"Track B: {getattr(t1, 'name', '?')} ({t1.id})  quality={quality}")

    player = PCMPlayer(lambda: client.session)

    def listener(snap):
        print(f"  [evt] state={snap.state} track={snap.track_id} seq={snap.seq}")

    player.subscribe(listener)

    print("\nload + play A …")
    snap = player.load(str(t0.id), quality=quality)
    if snap.state == "error":
        print(f"  load failed: {snap.error}")
        return 3
    player.play()
    wait(3.0, player, "A playing")

    print("\npreload B …")
    res = player.preload(str(t1.id), quality=quality)
    print(f"  preload result: {res}")

    print("\nseek A to 6s before end …")
    if snap.duration_ms > 0:
        target_fraction = max(
            0.0,
            (snap.duration_ms - 6000) / snap.duration_ms,
        )
    else:
        target_fraction = 0.95
    player.seek(target_fraction)

    wait(10.0, player, "listening for the transition")

    print("\nstop")
    player.stop()
    time.sleep(0.3)
    snap = player.snapshot()
    print(f"  final state={snap.state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
