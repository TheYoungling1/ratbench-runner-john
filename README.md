# env-bench

A benchmark for **environment-construction agents**: given a GitHub repo, can an agent write a
Dockerfile that builds and makes the repo's test suite run?

Every method (a "variety") is scored the same way, in two separate steps:

1. **Produce** — the agent reads the repo and writes `Dockerfile` (+ any setup scripts). This is the
   only step that uses an LLM, so it costs money and is not deterministic.
2. **Measure** — no agent involved. The Dockerfile is rebuilt in a **fresh** container, `pytest` runs at
   `/testbed`, and the run is scored:
   - **EBSR** — did the environment build and pytest collect tests? (Repo2Run's success gate)
   - **ESSR** — what fraction of the tests pass?
   - plus cost, tokens, turns, time and image size.

The Dockerfile is the *only* thing passed between the two steps, so a score can always be
reproduced by re-running measure on the saved Dockerfile.

## What's in this repo

| Folder / file | What it does |
|---|---|
| `runner/` | **Orchestration** — the `bench` command. `cli.py` looks up the variety in `varieties.toml`, runs produce, then measure. `benchmark.py` loops over repos, handles resume and aggregation. |
| `producers/` | **The produce step.** One file per method (`setupx.py`, `repo2run.py`, `pipreqs_baseline.py`, `sweagent_repo2run.py`, `claudecode_dockerfile.py`, …). Each writes `eval_build/Dockerfile` + `_meta.json`. |
| `bench/` | **The measure step.** Rebuild, run pytest, score (`measure.py`, `harvest.py`, `metrics.py`). Never calls an LLM. |
| `rat/` | A vendored copy of the RunAnyThing framework, used by the `rat` / `repo2run` producers. |
| `docker/` | Workbench images for the Claude Code arm. Built automatically when needed. |
| `datasets/` | Repo lists (`rat_python50`, `rat_python50_new`, `rat_python100`, `envbench_python100`) and the gold test counts (`gold_manifests_combined.json`). |
| `tools/` | Operator scripts: SetupX provisioning + patch, Dockerfile-quality report, run audit. |
| `varieties.toml` | The list of methods: which producer, which LLM, how to score it. |
| `run_bench.sh` | Entry point. Sets paths, loads `.env`, runs `python -m runner.cli`. |
| `setup.sh` / `env.sh` | One-time install / environment for an interactive shell. |
| `runs/` | Output of your own runs (created on first run, gitignored). |
| `runs_results/` | The final scores for every baseline. See [below](#benchmark-results-runs_results). |

## Setup

The steps are the same on a laptop and on a Linux VM.

### 1. Prerequisites

- **Docker**, with the daemon running. Measure builds one image per repo, so leave tens of GB of
  disk free.
- **Python 3.10+** and `git`.

On a fresh Ubuntu VM:

```bash
curl -fsSL https://get.docker.com | sh          # installs and starts Docker
sudo apt-get install -y git python3-venv tmux
```

### 2. Install

```bash
git clone <this-repo> env-bench && cd env-bench
./setup.sh                  # creates .venv and installs requirements.txt
cp .env.example .env        # then set LLM_API_KEY
```

### API keys

Every arm uses **one key and one OpenAI-compatible endpoint**:

| In `.env` | What it is |
|---|---|
| `LLM_API_KEY` | Your key. The only value you must fill in. |
| `LLM_BASE_URL` | The endpoint. Defaults to OpenRouter (`https://openrouter.ai/api/v1`). |
| `LLM_MODEL` | Optional: one model for every arm, as that provider names it. Empty = each arm's pinned model in `varieties.toml`. `--llm` overrides it. |

On OpenRouter every arm is pinned to the same upstream provider (DeepInfra) with reasoning off,
which is how `runs_results/` was produced. Those switches are OpenRouter-only: on another provider
the arms run with that provider's defaults, so numbers are not directly comparable. SWE-agent also
cannot price an unknown model there, so its cost is not tracked (the 100-call cap still applies).

The older per-provider variables (`OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, `SETUPX_BASE_URL`,
`EMBEDDING_*`, `ANTHROPIC_AUTH_TOKEN`) still work and take over when `LLM_API_KEY` and
`LLM_BASE_URL` are both unset.

### 3. Smoke test (one repo, free)

```bash
./run_bench.sh pipreqs --tier all --only bruin-data/ingestr
```

`pipreqs` uses no LLM, so this checks Docker, the install and the measure step at no cost.

### 4. A real run

```bash
./run_bench.sh <variety> --repos-json datasets/rat_python50.json --tier all --concurrency 4
```

> **Always pass `--tier all`.** The default is `smoke`, and no repo in the datasets has a tier
> label, so without it the run selects zero repos.

Useful flags: `--only owner/repo`, `--limit N`, `--llm <slug>`, `--num-turn N`. Without
`--repos-json` the default is `datasets/rat_python50.json`. The list of varieties is in
`varieties.toml`.

### 5. Results

Each run lands in `runs/<variety>/<run>-<timestamp>/`:

- `output/<owner>/<repo>/eval_build/Dockerfile` + `_meta.json` — what the agent produced, and its cost.
- `measure/metrics.json` — the scores, written automatically at the end of the run.

### Running on a VM

- Start long runs inside `tmux` so they survive an SSH disconnect (`tmux new -s bench`, detach with
  `Ctrl-b d`, reattach with `tmux attach -t bench`).
- Some arms print nothing until they finish (SetupX takes ~50 min per repo). That is not a hang.
- Docker images pile up; watch `df -h` and clean between runs.

## Adding SetupX

SetupX is not vendored. `tools/provision_setupx.sh` clones it to `~/agents/SetupX`, pins the commit,
applies the required `tools/setupx-bench.patch`, builds its Python 3.10 venv, starts a pgvector
container and loads SetupX's 600-entry XPU store. It is safe to re-run, and you **should** re-run it
after every `git pull`.

The XPU store's embeddings use `LLM_API_KEY` / `LLM_BASE_URL` (`openai/text-embedding-3-small` on
OpenRouter). On another provider, also set `EMBEDDING_MODEL` and `EMBEDDING_DIM` in `.env`.

```bash
# needs python3.10 on PATH, and LLM_API_KEY + SETUPX_DB_DSN in .env (both in .env.example)
./tools/provision_setupx.sh
export SETUPX_ROOT=~/agents/SetupX
```

Then run it like any other arm:

```bash
./run_bench.sh setupx --repos-json datasets/rat_python50.json --tier all --only bruin-data/ingestr
```

- **The XPU store is read-only during a run** — no new entries, no telemetry updates — so every repo
  sees the same store. Re-run the provision script before each run to reset the per-run copy
  (`xpu_run`) from the master.
- **Never create a `.env` inside `~/agents/SetupX`** — SetupX would load it and override the model
  and endpoint the benchmark passes in. The producer refuses to start if one exists.
- **Clean up after a run** — every checkpoint image is ~2 GB:

  ```bash
  docker images --filter 'reference=setup_agent_checkpoint*' -q | xargs -r docker rmi -f
  docker images --filter 'reference=setupx-mirror*' -q | xargs -r docker rmi -f
  ```

## Adding Claude Code

The `claudecode-dockerfile` arm runs the Claude Code CLI inside a `claude-runner` container, routed
to `deepseek/deepseek-v4-flash-0731` through OpenRouter's Anthropic-compatible API. The image is
built automatically from `docker/claude-runner.Dockerfile` on the first run.

The CLI can't turn reasoning off for a non-Claude model, so requests go through a small local proxy
that does it (`tools/anthropic_thinking_proxy.py`). Start it in its own terminal or `tmux` window:

```bash
PROXY_UPSTREAM_HOST=openrouter.ai PROXY_PROVIDER=deepseek \
  python3 tools/anthropic_thinking_proxy.py                                      # listens on :8931
```

`PROXY_PROVIDER` pins the OpenRouter provider (a comma-separated list, no fallbacks), which the CLI
cannot do itself. Without it, OpenRouter picks a provider per request.

Then run it. It authenticates with `LLM_API_KEY`; the two proxy settings are already in
`.env.example`:

```bash
# .env: ANTHROPIC_BASE_URL=http://host.docker.internal:8931/api   (the proxy, seen from the container)
#       PROXY_UPSTREAM_HOST=openrouter.ai                         (so cost uses OpenRouter rates)
./run_bench.sh claudecode-dockerfile --repos-json datasets/rat_python50.json --tier all
```

The CLI only speaks Anthropic's format, so on another provider point `ANTHROPIC_BASE_URL` (and the
proxy's `PROXY_UPSTREAM_HOST`) at that provider's Anthropic-compatible endpoint. The budget is 100
LLM calls per repo (`num_turn` in `varieties.toml`), the same as SetupX and SWE-agent.

## Benchmark results (`runs_results/`)

The original per-repo measurements behind the paper's tables, for each baseline on the three
datasets. `runs_results/PROVENANCE.md` records where each run came from, and
`python3 tools/reproduce_paper_table.py` checks every file against `runs_results/SHA256SUMS` and
recomputes Table 1 against `datasets/gold_manifests_combined_test_steps.json`.

```
runs_results/<arm>/<run>/_run_manifest.json                     dataset, source run, notes
runs_results/<arm>/<run>/measure/<variety>/<owner>/<repo>/row.json   one repo's score
runs_results/<arm>/<run>/measure/metrics.json, errors.json      the run's own summary, as written
runs_results/<arm>/<arm>.zip                                    the arm's Dockerfiles, trajectories, logs
```

Arms: `claudecode` (variety `claudecode-dockerfile`), `setupx`, `sweagent`
(`sweagent_repo2run_modern`), `pipreqs`, `rat`, `repo2run`, `graph2env`. Each `row.json` holds
`build_ok`, `status`, `collect_clean`, the pytest counts (`total`, `passed`, `failed`, `skipped`),
`pass_rate`, and the agent's cost (`tokens_in`, `tokens_out`, `cost_usd`). Unzip a zip at the repo
root. Its `runs/` or `results_pjy/` part holds the agent outputs and Dockerfiles; its
`runs_rescore/` part is a later re-measurement (2026-09-26) that the paper did not use.

## Scoring with `unified_bench`

`run_bench.sh` scores automatically. To re-score by hand, `source env.sh` first.

Rebuild and score one of your own runs from its Dockerfiles:

```bash
python -m bench.unified_bench --harvest <variety>=runs/<variety>/<run>/output \
  --out runs/<variety>/<run>/measure --gold datasets/gold_manifests_combined.json
```

Aggregate existing rows (e.g. `runs_results/`) into `metrics.json` without rebuilding:

```bash
python -m bench.unified_bench --aggregate-only \
  --out runs_results/claudecode/envbench100-merged-20260915/measure \
  --gold datasets/gold_manifests_combined.json
```

`metrics.json` holds the run-level EBSR, ESSR, gold-anchored ESSR and cost — report from it, not from
the per-row `ebsr` field. `--gold` adds the gold-anchored ESSR; it covers 190 of the 200 repos, and
repos without a gold count are left out of that one number.
