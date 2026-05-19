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

0. Branch, this doc, manifest scaffold. **Done.**
1. `flatpak-builder` builds in the VM. **Done.** All 20 Python
   modules + PortAudio + the web bundle build clean under the
   GNOME runtime sandbox; payload comes out around 526 MB. Two
   lessons baked into the manifest:
   - **Rust and Cython transitives need prebuilt wheels.**
     Offline-hashed sdists fail in the sandbox because their
     PEP-517 build backends (`maturin`, `setuptools-rust`,
     `Cython`) aren't available. We pass an explicit
     `--prefer-wheels` list to `flatpak-pip-generator` covering
     orjson, curl-cffi, pillow, av, numpy, scipy, rapidfuzz,
     pydantic-core, watchfiles, httptools, pyyaml, uvloop,
     aiohttp, zeroconf, cffi. Wheels are arch-conditional
     (`only-arches: x86_64 / aarch64`) so the same module file
     works on either build host.
   - **The npm install needs offline sources too.** `npm ci`
     against the live registry fails with the sandbox's
     network-disabled state. `flatpak-node-generator` produces
     `node-sources.json` from `package-lock.json`, the manifest
     points `npm_config_cache` and `XDG_CACHE_HOME` at the
     generated `flatpak-node/` tree, and `npm ci --offline`
     succeeds.
2. Headless VM run. **Done.** Inside the GNOME 49 sandbox
   (Python 3.13.13, PyGObject 3.50.1, GTK 3.0, WebKit2 4.1) the
   FastAPI server boots, `Uvicorn running on http://127.0.0.1:
   47823` lands, and pywebview reaches into the GTK backend
   trying to create a window. Under `xvfb-run` the window
   creation fails with `Gtk-WARNING: cannot open display` and
   `Invalid MIT-MAGIC-COOKIE-1 key` — the X11 cookie can't be
   shared into the Flatpak sandbox by xvfb, but the failure mode
   itself is the proof: pywebview only emits Gtk-WARNINGs
   because GTK is the backend it selected. No
   `webbrowser`-fallback log, no missing-`gi` import, no
   "GTK cannot be loaded". The native dependency stack (PyAV,
   numpy, scipy, sounddevice, curl_cffi, etc.) loads at runtime,
   not just at import. Visual confirmation of an actual window
   needs a real display and stays out of scope here.
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

## Runtime version

The manifest pins `org.gnome.Platform//49` (freedesktop 25.08 base,
Python 3.13). The original scaffold used 47, which went EOL on
2025-10-15. The bump is mechanical: change the runtime-version,
re-pull `org.freedesktop.Sdk.Extension.node20//25.08`, regenerate
`python3-requirements.json` with `--runtime org.gnome.Sdk//49` so
wheel selection matches the new runtime's Python ABI, rebuild.

## Note

The AppImage `/api/open-external` fix (PR #163) is irrelevant to
the Flatpak path — with a native window the browser-fallback /
external-open seam isn't taken. It still matters for the AppImage
until/unless the AppImage is retired in Stage 3.
