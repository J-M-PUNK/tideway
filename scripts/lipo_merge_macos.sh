#!/usr/bin/env bash
# Fuse two single-architecture macOS .app bundles into one universal2
# .app bundle. Reads an arm64 .app and an x86_64 .app, produces an
# output .app whose Mach-O binaries are universal (both archs in one
# file) and whose non-binary resources come from the arm64 source
# verbatim.
#
# Why this exists:
#
#   PyInstaller produces single-arch .app bundles. If we ship just
#   one, half our user base (Apple Silicon or Intel, depending which
#   we picked) gets a broken-on-launch app. The two-DMG approach
#   forces users to pick by architecture, which is a UX regression
#   relative to what every other modern Mac app does.
#
#   The standard fix is a "universal2" .app: every Mach-O binary
#   inside is fat (contains both arm64 and x86_64 slices), so macOS
#   loads the right slice automatically based on the host CPU.
#   Apple's official path to universal2 is `--target-arch universal2`
#   at build time, but that requires Python and every C-extension
#   wheel to be universal2 themselves — rarely the case in practice.
#   Building each arch separately and lipo-merging the results is
#   the standard workaround.
#
# Usage:
#
#   scripts/lipo_merge_macos.sh <arm64_app> <x64_app> <output_app>
#
# All three paths are .app bundle directories. The output bundle is
# created fresh (existing one at that path is removed first).
#
# Algorithm:
#
#   1. Copy arm64 .app to the output path. Most files in the bundle
#      (Info.plist, JS bundles, icons, fonts, frozen Python bytecode)
#      are arch-independent — they survive verbatim.
#   2. Walk every regular file in the output bundle. For each one,
#      run `file -b` to check the type:
#        - If "Mach-O" (executable, dylib, shared object): find the
#          matching path in the x64 bundle. Run lipo to extract
#          arm64 and x86_64 slices and combine them into a fat
#          binary that replaces the arm64-only original.
#        - If not Mach-O: skip. The arm64 copy is byte-identical to
#          what the x64 build would have produced for resources.
#   3. Walk the x64 bundle for any Mach-O files that DON'T exist in
#      the output. Those are arch-specific dependencies one wheel
#      pulled in but the other didn't. Copy them in as-is — they're
#      single-arch but at least they exist (better than missing).
#
# Edge cases:
#
#   - A Mach-O file already universal in one source (e.g. the
#     arm64 wheel happened to include a fat binary). lipo -thin
#     extracts the right slice; if no slice exists for the requested
#     arch, fall back to copying the existing fat binary unchanged.
#   - Symlinks inside the bundle (Frameworks/Versions/A pattern).
#     `cp -R` preserves them, and we skip non-regular files
#     during the walk.

set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $0 <arm64_app> <x64_app> <output_app>" >&2
    exit 2
fi

ARM64_APP="$1"
X64_APP="$2"
OUTPUT_APP="$3"

if [ ! -d "$ARM64_APP" ]; then
    echo "ERROR: arm64 .app not found: $ARM64_APP" >&2
    exit 1
fi
if [ ! -d "$X64_APP" ]; then
    echo "ERROR: x64 .app not found: $X64_APP" >&2
    exit 1
fi

if ! command -v lipo >/dev/null 2>&1; then
    echo "ERROR: lipo not in PATH (this script must run on macOS)" >&2
    exit 1
fi

echo "lipo-merging:"
echo "  arm64:  $ARM64_APP"
echo "  x86_64: $X64_APP"
echo "  output: $OUTPUT_APP"

# Start fresh. Removing first means a half-completed previous run
# can't leave stale Mach-O binaries lurking in the output.
rm -rf "$OUTPUT_APP"
# `cp -R` preserves symlinks, which matters for framework bundles
# that use the Versions/A + Versions/Current symlink pattern.
cp -R "$ARM64_APP" "$OUTPUT_APP"

merged_count=0
skipped_count=0
arm_only_count=0
x64_only_count=0

