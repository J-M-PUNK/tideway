#!/usr/bin/env python3
"""Dev-only AirPlay 2 pairing tool (Stage 2 validation).

Drives the real HAP pair-setup against a receiver so the PIN path
can be validated against the actual target (the Hisense TV), which
does not advertise the no-PIN transient path. Persists the
resulting credentials to the same store the sender uses, then runs
pair-verify to prove the credentials are good end to end.

Not bundled. See docs/airplay2-sender.md.

Usage:
  .venv/bin/python scripts/airplay2_pair.py "SmartTV 4K"
  .venv/bin/python scripts/airplay2_pair.py --list

The receiver shows a PIN on screen; type it when prompted.
"""

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app.audio.airplay2 import manager  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="AirPlay 2 pairing (dev)")
    ap.add_argument("name", nargs="?", help="device name or id substring")
    ap.add_argument(
        "--list", action="store_true", help="list discovered receivers"
    )
    args = ap.parse_args()

    m = manager()
    print("Scanning...")
    devices = m.discover(timeout=6.0)
    if not devices:
        print("No AirPlay devices found.")
        return 1

    if args.list or not args.name:
        for d in devices:
            tag = "streamable" if d.streamable else f"no ({d.reason})"
            print(
                f"  {d.name}  [{d.model}]  {d.address}:{d.port}  "
                f"transient={d.supports_transient_pairing}  {tag}"
            )
        return 0

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
    if match.supports_transient_pairing and not match.pairing == "Mandatory":
        print(
            "Note: this device advertises transient pairing; the sender "
            "can verify without a stored PIN. Pairing anyway is harmless."
        )

    try:
        m.pair_begin(match.id)
    except Exception as exc:  # noqa: BLE001 - dev tool, surface anything
        print(f"pair_begin failed: {exc!r}")
        return 1

    print("A PIN should now be on the receiver's screen.")
    try:
        pin = input("Enter PIN: ").strip()
    except (EOFError, KeyboardInterrupt):
        m.pair_cancel()
        print("\nCancelled.")
        return 1

    try:
        m.pair_finish(pin)
    except Exception as exc:  # noqa: BLE001
        print(f"pair_finish failed: {exc!r}")
        return 1
    print("Paired. Credentials persisted.")

    # Prove the stored credentials verify end to end.
    print("Verifying stored credentials...")
    try:

        async def _v():
            http, verifier = await m._verify(match)
            try:
                http.close()
            except Exception:
                pass
            return type(verifier).__name__

        name = m._run_coro(_v(), timeout=25.0)
        print(f"VERIFY OK -> {name}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"VERIFY FAILED -> {exc!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
