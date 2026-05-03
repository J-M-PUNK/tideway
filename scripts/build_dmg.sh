#!/usr/bin/env bash
# Build a distributable DMG from a pre-built .app bundle.
#
# Prereqs:
#   - The .app bundle exists at the path given as the first argument
#     (defaults to dist/Tideway.app when called with no arguments —
#     matches the local single-arch dev workflow).
#   - hdiutil (ships with macOS).
#
# Usage:
#   scripts/build_dmg.sh                       # uses dist/Tideway.app
#   scripts/build_dmg.sh path/to/Tideway.app   # explicit path (used by
#                                              # the universal-binary
#                                              # merge job in CI, where
#                                              # the merged .app lives
#                                              # outside dist/)
#
# Output:
#   dist/Tideway-<version>.dmg
#
# Users double-click the DMG, drag the app to the Applications shortcut
# inside the window, eject. Standard macOS install UX. No Homebrew or
# external tool required — pure macOS CLI.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

APP_NAME="Tideway"
APP_PATH="${1:-dist/${APP_NAME}.app}"
VERSION_FILE="VERSION"

if [[ ! -d "$APP_PATH" ]]; then
  echo "ERROR: $APP_PATH not found. Either run pyinstaller first:" >&2
  echo "  .venv/bin/pyinstaller Tideway-mac.spec --noconfirm" >&2
  echo "or pass a path to an existing .app bundle as the first argument." >&2
  exit 1
fi

if [[ -f "$VERSION_FILE" ]]; then
  VERSION="$(cat "$VERSION_FILE" | tr -d '[:space:]')"
else
  VERSION="0.0.0"
fi

STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

# Stage the DMG layout: the .app + a symlink to /Applications. The
# symlink is what gives users the canonical "drag me here" UX.
cp -R "$APP_PATH" "$STAGING/${APP_NAME}.app"
ln -s /Applications "$STAGING/Applications"

DMG_OUT="dist/${APP_NAME}-${VERSION}.dmg"
rm -f "$DMG_OUT"

# `hdiutil create -srcfolder ... -format UDZO` produces a compressed,
# read-only DMG — what users expect for app distribution. UDBZ would
# compress tighter but adds decompress time on mount; UDZO is the
# common choice.
#
# Retry the hdiutil call up to three times. On GitHub-hosted macOS
# runners, `hdiutil create` intermittently fails with "Resource busy"
# when Spotlight / xattr / security daemons still hold a lock on
# something inside the freshly-written staging tree. A 5-second
# backoff is enough for the lock to drop in practice; the failure has
# never been reproducible across consecutive attempts.
echo "Building ${DMG_OUT}…"
for attempt in 1 2 3; do
  if hdiutil create \
      -volname "${APP_NAME} ${VERSION}" \
      -srcfolder "$STAGING" \
      -format UDZO \
      -imagekey zlib-level=9 \
      -ov \
      "$DMG_OUT" > /dev/null; then
    break
  fi
  if [[ "$attempt" -eq 3 ]]; then
    echo "ERROR: hdiutil create failed after 3 attempts" >&2
    exit 1
  fi
  echo "hdiutil create failed (attempt ${attempt}), retrying in 5s…" >&2
  sleep 5
done

# Remove the macOS quarantine xattr from the DMG itself. Doesn't get
# rid of Gatekeeper (that requires signing), but avoids the weird
# "downloaded from the internet" prompt when *you* open a DMG you
# just built locally.
xattr -dr com.apple.quarantine "$DMG_OUT" 2>/dev/null || true

echo ""
echo "Built: $DMG_OUT"
ls -lh "$DMG_OUT" | awk '{print "Size: " $5}'
