# env-bench

A self-contained **environment-construction benchmark**. An **agent** reads a repository and
produces a Dockerfile (+ setup script); an agent-free **measure** stage rebuilds that Dockerfile in a
fresh container, runs pytest, and scores reproducible **EBSR / ESSR**. The produced Dockerfile is the
*only* interface between the two halves.

Everything needed to run is vendored here — the runner, the RAT eval framework, the agent(s), and the
datasets. You need only Docker, Python 3.10+, and an API key.

## Quickstart

```bash
git clone <this-repo> env-bench && cd env-bench
./setup.sh                       # creates .venv, installs deps (needs a running Docker daemon)
cp .env.example .env             # add your OPENROUTER_API_KEY
./run_bench.sh john-planner-v3 --only pallets/itsdangerous   # smoke: one repo
```

A full run:
```bash
./run_bench.sh john-planner-v3 --tier all --concurrency 4
```

Output lands in `runs/<variety>/<run>-<timestamp>/`:
- `output/<owner>/<repo>/eval_build/Dockerfile` + `_meta.json` + `_result_row.json` — the produced env.
- `measure/metrics.json` — reproducible re-measured **EBSR / ESSR / ESSR_all** + collection diagnostics
  + economy (written automatically after a successful run).

## Architecture

The benchmark is a **produce → measure** pipeline. The two halves share nothing but a Dockerfile:

- **Produce** (the agent; non-deterministic, needs an API key): given a repo, emit
  `eval_build/Dockerfile` + `_meta.json`. That's the agent's *entire* job.
- **Measure** (`bench/`; agent-free, deterministic): rebuild that Dockerfile in a **fresh** container,
  run `pytest` at `/testbed`, and score. Because measure never runs the agent, the score is reproducible.

### Directory layout

```
runner/        orchestration. cli.py = the `bench` command (resolve variety, provision, measure);
               benchmark.py = the per-repo produce loop / resume / aggregate; registry|provision|manifest.
               runner/live/ = the live baselines with no rebuildable artifact (rat, sweagent, claudecode).
producers/     the PRODUCE contract. base.py = Producer protocol + ProducedEnv + write_env_packet;
               one <name>.py per method (dockeragent, repo2run, claudecode_dockerfile, + the live gates).
bench/         the MEASURE authority. rebuild + pytest + score: metrics.py, inline_score.py, report/.
agents/        per-agent CHECKOUTS. each ships its own multi_docker_eval_adapter.py (see below).
rat/           the vendored RunAnyThing framework (libkit + eval). imported by producers/{rat,repo2run}
               and runner/live/* ONLY — never by bench/.
datasets/      repo lists for --repos-json.
varieties.toml the method registry (top-level).
runs/          run outputs, per variety + timestamp.
tools/         operator scripts (optional).
```

### Dispatch (one produce-able variety)

```
varieties.toml [variety.X]  (model=dockeragent, branch=X)
  └─ runner/cli.py        resolve variety → provision /opt/agents/X → set DOCKERAGENT_ROOT=that checkout
        └─ runner/benchmark.py::_make_model("dockeragent") → _ProducerModel
              └─ producers.get("dockeragent").produce(repo, ctx)   ← YOUR agent runs, returns a Dockerfile
                    └─ write_env_packet → output/<owner>/<repo>/eval_build/Dockerfile + _meta.json
                          └─ bench (unified_bench --harvest): fresh rebuild + pytest at /testbed + score
                                └─ measure/metrics.json  (EBSR / ESSR / ESSR_all + economy)
```

`_make_model` routes **produce-able** methods (`dockeragent`, `repo2run`, `claudecode-dockerfile`,
`executionagent`) to a
`_ProducerModel` that drives the producer registry directly, and **native-lane** methods (`rat`,
`sweagent`, `claudecode`) to the live models under `runner/live/`.

### Method lanes (the `measure` tag)

- **conforming** — the agent's Dockerfile already homes the repo at `/testbed` (e.g. `dockeragent`).
- **rehome** — the Dockerfile homes the repo elsewhere (`/repo`, `/app/<project>`); the producer moves
  it to `/testbed` (`repo2run`, `executionagent`).