# Phase 1: walk the output bundle (which is the arm64 copy), merge
# each Mach-O with its x64 sibling.
while IFS= read -r -d '' arm_file; do
    rel="${arm_file#"$OUTPUT_APP"/}"
    x64_file="$X64_APP/$rel"

    # `file -b` strips the filename prefix from the output; just
    # check whether the type description starts with "Mach-O".
    file_type="$(file -b "$arm_file" 2>/dev/null || true)"
    case "$file_type" in
        "Mach-O "*) ;;
        *)
            skipped_count=$((skipped_count + 1))
            continue
            ;;
    esac

    if [ ! -f "$x64_file" ]; then
        # Mach-O exists in arm64 but not in x64. Keep the arm64
        # version untouched; the universal app will be missing x86_64
        # support for this specific binary, which is the best we can
        # do without inventing a slice. Surface a warning so a build
        # producing a lot of these doesn't go unnoticed.
        echo "  WARN: $rel exists in arm64 only; keeping arm64-thin copy"
        arm_only_count=$((arm_only_count + 1))
        continue
    fi

    # Extract each arch's slice. `lipo -thin` is a no-op for an
    # already-thin file of that arch, and extracts the slice from a
    # universal binary if the input is fat. If the slice doesn't
    # exist (e.g. a binary that's arm64-only and somehow ended up in
    # the x64 build's path), fall back to copying the input file —
    # better to ship something than to fail the merge.
    arm_thin="$(mktemp -t lipo-arm)"
    x64_thin="$(mktemp -t lipo-x64)"
    if ! lipo "$arm_file" -thin arm64 -output "$arm_thin" 2>/dev/null; then
        cp "$arm_file" "$arm_thin"
    fi
    if ! lipo "$x64_file" -thin x86_64 -output "$x64_thin" 2>/dev/null; then
        cp "$x64_file" "$x64_thin"
    fi

    # `lipo -create` builds a fat binary from the inputs. Output
    # back into the bundle, replacing the arm64-only original.
    if lipo -create "$arm_thin" "$x64_thin" -output "$arm_file" 2>/dev/null; then
        merged_count=$((merged_count + 1))
    else
        # Failed to combine. Most common cause: both inputs already
        # universal with overlapping archs. Try lipo on the originals
        # directly with -replace; if that also fails, keep the arm64
        # copy and warn.
        if ! lipo -create "$arm_file" "$x64_file" -output "$arm_file.merged" 2>/dev/null; then
            echo "  WARN: lipo failed to merge $rel; keeping arm64 copy"
        else
            mv "$arm_file.merged" "$arm_file"
            merged_count=$((merged_count + 1))
        fi
    fi
    rm -f "$arm_thin" "$x64_thin"
done < <(find "$OUTPUT_APP" -type f -print0)

# Phase 2: catch x64-only Mach-O files. These are dylibs pulled in
# by the x64 wheel of some dependency but not the arm64 wheel (or
# vice versa). Copy them in single-arch — the universal app will
# only have x86_64 support for these, but that's better than the
# binary being missing entirely on Intel hosts.
while IFS= read -r -d '' x64_file; do
    rel="${x64_file#"$X64_APP"/}"
    out_file="$OUTPUT_APP/$rel"

    [ -e "$out_file" ] && continue

    file_type="$(file -b "$x64_file" 2>/dev/null || true)"
    case "$file_type" in
        "Mach-O "*) ;;
        *) continue ;;
    esac

    echo "  WARN: $rel exists in x64 only; copying x86_64-thin into output"
    mkdir -p "$(dirname "$out_file")"
    cp "$x64_file" "$out_file"
    x64_only_count=$((x64_only_count + 1))
done < <(find "$X64_APP" -type f -print0)

echo ""
echo "lipo merge complete:"
echo "  $merged_count Mach-O files fused (arm64 + x86_64 -> universal2)"
echo "  $skipped_count non-Mach-O files copied unchanged"
[ "$arm_only_count" -gt 0 ] && echo "  $arm_only_count Mach-O files kept arm64-only (no x64 match)"
[ "$x64_only_count" -gt 0 ] && echo "  $x64_only_count Mach-O files added x64-only (no arm64 match)"

# Quick sanity check on the main executable. If lipo says the
# bundle's binary now reports both archs, the merge worked end-to-end.
APP_NAME="$(basename "$OUTPUT_APP" .app)"
MAIN_EXE="$OUTPUT_APP/Contents/MacOS/$APP_NAME"
if [ -f "$MAIN_EXE" ]; then
    echo ""
    echo "Main executable architectures:"
    lipo -archs "$MAIN_EXE" | sed 's/^/  /'
fi
