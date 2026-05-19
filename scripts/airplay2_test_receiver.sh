#!/usr/bin/env bash
#
# Dev-only AirPlay 2 test receiver.
#
# Clones openairplay/airplay2-receiver into a gitignored local
# directory and runs it with verbose logging so the AirPlay 2
# sender (feature/airplay2) can be developed against a receiver
# whose internal state is readable, before any black-box TV.
#
# This is NOT bundled and NOT part of the app. See
# docs/airplay2-sender.md.
#
# Usage:
#   scripts/airplay2_test_receiver.sh [network-interface]
#
# The receiver advertises over mDNS on the given interface (default
# auto-detected by the upstream project). Once it is running it
# shows up as an AirPlay 2 device on the LAN; point the sender at
# it and watch this terminal for the receiver-side handshake trace.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/.airplay2-test"
REPO="https://github.com/openairplay/airplay2-receiver.git"
SRC="$DEST/airplay2-receiver"
VENV="$DEST/venv"

mkdir -p "$DEST"

if [ ! -d "$SRC/.git" ]; then
  echo "Cloning airplay2-receiver into $SRC"
  git clone --depth 1 "$REPO" "$SRC"
else
  echo "Updating airplay2-receiver in $SRC"
  git -C "$SRC" pull --ff-only || true
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating receiver venv at $VENV"
  python3 -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --quiet --upgrade pip
if [ -f "$SRC/requirements.txt" ]; then
  "$VENV/bin/python" -m pip install --quiet -r "$SRC/requirements.txt"
fi

IFACE="${1:-}"
echo
echo "Starting AirPlay 2 test receiver. Leave this running and point"
echo "the sender at it. Ctrl+C to stop."
echo
cd "$SRC"
if [ -n "$IFACE" ]; then
  exec "$VENV/bin/python" ap2-receiver.py -m TidewayDevTestRx -n "$IFACE"
else
  exec "$VENV/bin/python" ap2-receiver.py -m TidewayDevTestRx
fi
