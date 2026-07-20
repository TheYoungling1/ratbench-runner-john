#!/usr/bin/env bash
# One-time setup: creates .venv and installs dependencies.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"

echo "== env-bench setup =="

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not found. The benchmark builds a container per repo — install Docker and start the daemon." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: docker daemon not reachable. Start Docker and retry." >&2
  exit 1
fi
"$PY" -c 'import sys; assert sys.version_info[:2] >= (3,10), f"Python 3.10+ required, have {sys.version.split()[0]}"'

echo "Creating .venv with $("$PY" --version 2>&1) ..."
"$PY" -m venv .venv
. .venv/bin/activate
pip install -U pip wheel
pip install -r requirements.txt

echo
echo "Setup complete. Next:"
echo "  cp .env.example .env       # then add your OPENROUTER_API_KEY"
echo "  ./run_bench.sh john-planner-v3 --tier all --limit 2 --concurrency 2   # smoke"
