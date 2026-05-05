#!/usr/bin/env bash
# Build a Tideway AppImage from the PyInstaller output at
# dist/Tideway/.
#
# Prerequisites:
#   - PyInstaller has already produced dist/Tideway/ (run
#     `pyinstaller Tideway-linux.spec --noconfirm` first).
#   - `appimagetool` available on PATH or downloadable; the script
#     fetches the official x86_64 build into ./tools/ if missing.
#   - A 256x256 PNG icon at assets/icon.png (produced by
#     scripts/build_icons.sh from assets/icon-source.png).
#
# Output: dist/Tideway-<version>-x86_64.AppImage
# Naming convention matches the asset matcher in server.py.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -d "dist/Tideway" ]; then
  echo "dist/Tideway not found — run 'pyinstaller Tideway-linux.spec --noconfirm' first." >&2
  exit 1
fi

VERSION="$(cat VERSION 2>/dev/null || echo "0.0.0")"
APPDIR="$ROOT/dist/Tideway.AppDir"

# Fresh AppDir every time — leftover files from a previous build can
# silently bloat the AppImage or mask a missing-file regression.
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
mkdir -p "$APPDIR/usr/share/applications"
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# Stage the PyInstaller output into the AppDir's bin directory.
# Using cp -a preserves executable bits on Tideway and any bundled
# .so files; without -a the dynamic linker fails to load the libs.
cp -a dist/Tideway/. "$APPDIR/usr/bin/"

# Desktop entry — both at the canonical location (for system menu
# integration if the user runs `appimaged`) and as a top-level
# symlink, which is what appimagetool expects.
cp scripts/appimage/tideway.desktop "$APPDIR/usr/share/applications/tideway.desktop"
ln -sf usr/share/applications/tideway.desktop "$APPDIR/tideway.desktop"

# Icon — copy assets/icon.png into the AppDir's hicolor tree and
# symlink it to the AppDir root, where appimagetool expects to
# find a tideway.png matching the .desktop file's Icon= field.
ICON_SRC="assets/icon.png"
if [ ! -f "$ICON_SRC" ]; then
  echo "No icon found at $ICON_SRC. Run scripts/build_icons.sh to produce it." >&2
  exit 1
fi
cp "$ICON_SRC" "$APPDIR/usr/share/icons/hicolor/256x256/apps/tideway.png"
ln -sf usr/share/icons/hicolor/256x256/apps/tideway.png "$APPDIR/tideway.png"

# AppRun is the entry script the AppImage runtime invokes. Must be
# executable.
cp scripts/appimage/AppRun "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"

# appimagetool: prefer a system install; otherwise pull the official
# continuous build into ./tools/ on first run. Pinned to the
# `continuous` release because there's no stable tag, and AppImage's
# core team documents `continuous` as the supported entry point.
APPIMAGETOOL="$(command -v appimagetool || true)"
if [ -z "$APPIMAGETOOL" ]; then
  TOOLS="$ROOT/tools"
  mkdir -p "$TOOLS"
  APPIMAGETOOL="$TOOLS/appimagetool-x86_64.AppImage"
  if [ ! -x "$APPIMAGETOOL" ]; then
    echo "Downloading appimagetool to $APPIMAGETOOL"
    curl -L --fail \
      -o "$APPIMAGETOOL" \
      https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x "$APPIMAGETOOL"
  fi
fi

OUTPUT="dist/Tideway-${VERSION}-x86_64.AppImage"

# ARCH env var is required by appimagetool to set the AppImage
# runtime's architecture field. Without it the tool errors out on
# CI environments that lack a desktop session.
ARCH=x86_64 "$APPIMAGETOOL" \
  --no-appstream \
  "$APPDIR" \
  "$OUTPUT"

echo
echo "Built $OUTPUT"
ls -lh "$OUTPUT"
