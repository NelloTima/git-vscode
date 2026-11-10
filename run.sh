#!/usr/bin/env bash
# Start both apps for local development: ViajaMoz on :5001, AlugaMoz on :5002.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && { set -a; . ./.env; set +a; }
PY=${PY:-.venv/bin/python}
"$PY" -m viajamoz.app &
"$PY" -m alugamoz.app &
trap 'kill 0' EXIT INT TERM
echo "ViajaMoz → http://localhost:5001   AlugaMoz → http://localhost:5002"
wait