- **none** — a live in-place agent with no rebuildable artifact (`rat`, `sweagent`, `claudecode`). It is
  **gated out** of the fresh-container harvest (so it never emits a shadowing EBSR-0) and scored inline;
  the inline numbers land in `live_scores.json`.

## What `run_bench.sh` does

1. Points `RUNNER_ROOT / REPO_ROOT / RAT_ROOT / AGENTS_ROOT / BENCH_ROOT / RUNS_ROOT` at this repo and
   puts the repo root on `PYTHONPATH` so the `runner` package imports.
2. Sets `SKIP_PROVISION=1` (agents are vendored snapshots, not live git checkouts).
3. Loads `.env`, then runs `python -m runner.cli` through `.venv/bin/python`.
4. `runner.cli` produces the Dockerfile (via the agent) → runs the measure stage (fresh rebuild + score).

The `bench` command is `python -m runner.cli` (there is a `bench/` *directory* — the evaluator package —
so there is no top-level `bench` file). `run_bench.sh` / `env.sh` set the env for you; the raw form is
`python -m runner.cli <variety> [--only owner/repo] [--tier all] [--limit N] [--concurrency N] [--llm SLUG]`.

## Varieties (the methods)

Defined in `varieties.toml`:

| variety | model | what it is |
|---|---|---|
| `john-planner-v3`, `john-v3-multi-lang`, `radical`, `john-planner-v1`, `Jayint-Planer` | `dockeragent` | environment-construction agents, one per checkout under `agents/` (same model, different adapter) |
| `rat` | `rat` | RunAnyThing baseline (live, no Dockerfile) |
| `repo2run` | `repo2run` | Repo2Run baseline (re-homed to `/testbed`) |
| `sweagent` | `sweagent` | SWE-agent baseline (needs the extra deps in `requirements.txt`) |
| `claudecode` / `claudecode-dockerfile` | `claudecode` / `claudecode-dockerfile` | Claude Code, live in-place / Dockerfile-emitting |
| `executionagent` | `executionagent` | ExecutionAgent baseline (Dockerfile + `commands.sh` folded into one image, re-homed to `/testbed`) |
| `sweagent_repo2run` | `sweagent_repo2run` | SWE-agent under the Repo2Run paper's baseline settings — emits a Dockerfile, re-homed to `/testbed` |
| `setupx` | `setupx` | SetupX baseline (XPU store + speculative execution + prosecutor/judge; trajectory replayed and re-homed to `/testbed`) |

Run any with `./run_bench.sh <variety> …`. Override the LLM with `--llm <slug>`.

### SWE-agent setup

`sweagent` is **not** installed by `setup.sh` and is not on PyPI in a usable form (the `sweagent`
project there has a single 0.0.1 stub release). It needs its own Python 3.11+ venv, because its
`requires-python` is `>=3.11` while the runner venv is 3.10:

It must be an **editable install from a clone** — a wheel install cannot work.
`sweagent/__init__.py` asserts that `config/`, `tools/` and `trajectories/` exist as siblings of
the package directory, and pip does not ship them into `site-packages`, so `import sweagent` fails
outright. `tools/` is also where the tool bundles live (`tools/registry`, `tools/windowed`, ...),
so the configs could not resolve either.

```bash
git clone https://github.com/SWE-agent/SWE-agent.git /opt/swe-agent
mkdir -p /opt/swe-agent/trajectories          # asserted at import; not tracked in git
python3.11 -m venv /opt/sweagent_venv
/opt/sweagent_venv/bin/pip install -e /opt/swe-agent
export SWEAGENT_VENV_PY=/opt/sweagent_venv/bin/python

# validate the ported config against the installed package — no docker, no key, no cost:
$SWEAGENT_VENV_PY tools/check_sweagent_repo2run_config.py
```

Pin the SWE-agent commit (`git -C /opt/swe-agent rev-parse HEAD`) and record it with the run if you
are comparing against published numbers — the config schema moves between versions.

`runner/live/sweagent.py` subprocesses `runner/live/sweagent_runner.py` under that interpreter; the
runner venv itself never imports `sweagent`. Without the venv every repo returns
`failure_reason="sweagent_venv_missing"`. The lane is `measure="none"` — no Dockerfile artifact, so
it is gated out of the fresh-container harvest and scored inline into `live_scores.json`.

