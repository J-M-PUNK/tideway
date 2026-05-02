#!/usr/bin/env bash
# Sign a Tideway release's installer artifacts with the maintainer's
# minisign key and upload the .minisig files to the corresponding
# GitHub release.
#
# This is the manual step that slots in between two things the rest
# of the release flow handles automatically:
#
#   1. CI built the platform installers and attached them to a draft
#      GitHub release (see .github/workflows/release.yml).
#   2. (this script) Maintainer signs each artifact locally and
#      uploads the .minisig sidecars to the same draft release.
#   3. Maintainer publishes the draft. The auto-updater on every
#      installed copy will then download both the artifact and the
#      .minisig, verify, and run.
#
# The signing key is intentionally NOT in CI. If it lived in GitHub
# Actions secrets, a compromise of the GitHub publishing account
# would also yield the signing key, defeating the whole point of
# the verification step. Keeping the key local means an attacker
# needs to compromise both GitHub AND the maintainer's machine,
# which is a much bigger ask.
#
# Prereqs:
#   - minisign installed:        brew install minisign
#   - gh CLI installed and auth: gh auth login
#   - signing keypair generated: minisign -G -p tideway-release.pub \
#                                          -s ~/.tideway-release-key
#     (and the public key from tideway-release.pub already pasted into
#      app/release_keys.py and shipped in a previous release)
#
# Usage:
#   scripts/sign-release.sh v1.3.0
#
# The tag must already exist on GitHub as a draft release with the
# installer artifacts attached. The script downloads each artifact,
# signs it with -H (BLAKE2b-prehashed Ed25519, the only mode the
# in-app verifier accepts), uploads the .minisig back, and leaves
# the release as a draft so you can review and click Publish.

set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <tag>      (e.g. $0 v1.3.0)" >&2
    exit 2
fi

TAG="$1"
SECRET_KEY="${TIDEWAY_RELEASE_KEY:-$HOME/.tideway-release-key}"

if [ ! -f "$SECRET_KEY" ]; then
    echo "Signing key not found at $SECRET_KEY." >&2
    echo "Generate one with: minisign -G -p tideway-release.pub -s $SECRET_KEY" >&2
    echo "Or point TIDEWAY_RELEASE_KEY at an existing key file." >&2
    exit 1
fi

if ! command -v minisign >/dev/null 2>&1; then
    echo "minisign not installed. brew install minisign" >&2
    exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
    echo "gh CLI not installed. https://cli.github.com" >&2
    exit 1
fi

# Determine the GitHub repo from the current working directory's git
# context BEFORE we cd into the temp work dir for downloads. gh's
# release subcommands fall back to git remote inference when --repo
# isn't passed, so without this they'd fail with "not a git
# repository" once we're inside the temp dir.
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
if [ -z "$REPO" ]; then
    echo "Could not infer GitHub repo from $(pwd). Run this script from inside a Tideway checkout." >&2
    exit 1
fi

WORK_DIR="$(mktemp -d -t tideway-sign-XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "Downloading $TAG artifacts from $REPO to $WORK_DIR ..."
cd "$WORK_DIR"

# Pull every installer asset attached to the tag. The patterns match
# what release.yml uploads. -p can be repeated, and gh skips assets
# that don't match (so a tag missing one platform's build still
# proceeds for the platforms it does have).
gh release download "$TAG" \
    --repo "$REPO" \
    --pattern 'Tideway-*.dmg' \
    --pattern 'Tideway-setup-*.exe' \
    --pattern 'Tideway-*-x86_64.AppImage'

shopt -s nullglob
ARTIFACTS=( Tideway-*.dmg Tideway-setup-*.exe Tideway-*-x86_64.AppImage )
if [ "${#ARTIFACTS[@]}" -eq 0 ]; then
    echo "No installer artifacts found on release $TAG. Did the build job run?" >&2
    exit 1
fi

echo "Found ${#ARTIFACTS[@]} artifact(s). Signing each (you'll be prompted for the passphrase)..."

# minisign reads the passphrase from the controlling tty by default.
# We deliberately do NOT pass -W (no passphrase) — the signing key
# being passphrase-protected is half of the threat model: malware on
# the laptop that grabs the key file alone still doesn't get to sign
# without the passphrase.
for ARTIFACT in "${ARTIFACTS[@]}"; do
    echo ""
    echo "--> Signing $ARTIFACT"
    # -H = BLAKE2b prehashed mode. The in-app verifier rejects raw
    # Ed25519 signatures (algorithm marker "Ed") and only accepts
    # prehashed ("ED"), so this flag is mandatory not optional.
    # -t = trusted comment. Bound to the file by the global
    # signature, so it's safe to log on the verification side.
    minisign \
        -S -H \
        -s "$SECRET_KEY" \
        -t "Tideway $TAG" \
        -m "$ARTIFACT"
done

echo ""
echo "Uploading .minisig sidecars back to release $TAG ..."
gh release upload "$TAG" *.minisig --repo "$REPO" --clobber

echo ""
echo "Done. The release is still a draft."
echo "Review at: https://github.com/$REPO/releases"
echo "Click Publish when ready."
