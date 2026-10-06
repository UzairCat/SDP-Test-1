#!/usr/bin/env bash
# RAT — Repo Analysis Tool
# Installs dependencies and starts the dashboard at http://127.0.0.1:8000.
# Usage: ./start.sh
set -e
cd "$(dirname "$0")"

# Pick a Python interpreter.
PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python

# Prefer a local virtual environment (.venv): keeps the system Python clean and
# works around Ubuntu's "externally managed environment" pip restriction.
if [ ! -x .venv/bin/python ]; then
  echo "==> Creating virtual environment .venv ..."
  "$PY" -m venv .venv
fi

echo "==> Installing dependencies ..."
.venv/bin/pip install --quiet -r requirements.txt

echo "==> Starting RAT at http://127.0.0.1:8000  (Ctrl+C to stop)"
exec .venv/bin/python run.py
