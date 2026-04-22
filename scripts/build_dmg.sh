#!/usr/bin/env bash
# Build a distributable DMG from the PyInstaller .app output.
#
# Prereqs:
#   - pyinstaller Tideway-mac.spec has produced dist/Tideway.app
#   - hdiutil (ships with macOS)
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
APP_PATH="dist/${APP_NAME}.app"
VERSION_FILE="VERSION"

if [[ ! -d "$APP_PATH" ]]; then
  echo "ERROR: $APP_PATH not found. Run pyinstaller first:" >&2
  echo "  .venv/bin/pyinstaller Tideway-mac.spec --noconfirm" >&2
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
echo "Building ${DMG_OUT}…"
hdiutil create \
  -volname "${APP_NAME} ${VERSION}" \
  -srcfolder "$STAGING" \
  -format UDZO \
  -imagekey zlib-level=9 \
  -ov \
  "$DMG_OUT" > /dev/null

# Remove the macOS quarantine xattr from the DMG itself. Doesn't get
# rid of Gatekeeper (that requires signing), but avoids the weird
# "downloaded from the internet" prompt when *you* open a DMG you
# just built locally.
xattr -dr com.apple.quarantine "$DMG_OUT" 2>/dev/null || true

echo ""
echo "Built: $DMG_OUT"
ls -lh "$DMG_OUT" | awk '{print "Size: " $5}'
