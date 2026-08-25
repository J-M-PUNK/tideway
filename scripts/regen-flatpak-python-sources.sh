#!/usr/bin/env bash
# Regenerate flatpak/python3-requirements.json from requirements.txt.
#
# Shared by update-flatpak-python-sources.yml (which commits the result
# on main and deploy/**) and flatpak-build-check.yml (which regenerates
# into its working tree so a PR builds against its own requirements.txt
# without committing anything to the branch). The generator invocation
# below is long enough that keeping two copies of it in YAML would
# drift; this file is the single source of truth for both.
#
# Requires: the GNOME 49 Sdk installed as a flatpak, python3, and pip.
set -euo pipefail

# Pinned to a commit rather than tracking master so the generated file
# is reproducible — an upstream change must be adopted deliberately
# rather than silently altering what gets committed to main. Same
# commit the node generator is pinned to in
# .github/flatpak-node-generator-requirements.txt; bump them together.
GENERATOR_REF="737c0085912f9f7dabf9341d4608e2a77a51a73a"

# The historical `pip/flatpak-pip-generator` path is a git symlink to
# the .py file, and raw.githubusercontent serves a symlink as its
# one-line target text — fetch the real file.
curl -fsSL -o flatpak-pip-generator \
  "https://raw.githubusercontent.com/flatpak/flatpak-builder-tools/${GENERATOR_REF}/pip/flatpak-pip-generator.py"
head -1 flatpak-pip-generator | grep -q python || {
  echo "downloaded file is not the generator script:" >&2
  head -3 flatpak-pip-generator >&2
  exit 1
}

# Matches the script's PEP 723 inline metadata block.
python3 -m pip install --quiet "requirements-parser<1.0.0,>=0.11.0" "packaging>=23.0"

# --runtime resolves wheels by running pip inside the Sdk, so the Python
# ABI matches the manifest's pinned org.gnome.Platform//49.
#
# --requirements-file mirrors docs/linux-flatpak.md ("generated
# offline+hashed from requirements.txt"). Platform markers are resolved
# for the Linux/GNOME target, so darwin-only pyobjc drops out and
# linux-only dbus-next is included automatically.
#
# --prefer-wheels takes a per-package list upstream now (it used to be a
# boolean): these are the native-extension packages with no PEP-517
# build backend inside the sandbox, so their sdists can't build and the
# prebuilt manylinux wheel is required. When a new native dep joins
# requirements.txt, add it here — the build-check failing on a missing
# build backend is the symptom. Pure-Python packages build fine from
# sdists and don't need listing.
#
# --ignore-pkg drops build-only tools from the runtime set: pyinstaller
# freezes the macOS/Windows bundles and has no place inside the Flatpak
# (docs/linux-flatpak.md: "there is no freeze step in this path"); its
# module also fails to pip install in the sandbox, which is how its
# inclusion surfaces.
python3 flatpak-pip-generator \
  --runtime="org.gnome.Sdk//49" \
  --prefer-wheels="aiohttp,av,cffi,curl-cffi,httptools,numpy,orjson,pillow,pydantic-core,pyyaml,rapidfuzz,scipy,uvloop,watchfiles,zeroconf" \
  --ignore-pkg=pyinstaller \
  --requirements-file=requirements.txt \
  --output=flatpak/python3-requirements
