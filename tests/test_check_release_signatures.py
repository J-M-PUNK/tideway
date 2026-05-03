"""Tests for the release-signing-guard helper.

The bug the guard catches: a maintainer (or an AI driving the
deploy) tags a release, lets CI build the installers, and clicks
Publish without first running `scripts/sign-release.sh`. Every
v1.4.0+ install then refuses the auto-update because the .minisig
sidecar is missing. The guard's detection logic lives in
`find_missing_minisig`; these tests pin its behavior down so a
future refactor can't quietly break the production safety net.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_release_signatures import find_missing_minisig  # noqa: E402


# A fully-signed v1.4.0-ish release as the canonical positive case.
SIGNED_RELEASE = [
    "Tideway-1.4.0.dmg",
    "Tideway-1.4.0.dmg.minisig",
    "Tideway-setup-1.4.0.exe",
    "Tideway-setup-1.4.0.exe.minisig",
    "Tideway-setup-1.4.0-arm64.exe",
    "Tideway-setup-1.4.0-arm64.exe.minisig",
    "Tideway-1.4.0-x86_64.AppImage",
    "Tideway-1.4.0-x86_64.AppImage.minisig",
]


def test_fully_signed_release_returns_empty():
    assert find_missing_minisig(SIGNED_RELEASE) == []


def test_unsigned_release_flags_every_installer():
    """The exact failure mode v1.4.1 hit: workflow built four
    installers, sign-release.sh never ran, Publish was clicked."""
    assets = [
        "Tideway-1.4.1.dmg",
        "Tideway-setup-1.4.1.exe",
        "Tideway-setup-1.4.1-arm64.exe",
        "Tideway-1.4.1-x86_64.AppImage",
    ]
    missing = find_missing_minisig(assets)
    assert sorted(missing) == sorted(assets)


def test_partial_signing_flags_only_the_unsigned():
    """If the maintainer signed three of four installers (e.g.
    minisign passphrase fatigue, machine fell asleep mid-script),
    the guard should flag exactly the one that was missed —
    not pass because "most" of them are signed."""
    assets = [
        "Tideway-1.4.0.dmg",
        "Tideway-1.4.0.dmg.minisig",
        "Tideway-setup-1.4.0.exe",
        "Tideway-setup-1.4.0.exe.minisig",
        "Tideway-setup-1.4.0-arm64.exe",
        "Tideway-1.4.0-x86_64.AppImage",
        "Tideway-1.4.0-x86_64.AppImage.minisig",
    ]
    assert find_missing_minisig(assets) == ["Tideway-setup-1.4.0-arm64.exe"]


def test_minisig_alone_is_not_an_installer():
    """A `.minisig` file in the asset list without the matching
    installer is weird, but it's not what the guard cares about
    — the guard checks installers, not orphan sidecars. So no
    false positive here."""
    assets = ["Tideway-1.4.0.dmg.minisig"]
    assert find_missing_minisig(assets) == []


def test_non_installer_assets_are_ignored():
    """Future releases may attach checksums, SBOMs, source
    tarballs etc. — the guard must not demand .minisig sidecars
    for those, only for the binaries the auto-updater downloads."""
    assets = [
        "Tideway-1.4.0.dmg",
        "Tideway-1.4.0.dmg.minisig",
        "checksums.txt",
        "SBOM.spdx.json",
        "source.tar.gz",
    ]
    assert find_missing_minisig(assets) == []


def test_empty_release_returns_empty():
    """A release with zero assets (workflow failed mid-upload, or
    the maintainer is staging assets manually) has nothing for
    the guard to verify. Don't false-positive into rolling back
    an empty release."""
    assert find_missing_minisig([]) == []


def test_appimage_capitalisation_matches_actual_artifact_name():
    """`appimagetool` emits `.AppImage` (mixed case). The matcher
    treats the suffix exactly — no lowercasing — so the .minisig
    sidecar lookup is case-sensitive too. Pinning this in case
    a "let's lowercase everything" refactor accidentally breaks
    the AppImage detection."""
    assets = ["Tideway-1.4.0-x86_64.AppImage"]
    assert find_missing_minisig(assets) == ["Tideway-1.4.0-x86_64.AppImage"]
    assets_signed = [
        "Tideway-1.4.0-x86_64.AppImage",
        "Tideway-1.4.0-x86_64.AppImage.minisig",
    ]
    assert find_missing_minisig(assets_signed) == []


def test_arm64_dmg_naming_handled():
    """Forward-compat: PR #92 introduces `-arm64.dmg` and
    `-x64.dmg` arch-tagged DMG names. Each needs its own
    .minisig — the guard treats them as independent installer
    artifacts (because that's what they are)."""
    assets = [
        "Tideway-1.5.0-arm64.dmg",
        "Tideway-1.5.0-arm64.dmg.minisig",
        "Tideway-1.5.0-x64.dmg",
        # Intel .minisig deliberately missing
    ]
    assert find_missing_minisig(assets) == ["Tideway-1.5.0-x64.dmg"]
