#!/usr/bin/env python3
"""Dev-only AirPlay 2 pairing diagnostic.

Sends several pair-setup M1 variants and a transient verify to a
receiver and logs exactly what it returns (HTTP status + decoded
TLV). Reveals what a non-Apple receiver (Roku) actually wants for
pairing without any on-device menu navigation.

Run it once and watch the TV the whole time: note whether a code
appears and during which step.

Not bundled. See docs/airplay2-sender.md.

Usage:
  .venv/bin/python scripts/airplay2_diag.py "Roku"
"""

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app.audio.airplay2 import manager  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="AirPlay 2 pairing diag (dev)")
    ap.add_argument("name", help="device name or id substring")
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

    print(f"Target: {match.name} ({match.model}) {match.address}:{match.port}")
    print("Running pairing probes. WATCH THE TV for any code/popup.")
    try:
        m.diagnose(match.id)
        print(
            "\nDone. Paste all the [airplay2] [diag] lines above, and "
            "say whether a code appeared on the TV and during which step."
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - dev tool, surface anything
        print(f"FAILED: {exc!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
