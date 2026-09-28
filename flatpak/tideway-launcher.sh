#!/bin/sh
# Flatpak entry point. Runs Tideway under the GNOME runtime's
# Python so pywebview imports `gi` and uses the native WebKitGTK
# backend (no browser fallback). Not a frozen build — desktop.py's
# frozen-only paths are inert here.
export PYTHONPATH=/app/share/tideway

# Pin the GUI backend to GTK. pywebview otherwise picks one itself, and
# its Linux heuristic is wrong for this build:
#
#     forced_gui = 'qt' if 'KDE_FULL_SESSION' in os.environ else None
#     (webview/guilib.py)
#
# On any KDE session that forces the Qt backend to be tried first. This
# runtime ships GTK 3 and WebKit2 4.1 and no Qt bindings at all, so Qt
# can only fail — it imports qtpy, which isn't here, and logs a
# ModuleNotFoundError traceback that reads like the cause of a failed
# startup while being nothing of the kind (#424 was reported on KDE
# Plasma 6 for exactly this reason).
#
# Setting this doesn't remove the Qt fallback, it just stops KDE being
# a special case: pywebview tries GTK then Qt, the same order every
# other desktop already gets. If GTK ever stops resolving here, both
# backends fail, pywebview raises, and desktop.py falls back to the
# browser — which is the honest outcome rather than a silent one.
export PYWEBVIEW_GUI=gtk

cd /app/share/tideway || exit 1
exec python3 desktop.py "$@"