The same venv serves **`sweagent_repo2run`**, a second arm that runs SWE-agent under the
Repo2Run paper's baseline settings (arXiv:2502.13681 appendix I.2) instead of the in-place prompt:
the agent is asked for a Dockerfile at `/Dockerfile` with the repo at `/repo`, so it produces a
rebuildable artifact and gets a real EBSR/ESSR row alongside `repo2run` and `dockeragent`. Run both
— the delta between them is how much of the score is the agent versus the scaffolding.

```bash
./run_bench.sh sweagent_repo2run --repos-json datasets/rat_python50.json --tier all
```

Three things to know about that arm:

- **The paper's config could not be used verbatim.** Three of its six tool bundles
  (`tools/defaults`, `tools/edit_linting`, `tools/env_setup`) no longer exist in SWE-agent; the
  substitutions are listed at the top of `producers/sweagent_repo2run_config.yaml`. Its
  `parse_function: thought_action` and `last_n_observations` history processor port unchanged.
  Pin the SWE-agent git revision if you are reproducing published numbers.
- **The paper's success criterion is `pytest --collect-only -q`** — your EBSR gate, not ESSR.
  Expect a high-EBSR / low-ESSR profile by construction. That matches Repo2Run's own target, which
  is the point of the comparison, but it is not evidence the agent builds good environments.
- **Budget.** `rat/eval/sweagent/python-config.yaml` caps the in-place arm at
  `per_instance_call_limit: 15` and `sweagent_wrapper.py` overrides only the *cost* limit, so
  `--num-turn` never reaches it — while `dockeragent` gets 30 turns and `repo2run`/`executionagent`
  get 40. This arm sets the call limit from `--num-turn` (default 40) so it is comparable; the
  in-place arm's 15 is untouched and remains a confound if you compare the two directly.
- **Python only.** The paper's template hardcodes `FROM python:3.10` and `pip install pytest`.

### ExecutionAgent setup

`executionagent` is the only variety that is not vendored here — point it at a checkout:

```bash
git clone https://github.com/sola-st/ExecutionAgent /opt/agents/ExecutionAgent
python -m venv /opt/agents/ExecutionAgent/venv
/opt/agents/ExecutionAgent/venv/bin/pip install -e /opt/agents/ExecutionAgent
export EXECUTIONAGENT_ROOT=/opt/agents/ExecutionAgent
# optional; defaults to $EXECUTIONAGENT_ROOT/venv/bin/python, then the runner's own interpreter
export EXECUTIONAGENT_PYTHON=/opt/agents/ExecutionAgent/venv/bin/python

./run_bench.sh executionagent --repos-json datasets/rat_python50.json --tier all --concurrency 4
```

It goes through litellm, so the variety pins an `openrouter/…` slug and reads `OPENROUTER_API_KEY`
(same route as `sweagent`). **Note what is being measured:** EA's own prompt tells it to keep build
and test steps *out* of the Dockerfile — it installs dependencies live in the container and dumps
the transcript to `success_artifacts/commands.sh`. Rebuilding its Dockerfile alone measures a repo
with no dependencies (a guaranteed EBSR-0), so `producers/executionagent.py` replays `commands.sh`
as one build layer before re-homing `/app/<project>` to `/testbed` — the same env EA's own
`launch.sh` reaches. That makes the row `conformance="synthesized"`, not `native`. A budget-exhausted
run falls back to `forced_exit_cycle/Dockerfile` and is noted as such in `_meta.json`.

### SetupX setup

Like `executionagent`, `setupx` is not vendored — point it at a checkout. The vendored patch
`tools/setupx-bench.patch` is **required**: it namespaces the checkpoint images per run, makes the
container network mode configurable, and adds the `SETUPX_MAX_LLM_CALLS` budget and the
`XPU_READONLY` switch. The venv must be **Python 3.10** (3.10.11 here).

