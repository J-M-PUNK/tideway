# Linux: native Flatpak

The Linux AppImage has no bundled webview, so pywebview falls back
to opening the app in the system browser. That was deemed not
acceptable. This doc covers the Flatpak that replaces it with a
real native window, the scope, the staged plan, and the open
decisions.

## Why Flatpak

A frozen (PyInstaller) Linux build can't reuse the host's
PyGObject/WebKit2GTK (they're bound to the system Python), and
bundling that stack into the AppImage is the fragile,
high-maintenance path that pushed the build to browser-fallback in
the first place. The GNOME Flatpak runtime supplies GTK,
WebKit2GTK and PyGObject already, so the app runs under the
runtime's Python with `import gi` working and pywebview's GTK
backend giving a real native window. Flatpak therefore *replaces
PyInstaller for Linux* — there is no freeze step in this path.

macOS and Windows are unaffected: they keep their PyInstaller
specs and the existing signed-AppImage-free pipeline.

## Architecture

- Runtime: `org.gnome.Platform` // SDK `org.gnome.Sdk` (version
  pinned in the manifest). Brings Python 3, GTK3, WebKit2GTK,
  PyGObject.
- App id: `com.tidaldownloader.Tideway` (matches the existing
  `com.tidaldownloader.app` macOS bundle namespace; Flatpak
  convention capitalises the final component).
- Python deps: generated offline+hashed from `requirements.txt`
  via `flatpak-pip-generator` into a `python3-requirements.json`
  module (Stage 1). No network during the build sandbox.
- PortAudio: the GNOME runtime does not ship libportaudio, and
  `sounddevice` needs it. Built as an autotools module in the
  manifest.
- Web bundle: built with the freedesktop Node SDK extension inside
  the build sandbox (`npm ci && npm run build`), then `web/dist`
  is installed alongside the Python source.
- Entry point: `desktop.py` under the runtime Python. pywebview
  selects the GTK/WebKit backend (no browser fallback).

## finish-args (sandbox permissions)

- `--share=network` — Tidal / Spotify / Last.fm APIs.
- `--socket=pulseaudio` — audio out (PortAudio; PipeWire's pulse
  shim covers it).
- `--socket=wayland` + `--socket=fallback-x11` + `--device=dri` —
  the WebKitGTK window + GPU compositing.
- `--filesystem=xdg-download` — downloads land in ~/Downloads.
- `--talk-name=org.freedesktop.Notifications` — libnotify desktop
  notifications.

Kept deliberately narrow; widen only when a feature provably needs
it.

## Staged plan

0. Branch, this doc, manifest scaffold. (current)
1. `flatpak-builder` builds in the VM. Highest technical risk:
   PyAV/libav, curl_cffi (native), numpy, sounddevice all building
   and importing under the GNOME runtime sandbox + PortAudio.
2. Headless VM run: prove pywebview selects the GTK/WebKit backend
   (native, not browser-fallback) and the server comes up with no
   crash. Visual confirmation of an actual window needs a display
   (a GUI VM or a real Linux desktop) — explicitly out of scope of
   the headless harness and noted as a manual check.
3. Distribution + auto-updater + CI:
   - **Open decision:** self-hosted Flatpak repo (keeps release
     control + the existing GitHub-Actions cadence; lean) vs
     Flathub (slower external review, but discoverability and
     automatic user updates). Decided with a working build in
     hand.
   - The in-app self-updater currently downloads + minisign-
     verifies the AppImage and execs it. On Flatpak that's wrong:
     updates come from the Flatpak remote. The Linux updater must
     detect Flatpak (`/.flatpak-info` / `$FLATPAK_ID`) and either
     defer to `flatpak update` or hide the in-app install action.
   - `release.yml` gains a `flatpak-builder` job (the
     `flatpak/flatpak-github-actions` actions) producing a bundle
     and/or pushing the repo; the AppImage job's fate (drop vs
     keep as a fallback artifact) is part of the Stage 3 decision.

## Note

The AppImage `/api/open-external` fix (PR #163) is irrelevant to
the Flatpak path — with a native window the browser-fallback /
external-open seam isn't taken. It still matters for the AppImage
until/unless the AppImage is retired in Stage 3.
