# Linux: native Flatpak

The PyInstaller-frozen Linux AppImage had no bundled webview, so
pywebview fell back to opening the app in the system browser.
That was deemed not acceptable. The Flatpak built under the GNOME
49 runtime replaces it with a real native window. The AppImage
build was retired in v1.11.0 once the Flatpak shipped — this doc
covers the Flatpak's scope, architecture, and the staged build
that got it from manifest scaffold to a GitHub-Pages-hosted
auto-updating remote.

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
specs and the existing release pipeline.

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
3. Distribution + auto-updater + CI. **Done.** Self-hosted over
   Flathub: keeps release control on the existing tag-driven
   GitHub Actions cadence; no external review treadmill. AppImage
   was retired in v1.11.0; the Flatpak is the only Linux artifact.
   - **3a — In-app updater.** `_running_in_flatpak()` (server.py)
     checks `/.flatpak-info` and `$FLATPAK_ID`. The
     `/api/update-check` response carries `kind` (`"flatpak"` or
     `"installer"`), and `/api/update/install` returns HTTP 409
     with the exact `flatpak update --user
     com.tidaldownloader.Tideway` command inside the sandbox. The
     `UpdateBanner` reads `kind` and replaces the in-app "Install
     now" button with that command rendered inline. Pinned by
     `tests/test_update_flatpak.py` and
     `web/src/components/UpdateBanner.test.tsx`.
   - **3b — CI.** `build-linux-flatpak` in `.github/workflows/
     release.yml` installs flatpak-builder on a stock ubuntu-22.04
     runner, pulls the GNOME 49 runtime + Sdk + Node20 SDK
     extension from Flathub, runs the manifest with `--repo=repo`,
     and produces two artifacts: a `.flatpak` bundle (attached to
     the GitHub Release, signed by `sign-release.sh` alongside the
     DMG / .exe) and the OSTree repo directory.
   - **3c — Distribution.** `publish-flatpak-repo` deploys the
     OSTree repo to the `gh-pages` branch on every tag via
     `peaceiris/actions-gh-pages`. GitHub Pages serves it at
     `https://j-m-punk.github.io/tideway/`, with a
     `tideway.flatpakrepo` subscribe file and an `index.html`
     landing page next to the repo. Users run `flatpak remote-add
     --user tideway https://j-m-punk.github.io/tideway/
     tideway.flatpakrepo` and `flatpak install tideway
     com.tidaldownloader.Tideway`; `flatpak update` thereafter.

## Runtime version

The manifest pins `org.gnome.Platform//49` (freedesktop 25.08 base,
Python 3.13). The original scaffold used 47, which went EOL on
2025-10-15. The bump is mechanical: change the runtime-version,
re-pull `org.freedesktop.Sdk.Extension.node20//25.08`, regenerate
`python3-requirements.json` with `--runtime org.gnome.Sdk//49` so
wheel selection matches the new runtime's Python ABI, rebuild.

## Trust model

The published repo is served over HTTPS from GitHub Pages, so
content authenticity rides on GitHub's TLS chain. The `.flatpak`
bundle attached to each release is also minisign-signed by the
same `scripts/sign-release.sh` step that signs the DMG and .exe,
so direct-bundle installs from the release page are end-to-end
verifiable.

Signing OSTree commits with a project GPG key closes the remaining
gap (a compromised gh-pages host could otherwise substitute
malicious commits for users on the auto-updating remote). The CI
side is already wired through `release.yml`'s `build-linux-flatpak`
job — it's gated on the `OSTREE_GPG_KEY_ID` secret being present
and is a no-op until you do the one-time setup. See
[docs/flatpak-gpg-signing.md](flatpak-gpg-signing.md) for the
key-generation, secret-stashing, and validation steps.