```bash
mkdir -p ~/agents
git clone https://github.com/OpenDataBox/SetupX ~/agents/SetupX
git -C ~/agents/SetupX checkout 85de35515c45954b72afb9678dfd172855bfb847
git -C ~/agents/SetupX apply "$PWD/tools/setupx-bench.patch"
python3.10 -m venv ~/agents/SetupX/.venv
~/agents/SetupX/.venv/bin/pip install -r ~/agents/SetupX/requirements.txt
export SETUPX_ROOT=~/agents/SetupX
# optional; defaults to $SETUPX_ROOT/.venv/bin/python
export SETUPX_PYTHON=~/agents/SetupX/.venv/bin/python

# the producer re-checks these at runtime, but check them here too — an unpatched checkout
# fails silently and expensively:
grep -q "_ckpt_repo" ~/agents/SetupX/src/environment_manager.py
grep -q "SETUPX_MAX_LLM_CALLS" ~/agents/SetupX/src/llm_engine.py
```

**Never create a `.env` inside the checkout.** `src/config.py` calls `load_dotenv(override=True)` at
import, which would silently override the model, endpoint and DSN the producer passes in. The
producer refuses to start if one exists.

The arm routes through **DeepSeek's own API**, not OpenRouter — SetupX speaks raw OpenAI-compatible
HTTP rather than litellm — so it reads `DEEPSEEK_API_KEY`. A first run needs nothing else: with
`SETUPX_DB_DSN` unset the producer pins the arm XPU-off (`XPU_ENABLED=0`, `XPU_VECTOR_ENABLED=0`,
`XPU_READONLY=0`) and skips embeddings entirely, so `DEEPSEEK_API_KEY` plus docker is enough to
exercise the whole produce → replay → rebuild → measure path:

```bash
export SETUPX_ROOT=~/agents/SetupX
./run_bench.sh setupx --repos-json datasets/rat_python50.json --only bruin-data/ingestr --tier all
```

`bruin-data/ingestr` is the lightest repo in `rat_python50.json` (19 tests), so it is the cheapest
end-to-end signal. The smoke run scored `EBSR 1.0` / `ESSR 1.0`, 19 tests collected, 0 collect
errors; `_meta.json` showed `status=produced`, `conformance=synthesized`, `unreplayed=false`,
`llm_calls=101`, `turns_used=25`, `produce_s=3198.77` (~53 min).

**Note what is being measured:** SetupX emits no Dockerfile — it mutates a live container and
writes a JSON report. `producers/setupx.py` replays the surviving `setup.history` (SetupX rolls
back to `docker commit` checkpoints, so undone work is dropped rather than replayed) onto a clone
pinned at the dataset SHA, then re-homes `/workspace/repo` to `/testbed`. That makes the row
`conformance="synthesized"`, not `native` — the same status `executionagent` carries.

**Two fidelity limits, both surfaced in `_meta.json`:**

- Every phase-2 agent runs commands against the same live container — `VerifierAgent`,
  `ProsecutorAgent` and `JudgeAgent`. None of it lands in `history`, so anything they install or
  write is in the container the agent finished with but is not replayed.
- A successful XPU trial that fell back to atom-rendered `suggestion.commands` records no command in
  the trajectory. Those runs are flagged `unreplayed=true`.

**Commit pinning.** SetupX clones `--depth=1` at the live default-branch HEAD and has no notion of a
dataset SHA. Rather than patch the checkout, the producer builds a per-repo base image holding the
repo pinned at `/mirror` and hands SetupX `/mirror` as the repo URL, so its own clone lands the
pinned tree. `git` warns `--depth is ignored in local clones`; that is expected.

**Token accounting.** Stock `src/llm_engine.py` discards the API `usage` block; the vendored patch
accumulates it there and `src/main.py` reports it, so `_meta.json` carries `tokens_in` /
`tokens_out` / `total_tokens` alongside `llm_calls`, `turns_used` and `produce_s`. `tokens_in` is
**total** prompt tokens, DeepSeek cache hits included, so pricing from it alone overstates — the
hit/miss split for correct pricing is `usage.prompt_cache_hit_tokens` /
`prompt_cache_miss_tokens` in the per-repo `*_result.json`, which the producer preserves.
`cost_usd` stays `null` by design: DeepSeek prices hits, misses and output separately across
peak/off-peak windows, and that computation belongs with the metering ledger. On an unpatched
checkout there is no `usage` block and all three token fields are `null`; a patched run whose
provider omits `usage` records `0` instead, so `llm_calls > 0` with `total_tokens == 0` is the
signal that the counts cannot be trusted.

