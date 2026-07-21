#!/usr/bin/env bash
# Watch a running VM benchmark and auto-save its results locally when it finishes.
#
# Usage:
#   scripts/watch_and_save.sh <vm_run_subdir> <scheduler_pid> [extra save_run.sh args...]
#
# Typical use (run in the background right after launching a benchmark on the VM):
#   scripts/watch_and_save.sh rat_run_repo2run 1452525 --agent repo2run &
#   scripts/watch_and_save.sh rat_run_xxx_smoke 99999 --smoke &
#
# Polls every RATBENCH_POLL secs (default 300) until the scheduler PID exits, then calls
# save_run.sh (which pulls output + aggregate + logs, computes ESSR, writes a README, and
# appends "_smoke" for smoke runs).
set -euo pipefail
HOST="${RATBENCH_HOST:-root@167.233.64.96}"
BASE="${RATBENCH_BASE:-/opt/rat-bench-integration}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15"
INTERVAL="${RATBENCH_POLL:-300}"
MAXITERS="${RATBENCH_MAXITERS:-160}"

RUN="${1:?usage: watch_and_save.sh <vm_run_subdir> <scheduler_pid> [save args]}"; shift
PID="${1:?need scheduler pid}"; shift
EXTRA=("$@")

for i in $(seq 1 "$MAXITERS"); do
  ALIVE=$(ssh $SSH_OPTS "$HOST" "kill -0 $PID 2>/dev/null && echo yes || echo no" 2>/dev/null || echo no)
  ROWS=$(ssh $SSH_OPTS "$HOST" "find $BASE/$RUN/output -name _result_row.json 2>/dev/null | wc -l | tr -d ' '" 2>/dev/null || echo 0)
  echo "[watch $i $(date +%H:%M)] $RUN alive=$ALIVE rows=$ROWS"
  [ "$ALIVE" = "no" ] && break
  sleep "$INTERVAL"
done
echo "[watch] $RUN finished -> save_run.sh"
"$SCRIPT_DIR/save_run.sh" "$RUN" "${EXTRA[@]}"
