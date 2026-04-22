"""Phase 2 AirPlay probe. Streams a local audio file to a
discovered AirPlay receiver using pyatv's stream_file.

Use this to verify that pyatv's RAOP sender actually works on
this machine before designing the real integration. The script
does the same discovery the main probe does, picks the first
receiver whose name matches the target you pass, and then hands
the file off to pyatv.

Usage:
    .venv/bin/python scripts/test_airplay_stream.py <name> <file>

Example:
    .venv/bin/python scripts/test_airplay_stream.py "Living Room" song.flac

Name match is case insensitive substring, so "living" works for
"Living Room HomePod".

Formats pyatv supports for stream_file: MP3, WAV, FLAC, OGG. Any
file you point at the script has to be one of those. A downloaded
Tidal FLAC works.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pyatv


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
    match = next(
        (c for c in results if frag in c.name.lower()),
        None,
    )
    if not match:
        print(
            f"no receiver matched '{name_fragment}'. Found: "
            + ", ".join(c.name for c in results),
            file=sys.stderr,
        )
        return 1

    print(f"Connecting to {match.name} at {match.address}...")
    atv = await pyatv.connect(match, loop)

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
