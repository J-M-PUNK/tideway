"""Stream a local audio file to a discovered AirPlay receiver.

Phase 2 probe. Verifies that pyatv's RAOP sender actually works
on this machine before we design the real sink integration in the
app.

Usage:
    .venv/bin/python scripts/test_airplay_stream.py <name> <file>

Example:
    .venv/bin/python scripts/test_airplay_stream.py "Living Room" song.flac

The first arg is a case-insensitive substring of the receiver
name. The second arg is a path to an audio file pyatv can stream
(MP3, WAV, FLAC, or OGG, per the pyatv docs).

Receiver authentication: modern AirPlay receivers require a
one-time pair handshake before they accept streams. If the
script fails with `AuthenticationError: not authenticated`,
run the companion pair script first:

    .venv/bin/python scripts/pair_airplay.py <name>

That stashes credentials in `airplay_credentials.json`, which
this script then reads and applies before connecting.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pyatv
from pyatv.const import Protocol

CREDENTIALS_FILE = Path("airplay_credentials.json")


def load_credentials_for(identifier: str) -> str | None:
    if not CREDENTIALS_FILE.is_file():
        return None
    try:
        store = json.loads(CREDENTIALS_FILE.read_text())
    except Exception:
        return None
    entry = store.get(identifier)
    if isinstance(entry, dict):
        creds = entry.get("credentials")
        if isinstance(creds, str) and creds:
            return creds
    return None


async def main(name_fragment: str, file_path: str) -> int:
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        print(f"file does not exist: {path}", file=sys.stderr)
        return 2

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

    creds = load_credentials_for(conf.identifier)
    if creds:
        print("Using saved RAOP credentials.")
        conf.set_credentials(Protocol.RAOP, creds)
    else:
        print(
            "No saved credentials for this receiver. "
            "If streaming fails with 'not authenticated', pair first:\n"
            f"    .venv/bin/python scripts/pair_airplay.py '{name_fragment}'"
        )

    print(f"Connecting to {conf.name} at {conf.address}...")
    atv = await pyatv.connect(conf, loop)

    try:
        print(f"Streaming {path.name}...")
        await atv.stream.stream_file(str(path))
        print("Stream finished cleanly.")
    finally:
        atv.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