**`num_turn` is a budget in LLM completions, not agent steps** — enforced by the vendored patch at
`LLMClientBase.chat`. The smoke run's 25 agent steps cost 101 completions, roughly 4:1, so a
step-based budget at the same nominal number would have spent about four times what
`sweagent_repo2run` spends.

**Cleanup is mandatory, not advisory.** A single `initial_clone` checkpoint measured **2.07 GB**,
and SetupX commits another before every XPU trial. `_sweep_checkpoints` clears them on both the
normal and crashed exits (verified: zero left after the smoke run), but a `SIGKILL` bypasses it —
and the per-repo `setupx-mirror:*` images (2.01 GB each) are never swept at all. After any run:

```bash
docker images --filter 'reference=setup_agent_checkpoint*' -q | xargs -r docker rmi -f
docker images --filter 'reference=setupx-mirror*' -q | xargs -r docker rmi -f
```

**Concurrency is safe only with the patch applied** — it namespaces the checkpoint images per run
(e.g. `setup_agent_checkpoint_bruin-data__ingestr_54709:initial_clone`) and makes the network mode
configurable. Without it, run at `--concurrency 1`.

**A run prints nothing until it exits.** `run_setupx` captures the child's output rather than
streaming it, so the 53-minute smoke run was silent throughout. That is not a hang.

**The XPU store — optional, only for the XPU-on arm.** pgvector, verified working:

```bash
docker pull pgvector/pgvector:pg16          # ~2 min; pull before the run below
docker run -d --name setupx-xpu -p 5433:5432 \
  -e POSTGRES_PASSWORD=setupx -e POSTGRES_DB=xpu_warm pgvector/pgvector:pg16
sleep 5 && docker exec setupx-xpu psql -U postgres -d xpu_warm \
  -c 'CREATE EXTENSION IF NOT EXISTS vector;'
```

The import is now verified — 600 entries loaded, 0 failed:

```bash
# Import the 600 shipped entries. The JSONL carries no vectors — the importer embeds every entry,
# so this costs one embeddings call per entry and must use the same model the runs query with.
#
# scripts/import_xpu_jsonl.py imports the vector store, which triggers src/logger.py's
# LoggerSetup.setup() -> get_config(), and get_config() validates the *whole* config eagerly —
# chat credentials included — even though the import only ever calls the embeddings endpoint. The
# OPENAI_* vars below are therefore required for this embeddings-only import; they are not a
# copy-paste mistake. They mirror what producers/setupx.py::child_env sets at runtime, and no chat
# call is made during the import.
cd ~/agents/SetupX
LLM_PROVIDER=openai \
  OPENAI_API_KEY="$DEEPSEEK_API_KEY" \
  OPENAI_BASE_URL=https://api.deepseek.com/v1 \
  OPENAI_MODEL=deepseek-v4-flash \
  EMBEDDING_API_KEY="$OPENROUTER_API_KEY" \
  EMBEDDING_BASE_URL=https://openrouter.ai/api/v1 \
  EMBEDDING_MODEL=openai/text-embedding-3-small \
  EMBEDDING_DIM=1536 \
  dns=postgresql://postgres:setupx@localhost:5433/xpu_warm \
  .venv/bin/python scripts/import_xpu_jsonl.py data/xpu_warm.jsonl --clear
```

The OpenRouter embeddings endpoint returns HTTP 200 with 1536 dimensions, matching `EMBEDDING_DIM`,
priced at $0.02/1M — the whole import cost about $0.003. Confirmed in the database: 600 rows,
`vector_dims=1536`, 593 rows carrying non-empty telemetry (the ranking is telemetry-weighted, so
that matters).

`xpu_warm` is then the immutable master; nothing ever runs against it again. Take a fresh copy
**before each benchmark run**:

