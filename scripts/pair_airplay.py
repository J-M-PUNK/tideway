"""One-time pairing for an AirPlay receiver.

Modern AirPlay receivers (HomePods, Apple TV, the AirPlay Receiver
feature on macOS and recent iOS devices) require a HomeKit-style
pair handshake before they accept a stream. pyatv's streaming API
can't skip this. So we run the pair flow once, capture the
credentials string that comes out, and stash it in a JSON file
keyed by device identifier. The stream script reads the file and
sets the credentials on the config before connecting.

Usage:
    .venv/bin/python scripts/pair_airplay.py <name-substring>

Example:
    .venv/bin/python scripts/pair_airplay.py "MacBook"

What to expect:
    1. The script scans the network, picks the first receiver
       whose name matches the substring, and starts pairing.
    2. A 4-digit PIN appears on the receiver (or, for a macOS
       AirPlay Receiver, a pairing dialog pops up on the Mac
       with the code).
    3. Type the PIN into this script.
    4. Credentials get appended to `airplay_credentials.json`
       under the device id.

Run this once per receiver. The test/stream scripts then reuse
the saved credentials and won't re-prompt.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pyatv
from pyatv.const import Protocol

CREDENTIALS_FILE = Path("airplay_credentials.json")


def load_credentials() -> dict:
    if CREDENTIALS_FILE.is_file():
        try:
            return json.loads(CREDENTIALS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_credentials(store: dict) -> None:
    CREDENTIALS_FILE.write_text(json.dumps(store, indent=2))


async def main(name_fragment: str) -> int:
    loop = asyncio.get_event_loop()
    print("Scanning for receivers...")
    results = await pyatv.scan(loop, timeout=5)
    if not results:
        print("no AirPlay receivers found", file=sys.stderr)
        return 1

    frag = name_fragment.lower()
    conf = next(
        (c for c in results if frag in c.name.lower()),
        None,
    )
    if conf is None:
        print(
            f"no receiver matched '{name_fragment}'. Found: "
            + ", ".join(c.name for c in results),
            file=sys.stderr,
        )
        return 1

    print(f"Pairing with {conf.name} at {conf.address}...")
    pairing = await pyatv.pair(conf, Protocol.RAOP, loop)
    try:
        await pairing.begin()
        print(
            "A PIN should now be visible on the receiver. "
            "If the receiver is a Mac, watch for a dialog on the "
            "desktop of the Mac that is receiving."
        )
        pin = input("Enter PIN: ").strip()
        if not pin:
            print("no PIN entered, aborting", file=sys.stderr)
            return 1
        pairing.pin(pin)
        await pairing.finish()
    finally:
        # Always call close so sockets release even if the user ctrl-Cs.
        await pairing.close()

    if not pairing.has_paired:
        print("pairing did not complete successfully", file=sys.stderr)
        return 1

    creds = pairing.service.credentials
    if not creds:
        print("pairing reported success but credentials are empty", file=sys.stderr)
        return 1

    store = load_credentials()
    store[conf.identifier] = {
        "name": conf.name,
        "credentials": creds,
    }
    save_credentials(store)
    print(
        f"Saved credentials for {conf.name} (id {conf.identifier}) to "
        f"{CREDENTIALS_FILE.resolve()}."
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
