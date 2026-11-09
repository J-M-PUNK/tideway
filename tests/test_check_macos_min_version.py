"""Tests for the macOS minimum-version check run by the release build.

v1.39.0 moved the macOS build to a macos-15 runner, and pip picked
orjson's macOS-15-only build where macos-14 had picked one that runs
on macOS 11. Nothing noticed until the DMG was already built. The
check reads each Mach-O's minimum macOS and compares it against the
bundle's LSMinimumSystemVersion; these tests build small synthetic
Mach-O files so the parsing is pinned down without a Mac.
"""
from __future__ import annotations

import plistlib
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_macos_min_version import (  # noqa: E402
    find_too_new,
    main,
    min_macos_version,
    parse_version,
)

CPU_ARM64 = 0x0100000C
CPU_X86_64 = 0x01000007


def _packed(version: str) -> int:
    major, minor, patch = parse_version(version)
    return (major << 16) | (minor << 8) | patch


def _thin(minos: str | None, *, old_style: bool = False, cpu: int = CPU_ARM64) -> bytes:
    """A 64-bit little-endian Mach-O with one unrelated load command
    and, unless `minos` is None, a version command."""
    cmds = struct.pack("<II", 0x19, 16) + b"\0" * 8  # LC_SEGMENT_64 stand-in
    if minos is not None:
        if old_style:
            cmds += struct.pack("<IIII", 0x24, 16, _packed(minos), _packed(minos))
        else:
            cmds += struct.pack("<IIIIII", 0x32, 24, 1, _packed(minos), _packed(minos), 0)
    ncmds = 2 if minos is not None else 1
    header = struct.pack("<IIIIIIII", 0xFEEDFACF, cpu, 0, 6, ncmds, len(cmds), 0, 0)
    return header + cmds


def _fat(*slices: bytes) -> bytes:
    """A universal binary holding `slices`, each 4 KiB aligned."""
    table_size = 8 + 20 * len(slices)
    body = b""
    entries = b""
    offset = 4096
    for s in slices:
        entries += struct.pack(">IIIII", CPU_ARM64, 0, offset, len(s), 12)
        padded = s + b"\0" * (4096 - len(s) % 4096)
        body += padded
        offset += len(padded)
    head = struct.pack(">II", 0xCAFEBABE, len(slices)) + entries
    return head + b"\0" * (4096 - table_size) + body


def _app(tmp_path: Path, minimum: str | None, files: dict[str, bytes]) -> Path:
    app = tmp_path / "Tideway.app"
    contents = app / "Contents"
    contents.mkdir(parents=True)
    info = {"CFBundleName": "Tideway"}
    if minimum is not None:
        info["LSMinimumSystemVersion"] = minimum
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    for rel, data in files.items():
        path = contents / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return app


def test_reads_build_version(tmp_path):
    f = tmp_path / "lib.so"
    f.write_bytes(_thin("15.0"))
    assert min_macos_version(f) == (15, 0, 0)


def test_reads_the_older_version_min_command(tmp_path):
    f = tmp_path / "lib.so"
    f.write_bytes(_thin("10.13", old_style=True))
    assert min_macos_version(f) == (10, 13, 0)


def test_universal_binary_reports_its_strictest_slice(tmp_path):
    # The orjson case: an x86_64 slice for 10.15 and an arm64 slice
    # for 11.0. A Mac loads its own slice, so the higher one holds.
    f = tmp_path / "orjson.so"
    f.write_bytes(_fat(_thin("10.15", cpu=CPU_X86_64), _thin("11.0")))
    assert min_macos_version(f) == (11, 0, 0)


def test_non_macho_files_are_ignored(tmp_path):
    text = tmp_path / "readme.txt"
    text.write_bytes(b"not a binary at all")
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    # A Java class file starts with the same magic as a universal
    # binary; its next word is a version number, not a slice count.
    java = tmp_path / "Foo.class"
    java.write_bytes(struct.pack(">II", 0xCAFEBABE, 0x00000041) + b"\0" * 64)
    assert min_macos_version(text) is None
    assert min_macos_version(empty) is None
    assert min_macos_version(java) is None


def test_macho_without_a_version_command_is_ignored(tmp_path):
    f = tmp_path / "lib.so"
    f.write_bytes(_thin(None))
    assert min_macos_version(f) is None


def test_find_too_new_lists_only_files_above_the_minimum(tmp_path):
    app = _app(
        tmp_path,
        "14.0",
        {
            "MacOS/Tideway": _thin("11.0"),
            "Frameworks/numpy/_core.so": _thin("14.0"),
            "Frameworks/orjson/orjson.so": _thin("15.0"),
            "Resources/web/index.html": b"<!doctype html>",
        },
    )
    offenders = find_too_new(app, (14, 0, 0))
    assert [(p.name, v) for p, v in offenders] == [("orjson.so", (15, 0, 0))]


def test_main_fails_when_a_library_needs_a_newer_macos(tmp_path, capsys):
    app = _app(tmp_path, "14.0", {"Frameworks/orjson.so": _thin("15.0")})
    assert main(["check", str(app)]) == 1
    out = capsys.readouterr().out
    assert "needs macOS 15.0" in out
    assert "orjson.so" in out


def test_main_passes_when_everything_fits(tmp_path):
    app = _app(
        tmp_path,
        "15.0",
        {"MacOS/Tideway": _thin("11.0"), "Frameworks/orjson.so": _thin("15.0")},
    )
    assert main(["check", str(app)]) == 0


def test_main_refuses_a_bundle_with_no_declared_minimum(tmp_path):
    app = _app(tmp_path, None, {"MacOS/Tideway": _thin("11.0")})
    assert main(["check", str(app)]) == 2
