#!/usr/bin/env bash
# Run the full Solana-olala test suite inside the project venv.
set -euo pipefail
cd "$(dirname "$0")/.."

VENV="backend/.venv"
if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet -r backend/requirements.txt
fi
"$VENV/bin/pip" install --quiet -r tests/requirements-dev.txt

exec "$VENV/bin/python" -m pytest tests/ "$@"
