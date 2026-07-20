#!/usr/bin/env bash
# Save a RAT-bench run's results from the VM into the local results/ folder.
#
# Usage:
#   scripts/save_run.sh <vm_run_subdir> [--smoke] [--agent NAME] [--name NAME] [--log VM_LOG_PATH]
#
# Examples:
#   scripts/save_run.sh rat_run_rat_corrected                 # -> results/rat/<date>-rat_corrected/
#   scripts/save_run.sh rat_run_dockeragent --name 2026-06-07-baseline
#   scripts/save_run.sh rat_run_repo2run --smoke              # -> results/repo2run/<date>-repo2run_smoke/
#
# Behaviour:
#   - Pulls <run>/output (all _result_row.json + run.log + junit + ...), rat_results.json,
#     and any *.log in the run dir (+ the matching parent _run50_*.log scheduler log).
#   - Infers the agent (rat / dockeragent / repo2run) from the run-dir name unless --agent given.
#   - Appends "_smoke" to the destination when --smoke is passed, OR the run-dir name contains
#     "smoke", OR the run scored <=2 repos (single-repo smoke).
#   - Writes a README.md with the config + computed ESSR (paper macro over executed) + provenance.
set -euo pipefail

HOST="${RATBENCH_HOST:-root@167.233.64.96}"
BASE="${RATBENCH_BASE:-/opt/rat-bench-integration}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS="${RATBENCH_RESULTS:-$(cd "$SCRIPT_DIR/.." && pwd)/results}"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=20"

RUN=""; SMOKE=0; AGENT=""; NAME=""; LOG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --smoke) SMOKE=1 ;;
    --agent) AGENT="${2:-}"; shift ;;
    --name)  NAME="${2:-}";  shift ;;
    --log)   LOG="${2:-}";   shift ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *)  RUN="$1" ;;
  esac; shift
done
[ -n "$RUN" ] || { echo "usage: save_run.sh <vm_run_subdir> [--smoke] [--agent N] [--name N] [--log P]" >&2; exit 2; }
RUN="${RUN%/}"

ROWS=$(ssh $SSH_OPTS "$HOST" "find $BASE/$RUN/output -name _result_row.json 2>/dev/null | wc -l | tr -d ' '" 2>/dev/null || echo 0)
[ "${ROWS:-0}" -gt 0 ] || { echo "ERROR: no _result_row.json under $BASE/$RUN/output (rows=${ROWS:-0})" >&2; exit 1; }

if [ -z "$AGENT" ]; then
  case "$RUN" in
    *repo2run*)    AGENT=repo2run ;;
    *dockeragent*) AGENT=dockeragent ;;
    *rat*)         AGENT=rat ;;
    *)             AGENT=unknown ;;
  esac
fi

if [ "$SMOKE" -eq 0 ]; then
  case "$RUN" in *smoke*) SMOKE=1 ;; esac
  [ "$ROWS" -le 2 ] && SMOKE=1
fi

if [ -z "$NAME" ]; then NAME="$(date +%F)-$(echo "$RUN" | sed 's#^rat_run_##; s#/#-#g')"; fi
if [ "$SMOKE" -eq 1 ]; then case "$NAME" in *_smoke) : ;; *) NAME="${NAME}_smoke" ;; esac; fi

DEST="$RESULTS/$AGENT/$NAME"
mkdir -p "$DEST"
echo "Saving $HOST:$BASE/$RUN  ->  $DEST   (agent=$AGENT rows=$ROWS smoke=$SMOKE)"

# Package on VM: output/ + rat_results.json (if present) + any *.log inside the run dir.
ssh $SSH_OPTS "$HOST" "cd $BASE/$RUN && tar czf /tmp/_saverun.tgz output \$(ls rat_results.json 2>/dev/null) \$(ls *.log 2>/dev/null) 2>/dev/null"
scp $SSH_OPTS "$HOST:/tmp/_saverun.tgz" "/tmp/_saverun.tgz" >/dev/null
tar xzf /tmp/_saverun.tgz -C "$DEST"

# Parent scheduler log (auto: _run50_<suffix>.log; or explicit --log)
[ -z "$LOG" ] && LOG="$BASE/_run50_$(echo "$RUN" | sed 's#^rat_run_##').log"
scp $SSH_OPTS "$HOST:$LOG" "$DEST/" >/dev/null 2>&1 && echo "  + scheduler log $(basename "$LOG")" || true

# Compute ESSR (paper macro over executed) from the pulled rows for the README.
ESSR=$(python3 - "$DEST/output" <<'PY' 2>/dev/null || echo "n/a"
import json, glob, sys, statistics
rows=[]
for p in glob.glob(sys.argv[1]+"/*/*/_result_row.json"):
    try: rows.append(json.load(open(p)))
    except Exception: pass
ex=[r.get("pytest_pass_rate",0) for r in rows if r.get("pytest_executed")]
macro=round(statistics.mean(ex),4) if ex else 0.0
print(f"{macro} (executed {len(ex)}/{len(rows)})")
PY
)

cat > "$DEST/README.md" <<EOF
# ${AGENT} run — ${NAME}

- Source: \`${HOST}:${BASE}/${RUN}\`
- Saved: $(date +%F) · rows: ${ROWS} · smoke: $([ "$SMOKE" -eq 1 ] && echo yes || echo no)
- **ESSR (paper macro, ÷ executed): ${ESSR}**

Contents: \`output/<org>/<repo>/\` per-repo (_result_row.json, run.log, junit_report.xml, …),
\`rat_results.json\` (aggregate), and the scheduler/smoke log(s).
$([ "$SMOKE" -eq 1 ] && echo "
> SMOKE run — single/few repos, for validation only; not a baseline.")
EOF

echo "Done. ESSR=${ESSR}"
echo "  $DEST"
