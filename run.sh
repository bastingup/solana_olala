#!/usr/bin/env bash
# Solana-olala launcher (Linux). Creates the venv on first run, installs
# dependencies, starts the backend (which also serves the frontend), and
# opens the dashboard.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
VENV="backend/.venv"

if [ ! -d "$VENV" ]; then
    "$PYTHON" -m venv "$VENV"
fi

"$VENV/bin/pip" install --quiet --requirement backend/requirements.txt

( sleep 2; xdg-open "http://127.0.0.1:8420" >/dev/null 2>&1 || true ) &

exec "$VENV/bin/python" backend/run.py
