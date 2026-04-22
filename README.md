# Tidal Downloader

A Spotify-style desktop client for Tidal with downloads, Last.fm scrobbling,
listening stats, bit-perfect gapless playback (PyAV + sounddevice), 10-band
equalizer, output-device picker, global media keys, and more. FastAPI backend
wraps the download pipeline + audio engine; a Vite + React + Tailwind frontend
provides the UI inside a pywebview window.

## Install (released builds)

Head to [Releases](https://github.com/YOUR_USERNAME/tidal-downloader/releases)
and grab the latest:

- **macOS** — `TidalDownloader-macOS.zip`. Unzip, drag the .app to
  `/Applications`.
- **Windows** — `TidalDownloader-Windows.zip`. Unzip anywhere, run
  `TidalDownloader.exe` from the folder.

### Why does the OS warn me on first launch?

The builds aren't code-signed — signing requires paid developer certificates
(Apple $99/yr, Microsoft $200+/yr) that aren't worth it for an open-source
hobby project. Both OSes show a scary-looking warning once; after you open
the app the first time, you won't see it again.

- **macOS Gatekeeper** — first launch: right-click (or Control-click) the
  `.app` → **Open** → confirm **Open** in the dialog. After that, normal
  double-click works forever.
- **Windows SmartScreen** — on first run: click **More info** in the blue
  dialog, then **Run anyway**.

If you prefer to verify the build yourself, clone the repo and follow
**Run it from source** below — PyInstaller produces the same bundle you'd
download from Releases.

## Stack

- **Backend:** FastAPI, `tidalapi`, `mutagen` (reuses the `app/` package that
  the legacy desktop app uses).
- **Frontend:** Vite + React + TypeScript + Tailwind CSS + shadcn/ui-style
  primitives + React Router.
- **Realtime:** Server-Sent Events for download progress.

## Run it from source

First time only:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
(cd web && npm install)
```

Start both servers with one command:

```bash
./run.sh
```

- FastAPI: <http://127.0.0.1:8000> (JSON API + `/docs`)
- Web UI:  <http://127.0.0.1:5173>

On first launch, click "Login with Tidal", enter the code on the Tidal page,
then come back — the session is cached locally in `tidal_session.json`.

## Layout

```
app/              shared Python logic (tidal client, downloader, metadata)
server.py         FastAPI entry point
main.py           legacy customtkinter desktop app (still works)
web/              Vite + React frontend
run.sh            one-command dev launcher
```

## Notes

- **Dev runs**: `ffmpeg` is a system dependency — install via Homebrew
  (`brew install ffmpeg`) and it's picked up from PATH.
- **Shipped builds** bundle ffmpeg automatically — end users of the
  packaged app don't install anything. Run `scripts/fetch_ffmpeg.sh`
  once before building to populate `vendor/ffmpeg/<os>/`.
- Quality, output folder, filename template, etc. are editable under
  Settings in the UI and persisted to `settings.json`.

## Building a distributable

### Icons (once)

Drop a 1024×1024 PNG at `assets/icon-source.png`, then:

```
scripts/build_icons.sh
```

Produces `assets/icon.icns` (macOS) and `assets/icon.ico` (Windows).
Both specs already look for these files; without them the build
ships a generic PyInstaller placeholder icon.

### macOS

```
scripts/fetch_ffmpeg.sh                # once, populates vendor/ffmpeg/macos/
npm --prefix web run build             # frontend
.venv/bin/pyinstaller TidalDownloader-mac.spec --noconfirm
scripts/build_dmg.sh                   # outputs dist/TidalDownloader-<ver>.dmg
```

Ship the `.dmg`. Users drag-to-Applications and launch. No Homebrew,
no ffmpeg install, no VLC install — everything's in the bundle.

### Windows

```
bash scripts/fetch_ffmpeg.sh           # Git Bash / WSL — populates vendor/ffmpeg/windows/
npm --prefix web run build
.venv\Scripts\pyinstaller TidalDownloader-win.spec --noconfirm
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" scripts\TidalDownloader.iss
```

Outputs `dist/TidalDownloader-setup-<ver>.exe`. Users run it, hit
Next/Next/Install.

### Auto-update

The app polls GitHub's Releases API at `/api/update-check` once on
launch (result cached for 1 hour). When a newer tag is out, a banner
surfaces across the top of the UI. "Install now" downloads the
right asset for the user's OS from the latest release, opens it,
then quits the app so the installer can replace the bundle. The
release asset names must match:

- macOS: `TidalDownloader-<version>.dmg`
- Windows: `TidalDownloader-setup-<version>.exe`

Both of the build scripts above produce files in that format, so a
release is just `gh release create vX.Y.Z dist/TidalDownloader-X.Y.Z.dmg
dist/TidalDownloader-setup-X.Y.Z.exe`.
