#!/usr/bin/env bash
# Generate macOS .icns and Windows .ico from a single source PNG.
#
# Drop a 1024x1024 PNG at `assets/icon-source.png`, then run:
#   scripts/build_icons.sh
#
# Outputs:
#   assets/icon.icns  (macOS — picked up by Tideway-mac.spec)
#   assets/icon.ico   (Windows — picked up by Tideway-win.spec
#                      and scripts/Tideway.iss)
#
# Prereqs: `iconutil` (ships with Xcode Command Line Tools) and
# either ImageMagick (`brew install imagemagick`) or `sips` (ships
# with macOS) for the resize passes. Cross-platform alternative: a
# Python path using Pillow, but this shell version is faster and
# keeps the asset pipeline out of the Python runtime.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

SRC="assets/icon-source.png"
if [[ ! -f "$SRC" ]]; then
  cat >&2 <<EOF
ERROR: $SRC not found.

Drop a 1024x1024 PNG at that path (transparent background OK — macOS
uses it as-is, Windows paints on a checker otherwise). Then re-run:
  scripts/build_icons.sh
EOF
  exit 1
fi

# Verify input is close to square and at least 512px so downsamples
# don't look mushy. The .icns pipeline needs a 1024 — anything
# smaller gets resampled up and artifacts badly.
DIMS="$(sips -g pixelWidth -g pixelHeight "$SRC" | awk '/pixel/ {print $2}' | tr '\n' 'x' | sed 's/x$//')"
echo "Source: $SRC ($DIMS)"

# --- macOS .icns ---------------------------------------------------------
# .iconset convention: a folder of PNGs named icon_<size>.png plus the
# retina variants icon_<size>@2x.png. iconutil eats that folder and
# emits an .icns that macOS prefers over a bare .png.
ICONSET="$(mktemp -d)/icon.iconset"
mkdir -p "$ICONSET"

for SZ in 16 32 128 256 512; do
  sips -z "$SZ" "$SZ" "$SRC" --out "$ICONSET/icon_${SZ}x${SZ}.png" > /dev/null
  # Retina variant is 2x the logical size.
  SZ2X=$((SZ * 2))
  sips -z "$SZ2X" "$SZ2X" "$SRC" --out "$ICONSET/icon_${SZ}x${SZ}@2x.png" > /dev/null
done

iconutil -c icns "$ICONSET" -o assets/icon.icns
rm -rf "$ICONSET"
echo "Wrote: assets/icon.icns"

# --- Windows .ico --------------------------------------------------------
# Windows wants a multi-size .ico with 16/32/48/256 packed inside.
# Use Pillow since macOS ships no native .ico tooling. Prefer the
# project's .venv python when present so this works on a machine
# where Pillow is installed there but not system-wide.
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PY_BIN="$REPO_ROOT/.venv/bin/python"
else
  PY_BIN="python3"
fi
"$PY_BIN" - <<'PY'
from pathlib import Path
try:
    from PIL import Image
except ImportError:
    import sys
    sys.stderr.write(
        "ERROR: Pillow not installed. `pip install pillow` or run "
        "this inside the project's .venv.\n"
    )
    sys.exit(1)

src = Path("assets/icon-source.png")
img = Image.open(src).convert("RGBA")
sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
img.save(
    "assets/icon.ico",
    format="ICO",
    sizes=sizes,
)
print("Wrote: assets/icon.ico")
PY
