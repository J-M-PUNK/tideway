#!/bin/sh
# Flatpak entry point. Runs Tideway under the GNOME runtime's
# Python so pywebview imports `gi` and uses the native WebKitGTK
# backend (no browser fallback). Not a frozen build — desktop.py's
# frozen-only paths are inert here.
export PYTHONPATH=/app/share/tideway
cd /app/share/tideway || exit 1
exec python3 desktop.py "$@"
