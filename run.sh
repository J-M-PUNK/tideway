#!/usr/bin/env bash
# Dev launcher: starts FastAPI (:8000) and Vite (:5173) together.
# Ctrl+C stops both.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "No venv at $ROOT/.venv — create one and run: pip install -r requirements.txt" >&2
  exit 1
fi

if [ ! -d "$ROOT/web/node_modules" ]; then
  echo "Installing frontend deps…"
  (cd "$ROOT/web" && npm install)
fi

cleanup() {
  trap - EXIT INT TERM
  [[ -n "${API_PID:-}" ]] && kill "$API_PID" 2>/dev/null || true
  [[ -n "${WEB_PID:-}" ]] && kill "$WEB_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Starting FastAPI on http://127.0.0.1:8000"
"$PY" -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload &
API_PID=$!

echo "Starting Vite on http://127.0.0.1:5173"
(cd "$ROOT/web" && npm run dev -- --host 127.0.0.1 --port 5173) &
WEB_PID=$!

wait
