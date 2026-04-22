#!/usr/bin/env bash
# Fetch a static ffmpeg binary into vendor/ffmpeg/ so PyInstaller can
# bundle it. Users of the packaged app don't have to install anything
# — `brew install ffmpeg` is fine for dev, but shipped builds include
# their own so first-run video downloads just work.
#
# Source per OS:
#   macOS:    https://evermeet.cx/ffmpeg/   (static universal build)
#   Linux:    https://johnvansickle.com/ffmpeg/  (static LGPL build)
#   Windows:  https://github.com/BtbN/FFmpeg-Builds (LGPL, static)
#
# Run from repo root:
#   scripts/fetch_ffmpeg.sh
#
# Idempotent — re-running just re-downloads. The binary lands at:
#   vendor/ffmpeg/<os>/ffmpeg[.exe]
# and the PyInstaller specs pick it up from there.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

OS="$(uname -s)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

case "$OS" in
  Darwin)
    TARGET_DIR="vendor/ffmpeg/macos"
    mkdir -p "$TARGET_DIR"
    # evermeet.cx ships a universal binary (arm64 + x86_64) as a zip
    # containing a single `ffmpeg` file. Static build, ~30 MB.
    echo "Downloading macOS ffmpeg from evermeet.cx…"
    curl -fL -o "$TMP/ffmpeg.zip" "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip"
    unzip -o "$TMP/ffmpeg.zip" -d "$TMP"
    mv "$TMP/ffmpeg" "$TARGET_DIR/ffmpeg"
    chmod +x "$TARGET_DIR/ffmpeg"
    # macOS quarantines downloads. Remove so the binary can exec
    # without triggering a Gatekeeper prompt on users' machines.
    xattr -dr com.apple.quarantine "$TARGET_DIR/ffmpeg" 2>/dev/null || true
    echo "Installed: $TARGET_DIR/ffmpeg"
    ;;
  Linux)
    TARGET_DIR="vendor/ffmpeg/linux"
    mkdir -p "$TARGET_DIR"
    echo "Downloading Linux ffmpeg from johnvansickle.com…"
    ARCH="$(uname -m)"
    case "$ARCH" in
      x86_64) URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz" ;;
      aarch64|arm64) URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz" ;;
      *) echo "Unsupported Linux arch: $ARCH" >&2; exit 1 ;;
    esac
    curl -fL -o "$TMP/ffmpeg.tar.xz" "$URL"
    tar -xJf "$TMP/ffmpeg.tar.xz" -C "$TMP"
    # tar extracts to a versioned dir; find the ffmpeg binary inside.
    FOUND="$(find "$TMP" -name ffmpeg -type f -perm -u+x | head -1)"
    mv "$FOUND" "$TARGET_DIR/ffmpeg"
    chmod +x "$TARGET_DIR/ffmpeg"
    echo "Installed: $TARGET_DIR/ffmpeg"
    ;;
  MINGW*|MSYS*|CYGWIN*)
    TARGET_DIR="vendor/ffmpeg/windows"
    mkdir -p "$TARGET_DIR"
    echo "Downloading Windows ffmpeg from BtbN…"
    URL="https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-lgpl.zip"
    curl -fL -o "$TMP/ffmpeg.zip" "$URL"
    unzip -o "$TMP/ffmpeg.zip" -d "$TMP"
    FOUND="$(find "$TMP" -name ffmpeg.exe -type f | head -1)"
    mv "$FOUND" "$TARGET_DIR/ffmpeg.exe"
    echo "Installed: $TARGET_DIR/ffmpeg.exe"
    ;;
  *)
    echo "Unsupported OS: $OS" >&2
    exit 1
    ;;
esac