```bash
docker exec setupx-xpu psql -U postgres -d postgres -c 'DROP DATABASE IF EXISTS xpu_run;'
docker exec setupx-xpu psql -U postgres -d postgres \
  -c 'CREATE DATABASE xpu_run TEMPLATE xpu_warm;'
export SETUPX_DB_DSN=postgresql://postgres:setupx@localhost:5433/xpu_run
```

`CREATE DATABASE … TEMPLATE` is a file-level copy: instant, no re-embedding, and every run starts
from the same 600 entries — verified: the copy landed 600 rows, and deleting 5 rows from `xpu_run`
left `xpu_warm` at 600. A SELECT-only role is **not** a workable alternative —
`XpuVectorStore.__init__` runs CREATE EXTENSION/TABLE/INDEX DDL on every connect, unguarded, so a
role without those rights kills every repo before Stage 1. Setting `SETUPX_DB_DSN` without
`EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` is refused by the producer: every
retrieval would 404 and be swallowed, leaving an arm that reports as XPU-on while retrieving
nothing. What the template copy does *not* isolate is writes *within* one run — `XPU_READONLY=1`
(from the patch) handles that by skipping `_store_xpu_experience`, and `FREEZE_TELEMETRY=1` freezes
the telemetry counters.

**Still untested: the XPU-on arm itself.** A run against the loaded store has been launched, but
its result is not yet known.

## Datasets

`datasets/` holds the repo lists (`--repos-json`):
- `rat_python50.json` — **the authoritative Python set** (the default): 50 repos, each pinned to a
  `commit` SHA. Every clone path checks out that commit (see **Commit pinning** below), so runs are
  reproducible instead of drifting with each repo's live HEAD.
- `rat_node50.json` — the 50-repo Node set.

Pick one with `--repos-json datasets/<file>`. Filter/limit with `--limit N`, `--only owner/repo`,
`--tier all|smoke|extended`.

### Commit pinning

Each row's `commit` field is **enforced** at every clone site — `git clone --depth=1`, then
`git fetch --depth 1 origin <sha> && git checkout --detach <sha>`. This covers the framework clones
(`repo2run`, and the shared `download_repo` used by `rat` / `claudecode` / `claudecode-dockerfile`) AND
the build-time `git clone … /testbed` inside a `dockeragent`'s emitted Dockerfile — `producers/dockeragent.py`
injects the checkout into that Dockerfile, so the **measured** build is pinned for *every* dockeragent
branch without touching agent code. A row with no `commit` clones the live HEAD (unchanged behavior).

## Scoring

- **Inline** (per run): `_result_row.json` per repo; recompute with
  `python -m bench.inline_score <variety>=runs/<variety>/<run>`.
- **Reproducible** (`measure/metrics.json`): rebuilds each Dockerfile clean and re-runs pytest.
  `EBSR` = Repo2Run collect-only gate; `ESSR`/`ESSR_all` = RAT pass-rate ÷exec / ÷all; plus collection
  and token/image economy. Re-run by hand:
  ```bash
  source env.sh
  python -m bench.unified_bench --harvest john-planner-v3=runs/john-planner-v3/<run>/output \
                                --out runs/john-planner-v3/<run>/measure
  ```

### claudecode-dockerfile artifacts

Per repo, under `runs/<variety>/<run>/output/<owner>/<repo>/`:

