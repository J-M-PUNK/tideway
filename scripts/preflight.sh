#!/usr/bin/env bash
# Run all four pre-PR checks in sequence. Mirrors what CI runs on
# every push, so a clean local run means CI will pass too.
#
#   pytest tests/                  — backend
#   npx tsc -b --noEmit            — typecheck
#   npm run lint:all               — eslint + stylelint + htmlhint + prettier
#   npm test                       — vitest
#
# Exits non-zero on the first failure so it pairs cleanly with
# `git commit && ./scripts/preflight.sh && git push`.

set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "No venv at $ROOT/.venv — create one and run pip install -r requirements.txt" >&2
  exit 1
fi

echo "==> pytest"
"$PY" -m pytest tests/

echo "==> typecheck (tsc)"
(cd web && npx tsc -b --noEmit)

echo "==> lint:all (eslint, stylelint, htmlhint, prettier)"
(cd web && npm run lint:all)

echo "==> vitest"
(cd web && npm test)

echo
echo "All checks passed. Branch is ready for PR."
