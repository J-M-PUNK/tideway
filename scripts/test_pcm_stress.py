#!/usr/bin/env python3
"""Stress test — rapid concurrent play_track / seek / preload calls
to reproduce the 'generator already executing' race the user hit in
the real app. Runs through a burst of 10 overlapping track loads and
asserts nothing crashes.
"""
from __future__ import annotations

import os
import random
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app.audio.player import PCMPlayer
from app.tidal_client import TidalClient


def main() -> int:
    client = TidalClient()
    if not client.load_session():
        print("no session; log in via the app first", file=sys.stderr)
        return 1
    favs = client.get_favorite_tracks()
    if len(favs) < 2:
        print("need at least 2 favourite tracks", file=sys.stderr)
        return 2
    quality = sys.argv[1] if len(sys.argv) > 1 else "high_lossless"
    ids = [str(t.id) for t in favs[:3]]
    print(f"tracks: {ids}  quality={quality}")

    player = PCMPlayer(lambda: client.session)

    errors: list[str] = []

    def worker(action: str, delay: float) -> None:
        time.sleep(delay)
        try:
            if action == "play":
                tid = random.choice(ids)
                player.play_track(tid, quality=quality)
            elif action == "preload":
                tid = random.choice(ids)
                player.preload(tid, quality=quality)
            elif action == "seek":
                player.seek(random.random())
            elif action == "stop":
                player.stop()
        except Exception as exc:
            errors.append(f"{action}: {type(exc).__name__}: {exc}")

    threads: list[threading.Thread] = []
    # Fire a burst: 10 play_tracks, 3 seeks, 2 preloads, in quick
    # succession. Some overlap, some don't — mimicking the real
    # app's rapid-click behavior.
    for i in range(10):
        threads.append(threading.Thread(target=worker, args=("play", i * 0.1)))
    for i in range(3):
        threads.append(threading.Thread(target=worker, args=("seek", 0.3 + i * 0.2)))
    for i in range(2):
        threads.append(threading.Thread(target=worker, args=("preload", 0.5 + i * 0.3)))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # Let any lingering threads finish their current network I/O.
    time.sleep(2.0)
    player.stop()

    if errors:
        print("\nerrors:")
        for e in errors:
            print(f"  - {e}")
    else:
        print("\nno errors.")
    final = player.snapshot()
    print(f"final state: {final.state}  track={final.track_id}")
    return 0 if not errors else 3


if __name__ == "__main__":
    sys.exit(main())