| File | Contents |
| --- | --- |
| `claude_stream.jsonl` | Raw Claude Code `stream-json` events — the agent's full trajectory (one `tool_use` per action, `tool_result` per observation). The ccdf analogue of dockeragent's `react_trace.jsonl`. |
| `claude_actions.log` | Readable action log rendered from the stream. |
| `claude_stderr.txt` | CLI stderr (only when non-empty). |
| `_meta.json` | `cost_usd` (Claude Code's own `total_cost_usd`), `tokens_in`/`tokens_out`/`total_tokens`, `llm_calls`, `turns_used`. |

`measure/metrics.json` then carries `total_cost_usd`, `mean_cost_usd`, `cost_per_ebsr`,
`cost_per_real_success` and `n_cost_reporting` alongside the token/turn economy. Producers whose
model API reports no cost stay `None` and are excluded from those denominators — never counted as free.

Cost is taken from Claude Code's reported `total_cost_usd`, **not** derived from tokens and a rate
card: Claude Code mixes models within a single run (a haiku for side tasks alongside the primary
model), so one rate would mis-price it. `tokens_in` includes cache-creation and cache-read tokens
(matching `ccdf_costs.py`), which makes it **not** directly comparable to the raw prompt-token counts
the deepseek-backed agents report — the component split is preserved in `claude_stream.jsonl`.
`setupx` is the exception among those: its `tokens_in` is cache-inclusive too, with its own split
kept in the per-repo `*_result.json` (see the SetupX section above).

The `claudecode*` lanes need the local-only `claude-runner:latest` workbench image (no registry, so a
`docker system prune` deletes it). `bench` builds it automatically from `docker/claude-runner.Dockerfile`
when missing and aborts the run if that build fails — without the preflight every repo would record
`status="error"`, which on disk is indistinguishable from a completed run that scored zero.

## Adding your own agent

> ⚠️ **Honor the commit pin in your adapter — read this first.** The authoritative dataset pins every
> repo to a `commit` SHA, passed to your adapter as `instance["commit"]`. The framework already injects
> a checkout of that commit into the `/testbed` clone of a dockeragent's emitted Dockerfile, so the
> **measured** build is pinned for you. But your adapter's OWN clone — the repo your agent reads to
> decide the setup — must ALSO honor `instance["commit"]`: after cloning, run
> `git fetch --depth 1 origin <commit> && git checkout --detach <commit>`. Otherwise your agent
> *analyzes* the live HEAD while the *build* runs the pinned commit, and the setup it generates may not
> match. See `agents/john-planner-v3/multi_docker_eval_adapter.py::_clone` for the one-liner.

For the common case you touch **two things** and change **no framework code**: your agent ships a
`multi_docker_eval_adapter.py`, and you register a variety. Every `dockeragent`-family agent uses the
same `model = "dockeragent"` but its own adapter in its own checkout — the runner loads *your* adapter
from *your* checkout by path.

### 1. The adapter — `multi_docker_eval_adapter.py` (the only required file)

Put it at the **root of your agent's checkout** (`agents/<name>/`). This is the entire contract:

```python
class MultiDockerEvalAdapter:
    def __init__(self, output_dir: str = "./out"):
        self.output_dir = output_dir            # runner passes the per-repo output/<name>/ dir

    def process_single_instance(
        self, instance: dict, base_image="auto", model=None, max_steps=30,
        enable_artifact_preflight=False, **_ignored,
    ) -> dict:
        # instance = {"instance_id": "owner__repo", "repo_url": "https://github.com/owner/repo",
        #             "language": "python", "commit": "<sha>|None"};  model = the llm slug
        iid = instance["instance_id"]
        result = {"dockerfile": None, "setup_scripts": {}, "base_image": None, "logs": {}}
        try:
            # --- YOUR AGENT: clone repo_url, THEN pin: if instance["commit"], run
            #     `git fetch --depth 1 origin <commit> && git checkout --detach <commit>`
            #     so you analyze the pinned commit; then decide base image + install steps ---
            result["dockerfile"] = (
                "FROM python:3.13-slim\nWORKDIR /testbed\n"
                "RUN apt-get update && apt-get install -y --no-install-recommends git\n"
                f"RUN git clone --depth=1 {instance['repo_url']} /testbed\n"
                "RUN pip install -e . && pip install --no-cache-dir pytest\n")
            result["base_image"] = "python:3.13-slim"
            result["logs"] = {"head_sha": "...", "tokens_in": 0, "tokens_out": 0}   # optional economy
        except Exception as e:
            result["logs"] = {"error": repr(e)}     # NEVER raise — None dockerfile = clean no_dockerfile
        return {iid: result}                        # {id: result} or a bare result both accepted
```

Rules the contract enforces:
- **Never raise.** On failure, leave `dockerfile = None` and set `logs["error"]` (a raise becomes a
  silent `status=error` scored EBSR-0).
- **The Dockerfile must clone the repo to `/testbed`** and set up a working env so `pytest` runs there —
  `/testbed` is the fixed mount point the measure stage runs its test tools in.
- **Ensure pytest is installed** (the producer appends `RUN pip install pytest` if missing, but do it).
- **Optional economy:** `tokens_in`/`tokens_out`/`llm_calls`/`turns_used` in `logs` are harvested into
  `_meta.json`. `setup_scripts` is `{name: content}` for any files your Dockerfile `COPY`s.

Your actual agent code lives on the same checkout; the runner puts the checkout root on `sys.path`, so
your adapter's imports resolve. See `agents/john-planner-v3/multi_docker_eval_adapter.py` for a real one.

### 2. Register it in `varieties.toml`

```toml
[variety.my-agent]
branch  = "my-agent"                     # your branch name; must be NON-EMPTY (empty => a no-agent
                                         #   baseline). Used only for provisioning + provenance; for a
                                         #   local clone into agents/my-agent it can be any non-empty label.
model   = "dockeragent"                  # routes to _ProducerModel → your adapter
venv    = "/opt/rat_venv"                # (optional) venv your agent runs under
llm     = "deepseek/deepseek-v4-flash"   # forwarded to your adapter as `model=`
measure = "conforming"                   # your Dockerfile homes the repo at /testbed
```

The variety **name** is also the run bucket (`runs/my-agent`) and the checkout dir
(`agents/my-agent` / `/opt/agents/my-agent`) — keep it equal to your branch.

### 3. Get your checkout into `agents/` — for local runs, just `git clone` it

**The quickest path (local): `git clone` your agent branch straight into `agents/<variety>/` and run — no
push, no provisioning step.** `run_bench.sh` exports `SKIP_PROVISION=1`, so the runner uses the checkout
as-is (it never fetches or resets) and loads your adapter from `agents/<variety>/multi_docker_eval_adapter.py`:

```bash
git clone --branch my-agent <your-repo-url> agents/my-agent   # root must hold multi_docker_eval_adapter.py
./run_bench.sh my-agent --only owner/repo
```

That's the whole setup — verified end to end: the runner puts `agents/my-agent/` on `sys.path` (so your
adapter's own imports resolve), loads your `MultiDockerEvalAdapter`, and measures the Dockerfile it emits.
Two requirements: the clone's **folder name must equal the variety name** (the runner looks for exactly
`agents/<variety>/`), and the `[variety.my-agent]` block must have a **non-empty `branch`** (an empty
branch marks a no-agent baseline, which is routed elsewhere — the value itself is only used for
provisioning + provenance under `SKIP_PROVISION`).

