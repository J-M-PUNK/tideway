#!/usr/bin/env python3
"""Dev-only AirPlay 2 tone spike (Stage 4/5 gate).

Runs the validated PTP buffered SETUP against a paired receiver,
then acts as our own PTP grandmaster (RTCP time-announce packets)
and streams a 440 Hz LPCM test tone to the negotiated dataPort.

This answers the one remaining empirical question: does the
Hisense actually render audio off our self-assigned grandmaster
with no real gPTP exchange? If you hear a tone, the gate passes
and the rest is engineering. If it's silent, the receiver needs a
real IEEE 1588 grandmaster.

Not bundled. See docs/airplay2-sender.md.

Usage:
  .venv/bin/python scripts/airplay2_play_tone.py "SmartTV 4K" [seconds]

Pair first if needed:
  .venv/bin/python scripts/airplay2_pair.py "SmartTV 4K"
"""

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app.audio.airplay2 import manager  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="AirPlay 2 tone spike (dev)")
    ap.add_argument("name", help="device name or id substring")
    ap.add_argument(
        "seconds", nargs="?", type=float, default=6.0, help="tone duration"
    )
    ap.add_argument(
        "--with-grandmaster",
        action="store_true",
        help=(
            "boot a real IEEE 1588 v2 grandmaster on UDP 319/320 next to "
            "the tone stream. Historical: the doc's Stage 4b spike. Real "
            "AirPlay 2 receivers ARE the grandmaster so this almost "
            "never helps; use --with-ptp-slave instead."
        ),
    )
    ap.add_argument(
        "--with-ptp-slave",
        action="store_true",
        help=(
            "passively listen for the receiver's gPTP master, derive a "
            "clock offset, and project the RTCP TIME_ANNOUNCE packet "
            "onto the master's clock. Matches owntone+nqptp behaviour."
        ),
    )
    args = ap.parse_args()

    m = manager()
    print("Scanning...")
    devices = m.discover(timeout=6.0)
    needle = args.name.lower()
    match = next(
        (
            d
            for d in devices
            if needle in d.name.lower() or needle in d.id.lower()
        ),
        None,
    )
    if match is None:
        print(f"No device matching {args.name!r}.")
        return 1

    print(
        f"Target: {match.name} ({match.model}) "
        f"{match.address}:{match.port}"
    )
    print(
        f"Streaming a {args.seconds:.0f}s 440 Hz tone. "
        f"LISTEN TO THE TV."
    )
    try:
        res = m.probe_play_tone(
            match.id,
            seconds=args.seconds,
            with_grandmaster=args.with_grandmaster,
            with_ptp_slave=args.with_ptp_slave,
        )
        print(f"DONE: {res}")
        print(
            "Did you hear a tone? Yes -> grandmaster gate PASSES. "
            "Silent -> the receiver needs real gPTP."
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - dev tool, surface anything
        print(f"FAILED: {exc!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
