"""Tests for `_match_release_asset` arch-aware selection.

Real bug we hit: the original matcher returned the first
`tideway*.exe` it found, so once a release shipped both an x64 and
an ARM64 installer the auto-updater could hand an ARM64 host the
x64 EXE (or vice versa) and the install would crash on first launch
when sounddevice tried to load the wrong libportaudio*.dll.
"""
from __future__ import annotations

import sys

import pytest

import server


def _release(*names: str) -> dict:
    return {
        "assets": [
            {"name": n, "browser_download_url": f"https://example/{n}"}
            for n in names
        ]
    }


@pytest.fixture
def force_windows(monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "win32")


@pytest.fixture
def force_macos(monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "darwin")


def _set_machine(monkeypatch, value: str) -> None:
    monkeypatch.setattr(server.platform, "machine", lambda: value)


def test_x64_host_picks_plain_exe_when_both_assets_present(force_windows, monkeypatch):
    _set_machine(monkeypatch, "AMD64")
    rel = _release("Tideway-setup-1.0.0.exe", "Tideway-setup-1.0.0-arm64.exe")
    assert server._match_release_asset(rel) == "https://example/Tideway-setup-1.0.0.exe"


def test_arm64_host_picks_arm64_exe_when_both_assets_present(force_windows, monkeypatch):
    _set_machine(monkeypatch, "ARM64")
    rel = _release("Tideway-setup-1.0.0.exe", "Tideway-setup-1.0.0-arm64.exe")
    assert (
        server._match_release_asset(rel)
        == "https://example/Tideway-setup-1.0.0-arm64.exe"
    )


def test_arm64_host_picks_arm64_exe_when_listed_first(force_windows, monkeypatch):
    """Asset order in the GitHub response shouldn't influence the pick."""
    _set_machine(monkeypatch, "ARM64")
    rel = _release("Tideway-setup-1.0.0-arm64.exe", "Tideway-setup-1.0.0.exe")
    assert (
        server._match_release_asset(rel)
        == "https://example/Tideway-setup-1.0.0-arm64.exe"
    )


def test_arm64_host_falls_back_to_x64_on_old_release(force_windows, monkeypatch):
    """Releases cut before the ARM64 build job existed only ship a
    single x64 EXE. Hand it to the ARM64 user as a fallback rather
    than 404'ing — install will still crash, but that's the pre-fix
    status quo, not a regression introduced here."""
    _set_machine(monkeypatch, "ARM64")
    rel = _release("Tideway-setup-1.0.0.exe")
    assert server._match_release_asset(rel) == "https://example/Tideway-setup-1.0.0.exe"


def test_x64_host_does_not_pick_arm64_only_release(force_windows, monkeypatch):
    """Inverse fallback: an x64 host on a release that only ships
    arm64 still gets *something* back rather than None, so the
    'Install' button at least surfaces an actionable failure instead
    of silently doing nothing."""
    _set_machine(monkeypatch, "AMD64")
    rel = _release("Tideway-setup-1.0.0-arm64.exe")
    assert (
        server._match_release_asset(rel)
        == "https://example/Tideway-setup-1.0.0-arm64.exe"
    )


def test_macos_picks_dmg_ignoring_arch(force_macos, monkeypatch):
    """macOS doesn't yet ship per-arch DMGs, so the matcher should
    return the .dmg unconditionally and ignore the Windows assets."""
    _set_machine(monkeypatch, "arm64")
    rel = _release(
        "Tideway-1.0.0.dmg",
        "Tideway-setup-1.0.0.exe",
        "Tideway-setup-1.0.0-arm64.exe",
    )
    assert server._match_release_asset(rel) == "https://example/Tideway-1.0.0.dmg"


def test_no_matching_asset_returns_none(force_windows, monkeypatch):
    _set_machine(monkeypatch, "AMD64")
    rel = _release("Tideway-1.0.0.dmg", "checksums.txt")
    assert server._match_release_asset(rel) is None


def test_unsupported_platform_returns_none(monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "linux")
    rel = _release("Tideway-setup-1.0.0.exe", "Tideway-setup-1.0.0-arm64.exe")
    assert server._match_release_asset(rel) is None
