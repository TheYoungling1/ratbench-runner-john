#!/usr/bin/env bash
# env-bench entry point. Sets the harness ROOT vars to THIS repo (not /opt/*),
# loads .env, and runs the benchmark through the repo's own venv so the
# "system python has no dotenv" gotcha can never happen.
#
#   ./run_bench.sh <variety> [--tier all] [--limit N] [--only owner/repo] \
#                  [--concurrency N] [--llm MODEL] [--repos-json PATH]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

export HARNESS_ROOT="$ROOT/harness"
export RAT_ROOT="$ROOT/rat"
export AGENTS_ROOT="$ROOT/agents"
export BENCH_ROOT="$ROOT/bench"
export RUNS_ROOT="$ROOT/runs"
export SKIP_PROVISION=1                 # vendored agents: no git fetch/reset

# load credentials
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "ERROR: $ROOT/.venv not found — run ./setup.sh first." >&2
  exit 1
fi
if [ $# -eq 0 ]; then
  echo "usage: ./run_bench.sh <variety> [bench args...]" >&2
  echo "varieties: $(grep -oE '^\[variety\.[^]]+' "$HARNESS_ROOT/varieties.toml" | sed 's/\[variety\.//' | tr '\n' ' ')" >&2
  exit 2
fi
exec "$PY" "$HARNESS_ROOT/bench" "$@"
