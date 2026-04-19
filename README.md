# Tidal Downloader

A Spotify-style web UI for downloading music from Tidal. FastAPI backend wraps
the existing Python download pipeline; a Vite + React + Tailwind + shadcn/ui
frontend provides the UI.

## Stack

- **Backend:** FastAPI, `tidalapi`, `mutagen` (reuses the `app/` package that
  the legacy desktop app uses).
- **Frontend:** Vite + React + TypeScript + Tailwind CSS + shadcn/ui-style
  primitives + React Router.
- **Realtime:** Server-Sent Events for download progress.

## Run it

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
