"""Fail the macOS release build if anything in the app needs a newer
macOS than the app declares.

pip picks each library's build for the macOS of the machine it runs
on, so moving the release job to a newer GitHub runner can quietly
raise the macOS the app needs. That happened in v1.39.0: on macos-15,
pip chose orjson's macOS-15-only build over the macOS 11 build that
macos-14 had picked, and nothing noticed.

The minimum we promise is `LSMinimumSystemVersion` in the bundle's
Info.plist (set in Tideway-mac.spec). This walks every Mach-O file in
the built .app, reads the minimum macOS each one was built for, and
lists the ones that need more than that.

The Mach-O parsing is plain Python rather than `otool` so it is
testable from `tests/test_check_macos_min_version.py` on any OS.

Usage:
    python scripts/check_macos_min_version.py dist/Tideway.app

Exits 0 when everything fits, 1 when some file needs a newer macOS
(each one printed as `<path>: needs macOS <version>`), and 2 when the
bundle has no LSMinimumSystemVersion to check against.
"""
from __future__ import annotations

import plistlib
import struct
import sys
from pathlib import Path
from typing import BinaryIO, Optional

MH_MAGIC = 0xFEEDFACE
MH_MAGIC_64 = 0xFEEDFACF
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF

LC_VERSION_MIN_MACOSX = 0x24
LC_BUILD_VERSION = 0x32
PLATFORM_MACOS = 1

# A real universal binary has a handful of slices. Java class files
# share FAT_MAGIC, and their next word is a version number in the
# tens, so anything above this isn't a fat Mach-O.
_MAX_FAT_ARCHS = 16

Version = tuple[int, int, int]


def parse_version(text: str) -> Version:
    """`"15"` / `"15.0"` / `"15.0.1"` -> (15, 0, 0) / (15, 0, 1)."""
    parts = [int(p) for p in text.strip().split(".")]
    parts += [0] * (3 - len(parts))
    return (parts[0], parts[1], parts[2])


def _decode(packed: int) -> Version:
    # Mach-O packs X.Y.Z as xxxx.yy.zz in nibbles.
    return (packed >> 16, (packed >> 8) & 0xFF, packed & 0xFF)


def format_version(v: Version) -> str:
    return f"{v[0]}.{v[1]}" if v[2] == 0 else f"{v[0]}.{v[1]}.{v[2]}"


def _slice_min_version(f: BinaryIO, offset: int) -> Optional[Version]:
    """Minimum macOS of the thin Mach-O starting at `offset`, or None
    if it doesn't say (or isn't a Mach-O)."""
    f.seek(offset)
    head = f.read(4)
    if len(head) < 4:
        return None
    for endian in ("<", ">"):
        (magic,) = struct.unpack(endian + "I", head)
        if magic in (MH_MAGIC, MH_MAGIC_64):
            break
    else:
        return None
    header_size = 32 if magic == MH_MAGIC_64 else 28
    f.seek(offset)
    header = f.read(header_size)
    if len(header) < header_size:
        return None
    ncmds, sizeofcmds = struct.unpack(endian + "II", header[16:24])
    cmds = f.read(sizeofcmds)
    found: Optional[Version] = None
    pos = 0
    for _ in range(ncmds):
        if pos + 8 > len(cmds):
            break
        cmd, cmdsize = struct.unpack(endian + "II", cmds[pos:pos + 8])
        if cmdsize < 8:
            break
        if cmd == LC_BUILD_VERSION and pos + 16 <= len(cmds):
            platform, minos = struct.unpack(endian + "II", cmds[pos + 8:pos + 16])
            if platform == PLATFORM_MACOS:
                found = _decode(minos)
        elif cmd == LC_VERSION_MIN_MACOSX and pos + 12 <= len(cmds):
            (version,) = struct.unpack(endian + "I", cmds[pos + 8:pos + 12])
            found = _decode(version)
        pos += cmdsize
    return found


def min_macos_version(path: Path) -> Optional[Version]:
    """Highest minimum macOS across the slices of the Mach-O file at
    `path`, or None if it isn't a Mach-O or none of its slices say.

    For a universal binary the highest slice wins: a Mac loads the
    slice for its own architecture, and we can't know which one that
    is, so the strictest requirement is the one that holds."""
    with open(path, "rb") as f:
        head = f.read(8)
        if len(head) < 8:
            return None
        magic, nfat = struct.unpack(">II", head)
        if magic in (FAT_MAGIC, FAT_MAGIC_64):
            if not 0 < nfat <= _MAX_FAT_ARCHS:
                return None
            size = path.stat().st_size
            entry = 32 if magic == FAT_MAGIC_64 else 20
            table = f.read(nfat * entry)
            versions = []
            for i in range(nfat):
                rec = table[i * entry:(i + 1) * entry]
                if len(rec) < entry:
                    return None
                if magic == FAT_MAGIC_64:
                    (offset,) = struct.unpack(">Q", rec[8:16])
                else:
                    (offset,) = struct.unpack(">I", rec[8:12])
                if offset >= size:
                    return None
                v = _slice_min_version(f, offset)
                if v is not None:
                    versions.append(v)
            return max(versions) if versions else None
        return _slice_min_version(f, 0)


def declared_minimum(app: Path) -> Optional[Version]:
    plist = app / "Contents" / "Info.plist"
    if not plist.is_file():
        return None
    with open(plist, "rb") as f:
        value = plistlib.load(f).get("LSMinimumSystemVersion")
    return parse_version(str(value)) if value else None


def find_too_new(app: Path, minimum: Version) -> list[tuple[Path, Version]]:
    """Every Mach-O under `app` built for a newer macOS than `minimum`."""
    offenders: list[tuple[Path, Version]] = []
    for path in sorted(app.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        v = min_macos_version(path)
        if v is not None and v > minimum:
            offenders.append((path, v))
    return offenders


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_macos_min_version.py <path/to/App.app>", file=sys.stderr)
        return 2
    app = Path(argv[1])
    minimum = declared_minimum(app)
    if minimum is None:
        print(
            f"{app}: Info.plist has no LSMinimumSystemVersion; "
            "declare it in Tideway-mac.spec",
            file=sys.stderr,
        )
        return 2
    offenders = find_too_new(app, minimum)
    if not offenders:
        print(f"OK: nothing in {app.name} needs more than macOS {format_version(minimum)}")
        return 0
    print(
        f"{len(offenders)} file(s) need a newer macOS than the declared "
        f"{format_version(minimum)}:"
    )
    for path, v in offenders:
        print(f"{path.relative_to(app)}: needs macOS {format_version(v)}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
