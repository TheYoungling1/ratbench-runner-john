#!/usr/bin/env bash
# Provision a machine for the `setupx` benchmark arm. Idempotent — safe to re-run.
#
# The harness's own provisioning (runner/provision.py) only handles varieties that declare a
# `branch`; setupx is a baseline with branch = None, so nothing automates any of this. A git pull
# updates tools/setupx-bench.patch but NEVER re-applies it to an existing checkout — that exact
# gap produced a run with null tokens while agent_settings reported setupx_patched: true.
# Re-running this script after every pull is the fix.
#
# Usage:  ./tools/provision_setupx.sh            # full setup incl. the XPU store
#         ./tools/provision_setupx.sh --no-xpu   # skip pgvector; XPU-off arm only
#
# Requires in .env: DEEPSEEK_API_KEY, and for the XPU store OPENROUTER_API_KEY.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SETUPX_ROOT="${SETUPX_ROOT:-$HOME/agents/SetupX}"
SETUPX_PIN="85de35515c45954b72afb9678dfd172855bfb847"
PATCH="$REPO_ROOT/tools/setupx-bench.patch"
PG_NAME="${PG_NAME:-setupx-xpu}"
PG_PORT="${PG_PORT:-5433}"
PG_PASS="${PG_PASS:-setupx}"
WANT_XPU=1
[ "${1:-}" = "--no-xpu" ] && WANT_XPU=0

say() { printf '\n== %s\n' "$1"; }
die() { printf 'FATAL: %s\n' "$1" >&2; exit 1; }

say "preflight"
command -v docker >/dev/null || die "docker not on PATH"
docker info >/dev/null 2>&1 || die "docker daemon not running"
command -v git >/dev/null || die "git not on PATH"
PY310="$(command -v python3.10 || true)"
[ -n "$PY310" ] || die "python3.10 not on PATH — SetupX needs it (repo venv is 3.10; system python3 may differ)"
[ -f "$PATCH" ] || die "missing $PATCH — is this a full checkout of the harness?"
[ -f "$REPO_ROOT/.env" ] || die "missing $REPO_ROOT/.env"
grep -q '^DEEPSEEK_API_KEY=' "$REPO_ROOT/.env" || die ".env has no DEEPSEEK_API_KEY"
if [ "$WANT_XPU" = 1 ]; then
  grep -q '^OPENROUTER_API_KEY=' "$REPO_ROOT/.env" \
    || die ".env has no OPENROUTER_API_KEY (needed to embed the warm store; use --no-xpu to skip)"
fi
echo "  ok"

say "SetupX checkout at $SETUPX_ROOT"
mkdir -p "$(dirname "$SETUPX_ROOT")"
if [ ! -d "$SETUPX_ROOT/.git" ]; then
  git clone -q https://github.com/OpenDataBox/SetupX "$SETUPX_ROOT"
  echo "  cloned"
fi
git -C "$SETUPX_ROOT" fetch -q origin
git -C "$SETUPX_ROOT" checkout -q -- .          # drop any previously applied patch
git -C "$SETUPX_ROOT" checkout -q "$SETUPX_PIN"
echo "  pinned at $(git -C "$SETUPX_ROOT" rev-parse --short HEAD)"
[ -e "$SETUPX_ROOT/.env" ] && die "$SETUPX_ROOT/.env exists — SetupX loads it with override=True and would silently replace the model/endpoint/DSN the producer passes in. Remove it."
[ -e "$SETUPX_ROOT/.env.local" ] && die "$SETUPX_ROOT/.env.local exists — same problem. Remove it."
git -C "$SETUPX_ROOT" apply "$PATCH"
echo "  patch applied"

say "verifying every patched capability is present"
fail=0
for pair in "_ckpt_repo:src/environment_manager.py" \
            "SETUPX_MAX_LLM_CALLS:src/llm_engine.py" \
            "llm_usage_used:src/llm_engine.py" \
            "XPU_READONLY:src/main.py"; do
  m="${pair%%:*}"; f="${pair##*:}"
  if grep -q "$m" "$SETUPX_ROOT/$f"; then printf '  %-22s ok\n' "$m"; else printf '  %-22s MISSING (%s)\n' "$m" "$f"; fail=1; fi
done
[ "$fail" = 0 ] || die "patch did not fully apply"

say "SetupX venv (python3.10)"
[ -x "$SETUPX_ROOT/.venv/bin/python" ] || "$PY310" -m venv "$SETUPX_ROOT/.venv"
"$SETUPX_ROOT/.venv/bin/pip" install -q --upgrade pip
"$SETUPX_ROOT/.venv/bin/pip" install -q -r "$SETUPX_ROOT/requirements.txt"
"$SETUPX_ROOT/.venv/bin/python" -c "import docker,httpx,openai,psycopg2,pgvector,numpy,dotenv" \
  || die "SetupX dependencies failed to import"
