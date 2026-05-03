"""Find installer assets on a GitHub release that are missing
their minisign sidecar.

Used by `.github/workflows/release-signing-guard.yml` to catch the
case where someone published a release before running
`scripts/sign-release.sh`. Every Tideway install >= v1.4.0 verifies
a `.minisig` sidecar before launching the installer the auto-
updater downloaded; without the sidecar, the install click is a
hard error for every user. The guard auto-rolls a missing-sidecar
release back to draft so the failure window is seconds, not
"whenever the maintainer next checks the release page".

The detection logic lives here (and not inline in the workflow
YAML) so it's testable from `tests/test_check_release_signatures.py`
without spinning up GitHub.

Usage:
    python scripts/check_release_signatures.py <asset-list-file>

The asset-list-file is one filename per line — typically the
output of `gh release view <tag> --json assets --jq
'.assets[].name'`. Exits 0 if every installer has a paired
sidecar, 1 if any are missing (and prints the missing names to
stdout, one per line, for the workflow to consume).
"""
from __future__ import annotations

import sys
from pathlib import Path


# Asset filename suffixes we treat as "installer artifacts that
# the auto-updater will hand to a user". Anything matching one of
# these MUST have a `<name>.minisig` sidecar in the same release.
INSTALLER_SUFFIXES: tuple[str, ...] = (".dmg", ".exe", ".AppImage")


def find_missing_minisig(asset_names: list[str]) -> list[str]:
    """Return the installer asset names that lack a `.minisig`
    sidecar in `asset_names`.

    `asset_names` is the flat list of every asset filename on a
    GitHub release. Order doesn't matter. The check is exact-name:
    `Foo-1.0.dmg` is satisfied iff `Foo-1.0.dmg.minisig` is also
    in the list.
    """
    names = set(asset_names)
    missing: list[str] = []
    for name in asset_names:
        if not name.endswith(INSTALLER_SUFFIXES):
            continue
        if f"{name}.minisig" not in names:
            missing.append(name)
    return missing


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: check_release_signatures.py <asset-list-file>",
            file=sys.stderr,
        )
        return 2
    path = Path(argv[1])
    asset_names = [
        line.strip() for line in path.read_text().splitlines() if line.strip()
    ]
    missing = find_missing_minisig(asset_names)
    if not missing:
        return 0
    for name in missing:
        print(name)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