**The provisioning path (e.g. the shared VM), for when you push instead of vendoring:** without
`SKIP_PROVISION`, `provision_agent` runs `git fetch origin <branch> && git reset --hard FETCH_HEAD` in the
checkout (never `git clean` — it preserves your `workplace/`), so the checkout must already be a git repo
with that branch pushed to `origin`. Use it to keep a deployed checkout in sync with your branch.

### Escape hatch — a first-class producer

If your agent doesn't fit the `MultiDockerEvalAdapter` shape, write a producer instead:
1. `producers/my_producer.py` implementing the `Producer` protocol (`name`, `needs_llm`,
   `measurable=True`, `produce(repo, ctx) -> ProducedEnv`).
2. Register it in `producers/__init__.py` (`register(MyProducer)`).
3. `model = "my_producer"` in `varieties.toml`.
4. Add `"my_producer"` to `_PRODUCE_ABLE` in `runner/benchmark.py` (that set is currently hardcoded, not
   derived from the registry's `measurable` flag).

## Notes / gotchas

- **Docker is required and does real work** — the measure stage builds one image per repo. Run on a box
  with a working daemon and a few GB free.
- **Launch via `./run_bench.sh`** (or `source env.sh` first, then `python -m runner.cli …`). Running the
  raw module without those env vars / `python-dotenv` (which lives in `.venv`) will fail to find keys.
- **Two runs of the same repo collide** in the agent's repo-keyed `workplace/` — run distinct repos
  concurrently, not the same repo twice at once.
- **`sentence-transformers` is heavy** (pulls torch). If your agent doesn't use embeddings you can drop
  it from `requirements.txt` for a lighter install.
- **Never commit `.env`** — it's gitignored; share keys out of band.
- The RAT framework under `rat/` is vendored from "RunAnyThing"; check its upstream terms before
  redistributing further.
