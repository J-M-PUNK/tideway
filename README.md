# Tidal Downloader

A Spotify-style desktop client for Tidal with downloads, Last.fm scrobbling,
listening stats, native audio engine (libvlc), equalizer, output-device picker,
global media keys, and more. FastAPI backend wraps the download pipeline +
libvlc playback; a Vite + React + Tailwind frontend provides the UI inside a
pywebview window.

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

- `ffmpeg` is a system dependency — install via Homebrew: `brew install ffmpeg`.
- Quality, output folder, filename template, etc. are editable under Settings
  in the UI and persisted to `settings.json`.
