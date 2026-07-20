# Source this for an interactive session (e.g. to run the measure stage by hand):
#   source env.sh
# Then `python -m bench.unified_bench ...` and `./run_bench.sh ...` both work.
_EB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export HARNESS_ROOT="$_EB_ROOT/harness"
export RAT_ROOT="$_EB_ROOT/rat"
export AGENTS_ROOT="$_EB_ROOT/agents"
export BENCH_ROOT="$_EB_ROOT/bench"
export RUNS_ROOT="$_EB_ROOT/runs"
export SKIP_PROVISION=1
export PYTHONPATH="$_EB_ROOT/bench:${PYTHONPATH:-}"
if [ -f "$_EB_ROOT/.env" ]; then set -a; . "$_EB_ROOT/.env"; set +a; fi
if [ -f "$_EB_ROOT/.venv/bin/activate" ]; then . "$_EB_ROOT/.venv/bin/activate"; fi
echo "env-bench ready (ROOTs under $_EB_ROOT). Run: ./run_bench.sh <variety> ..."
