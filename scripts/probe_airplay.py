"""Probe script for AirPlay support.

Phase 1 of the AirPlay feature. This script does two things and
prints what it finds. It does not touch the main app.

1. Scans the local network for AirPlay receivers using pyatv's
   built in mDNS discovery. Prints every device it sees along
   with the protocols the device exposes (RAOP for audio only
   AirPlay, AIRPLAY for the full protocol).

2. If a device address is passed as the first argument, connects
   to it and prints what the stream interface exposes, so we know
   before committing to an integration whether we are looking at
   an AirPlay 1 receiver (RAOP only), an AirPlay 2 receiver, or an
   Apple TV that accepts play_url.

Run with:
    .venv/bin/python scripts/probe_airplay.py
    .venv/bin/python scripts/probe_airplay.py 192.168.1.50

If nothing is found on the discovery pass, make sure the machine
running this script is on the same subnet as the receiver. mDNS
does not cross subnets.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Optional

import pyatv
from pyatv.const import Protocol


async def discover() -> list:
    loop = asyncio.get_event_loop()
    print("Scanning for AirPlay and RAOP receivers...")
    results = await pyatv.scan(loop, timeout=5)
    if not results:
        print("  (nothing found)")
        return []
    for conf in results:
        protocols = sorted({svc.protocol.name for svc in conf.services})
        print(f"  {conf.name}")
        print(f"    address:   {conf.address}")
        print(f"    protocols: {', '.join(protocols)}")
        print(f"    device id: {conf.identifier}")
        print()
    return results


async def inspect(address: str) -> None:
    loop = asyncio.get_event_loop()
    print(f"Looking up {address}...")
    results = await pyatv.scan(loop, hosts=[address], timeout=5)
    if not results:
        print(f"  no AirPlay service found at {address}")
        return
    conf = results[0]
    print(f"  found {conf.name} ({conf.identifier})")
    print(f"  services:")
    for svc in conf.services:
        print(
            f"    - {svc.protocol.name:8s} port {svc.port} "
            f"pairing={svc.pairing.name}"
        )
    # Not connecting for real, just reporting. A real connect would
    # need credentials for any device that required pairing, and
    # this script is a probe, not an integration test.


async def main(argv: list[str]) -> None:
    if len(argv) > 1:
        await inspect(argv[1])
        return
    await discover()


if __name__ == "__main__":
    asyncio.run(main(sys.argv))