echo "  $("$SETUPX_ROOT/.venv/bin/python" -V), deps import"

if [ "$WANT_XPU" = 0 ]; then
  say "done (XPU-off)"
  echo "  export SETUPX_ROOT=$SETUPX_ROOT   # leave SETUPX_DB_DSN unset to pin the arm off"
  exit 0
fi

set -a; . "$REPO_ROOT/.env"; set +a

say "pgvector ($PG_NAME on :$PG_PORT)"
docker image inspect pgvector/pgvector:pg16 >/dev/null 2>&1 || docker pull -q pgvector/pgvector:pg16
if ! docker ps -a --format '{{.Names}}' | grep -qx "$PG_NAME"; then
  docker run -d --name "$PG_NAME" -p "$PG_PORT:5432" \
    -e POSTGRES_PASSWORD="$PG_PASS" -e POSTGRES_DB=xpu_warm pgvector/pgvector:pg16 >/dev/null
  echo "  container created"
else
  docker start "$PG_NAME" >/dev/null 2>&1 || true
  echo "  container already exists"
fi
for i in $(seq 1 60); do
  docker exec "$PG_NAME" pg_isready -U postgres -d xpu_warm >/dev/null 2>&1 && break
  [ "$i" = 60 ] && die "postgres did not become ready"
  sleep 1
done
docker exec "$PG_NAME" psql -U postgres -d xpu_warm -c 'CREATE EXTENSION IF NOT EXISTS vector;' >/dev/null
echo "  ready, vector extension enabled"

say "warm store"
DSN="postgresql://postgres:$PG_PASS@localhost:$PG_PORT/xpu_warm"
have=$(docker exec "$PG_NAME" psql -U postgres -d xpu_warm -tAc \
  "select count(*) from xpu_entries" 2>/dev/null | tr -d ' ' || echo 0)
if [ "${have:-0}" -ge 600 ]; then
  echo "  already loaded ($have entries) — skipping the import"
else
  echo "  importing 600 entries (~\$0.003, a few minutes)"
  # The chat credentials are required even for an embeddings-only import: the script imports the
  # vector store, which triggers src/logger.py -> get_config(), and that validates them eagerly.
  ( cd "$SETUPX_ROOT" && \
    LLM_PROVIDER=openai \
    OPENAI_API_KEY="$DEEPSEEK_API_KEY" \
    OPENAI_BASE_URL=https://api.deepseek.com/v1 \
    OPENAI_MODEL=deepseek-v4-flash \
    EMBEDDING_API_KEY="$OPENROUTER_API_KEY" \
    EMBEDDING_BASE_URL=https://openrouter.ai/api/v1 \
    EMBEDDING_MODEL=openai/text-embedding-3-small \
    EMBEDDING_DIM=1536 \
    dns="$DSN" \
    ./.venv/bin/python scripts/import_xpu_jsonl.py data/xpu_warm.jsonl --clear )
  have=$(docker exec "$PG_NAME" psql -U postgres -d xpu_warm -tAc "select count(*) from xpu_entries" | tr -d ' ')
fi
dims=$(docker exec "$PG_NAME" psql -U postgres -d xpu_warm -tAc "select vector_dims(embedding) from xpu_entries limit 1" | tr -d ' ')
echo "  master xpu_warm: $have rows, ${dims}-dim"
[ "$have" = "600" ] || die "expected 600 entries, got $have"

say "per-run copy"
docker exec "$PG_NAME" psql -U postgres -d postgres -c 'DROP DATABASE IF EXISTS xpu_run;' >/dev/null 2>&1
docker exec "$PG_NAME" psql -U postgres -d postgres -c 'CREATE DATABASE xpu_run TEMPLATE xpu_warm;' >/dev/null
echo "  xpu_run: $(docker exec "$PG_NAME" psql -U postgres -d xpu_run -tAc 'select count(*) from xpu_entries' | tr -d ' ') rows"

say "ready"
cat <<EOF
  export SETUPX_ROOT=$SETUPX_ROOT
  export SETUPX_DB_DSN=postgresql://postgres:$PG_PASS@localhost:$PG_PORT/xpu_run
  export EMBEDDING_API_KEY="\$OPENROUTER_API_KEY"
  export EMBEDDING_BASE_URL=https://openrouter.ai/api/v1
  export EMBEDDING_MODEL=openai/text-embedding-3-small
  export EMBEDDING_DIM=1536

  Re-take the xpu_run copy before each run so nothing carries across:
    docker exec $PG_NAME psql -U postgres -d postgres -c 'DROP DATABASE IF EXISTS xpu_run;'
    docker exec $PG_NAME psql -U postgres -d postgres -c 'CREATE DATABASE xpu_run TEMPLATE xpu_warm;'

  Then: ./run_bench.sh setupx --repos-json datasets/rat_python50.json --tier all --concurrency 4
  Check the first _meta.json for setupx_patched: true before trusting the run.
EOF
