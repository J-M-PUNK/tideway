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


def test_macos_apple_silicon_picks_arm64_dmg(force_macos, monkeypatch):
    """Apple Silicon Macs should be offered the arm64 DMG when both
    are present. Critical for the bit-perfect audio path — running
    an x64 binary under Rosetta 2 changes ALSA / CoreAudio behaviour
    in subtle ways that compromise the engine's invariants."""
    _set_machine(monkeypatch, "arm64")
    rel = _release(
        "Tideway-1.5.0-arm64.dmg",
        "Tideway-1.5.0-x64.dmg",
        "Tideway-setup-1.5.0.exe",
    )
    assert (
        server._match_release_asset(rel)
        == "https://example/Tideway-1.5.0-arm64.dmg"
    )


def test_macos_intel_picks_x64_dmg(force_macos, monkeypatch):
    """The whole reason this PR exists: Intel Mac users were
    silently getting served the arm64 DMG and hitting "this
    application is damaged" or worse. The x64-tagged DMG must
    win for Intel hosts."""
    _set_machine(monkeypatch, "x86_64")
    rel = _release(
        "Tideway-1.5.0-arm64.dmg",
        "Tideway-1.5.0-x64.dmg",
    )
    assert (
        server._match_release_asset(rel)
        == "https://example/Tideway-1.5.0-x64.dmg"
    )


def test_macos_apple_silicon_falls_back_to_unsuffixed_dmg(force_macos, monkeypatch):
    """Releases predating this PR (≤v1.4.0) shipped a single
    unsuffixed `Tideway-<v>.dmg` that was actually arm64. Apple
    Silicon users running the auto-updater against one of those
    older releases (e.g. browsing back through the release log)
    should still get pointed at it."""
    _set_machine(monkeypatch, "arm64")
    rel = _release("Tideway-1.4.0.dmg")
    assert (
        server._match_release_asset(rel)
        == "https://example/Tideway-1.4.0.dmg"
    )


def test_macos_intel_falls_back_to_unsuffixed_dmg_on_old_release(
    force_macos, monkeypatch
):
    """An Intel Mac user updating against a pre-PR release gets
    handed the unsuffixed (=arm64) DMG as a fallback. It won't
    actually run on Intel — but that's the pre-fix status quo,
    not a regression. The new arch-tagged releases will give
    them a working install going forward."""
    _set_machine(monkeypatch, "x86_64")
    rel = _release("Tideway-1.4.0.dmg")
    assert (
        server._match_release_asset(rel)
        == "https://example/Tideway-1.4.0.dmg"
    )


def test_macos_picks_dmg_ignoring_windows_and_linux_assets(force_macos, monkeypatch):
    """Sanity: with all four platforms' assets in the release,
    the matcher only looks at .dmg files."""
    _set_machine(monkeypatch, "arm64")
    rel = _release(
        "Tideway-1.5.0-arm64.dmg",
        "Tideway-1.5.0-x64.dmg",
        "Tideway-setup-1.5.0.exe",
        "Tideway-setup-1.5.0-arm64.exe",
        "Tideway-1.5.0-x86_64.AppImage",
    )
    assert (
        server._match_release_asset(rel)
        == "https://example/Tideway-1.5.0-arm64.dmg"
    )


def test_no_matching_asset_returns_none(force_windows, monkeypatch):
    _set_machine(monkeypatch, "AMD64")
    rel = _release("Tideway-1.0.0.dmg", "checksums.txt")
    assert server._match_release_asset(rel) is None


# --- Linux ---------------------------------------------------------------


@pytest.fixture
def force_linux(monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "linux")


def test_linux_x86_64_picks_appimage(force_linux, monkeypatch):
    _set_machine(monkeypatch, "x86_64")
    rel = _release(
        "Tideway-1.2.0-x86_64.AppImage",
        "Tideway-1.2.0.dmg",
        "Tideway-setup-1.2.0.exe",
    )
    assert (
        server._match_release_asset(rel)
        == "https://example/Tideway-1.2.0-x86_64.AppImage"
    )


def test_linux_aarch64_returns_none_pending_arm_build(force_linux, monkeypatch):
    """No aarch64 AppImage is built today. Defensive: we hand back
    None rather than the wrong-arch x86_64 file so the auto-updater
    surfaces 'no installer' instead of silently downloading
    something that won't run."""
    _set_machine(monkeypatch, "aarch64")
    rel = _release("Tideway-1.2.0-x86_64.AppImage")
    assert server._match_release_asset(rel) is None


def test_linux_no_appimage_in_release_returns_none(force_linux, monkeypatch):
    """Releases predating the AppImage build job (≤v1.1.0) don't
    ship one. Match returns None; the UI's update banner falls back
    to "open the releases page" for those users."""
    _set_machine(monkeypatch, "x86_64")
    rel = _release("Tideway-1.0.0.dmg", "Tideway-setup-1.0.0.exe")
    assert server._match_release_asset(rel) is None


def test_linux_match_is_case_insensitive_on_extension(force_linux, monkeypatch):
    """Asset names in GitHub responses preserve the upload casing.
    appimagetool emits .AppImage (mixed case); the matcher
    lowercases before comparing so Tideway-1.2.0-x86_64.AppImage
    still matches the .appimage suffix in the matcher."""
    _set_machine(monkeypatch, "x86_64")
    rel = _release("Tideway-1.2.0-x86_64.AppImage")
    assert (
        server._match_release_asset(rel)
        == "https://example/Tideway-1.2.0-x86_64.AppImage"
    )


def test_unknown_platform_returns_none(monkeypatch):
    """Any platform that isn't darwin / win / linux falls through.
    Catches future BSDs / unknowns rather than silently picking the
    wrong installer."""
    monkeypatch.setattr(server.sys, "platform", "freebsd")
    rel = _release("Tideway-setup-1.0.0.exe", "Tideway-1.0.0-x86_64.AppImage")
    assert server._match_release_asset(rel) is None
