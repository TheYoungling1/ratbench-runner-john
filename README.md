# env-bench

A self-contained environment-construction benchmark. An **agent** reads a repository and
produces a Dockerfile + setup script; the harness **builds** it and runs pytest; a **measure**
stage rebuilds each Dockerfile from scratch and re-scores it for reproducible EBSR/ESSR.

Everything needed to run is vendored here — the harness, the RAT eval framework, the agent(s),
and the datasets. You need only Docker, Python 3.10+, and an API key.

## Quickstart

```bash
git clone <this-repo> env-bench && cd env-bench
./setup.sh                       # creates .venv, installs deps (needs a running Docker daemon)
cp .env.example .env             # add your OPENROUTER_API_KEY
./run_bench.sh john-planner-v3 --tier all --limit 2 --concurrency 2   # smoke: 2 repos
```

A full run:
```bash
./run_bench.sh john-planner-v3 --tier all --concurrency 4
```

Output lands in `runs/<variety>/<run>-<timestamp>/`:
- `output/<owner>/<repo>/eval_build/Dockerfile` + `_result_row.json` — produced env + inline score.
- `measure/metrics.json` — reproducible re-measured **EBSR / ESSR / ESSR_all** + collection
  diagnostics + economy (written automatically after a successful run).

## What `run_bench.sh` does

1. Points `HARNESS_ROOT / RAT_ROOT / AGENTS_ROOT / BENCH_ROOT / RUNS_ROOT` at this repo.
2. Sets `SKIP_PROVISION=1` (agents are vendored snapshots, not live git checkouts).
3. Loads `.env`, then runs the harness through `.venv/bin/python`.
4. The harness runs the agent → builds the Dockerfile → scores it → runs the measure stage.

## Varieties (the methods)

Defined in `harness/varieties.toml`:

| variety | what it is |
|---|---|
| `john-planner-v3` | the vendored graph-scheduled environment-construction agent (`agents/john-planner-v3/`) |
| `rat` | RunAnyThing baseline (no agent) |
| `repo2run` | Repo2Run baseline (no agent) |
| `sweagent` | SWE-agent baseline (needs the extra deps in `requirements.txt`) |

Run any with `./run_bench.sh <variety> …`. Override the LLM with `--llm <slug>`.

## Datasets

`datasets/` holds the repo lists (`--repos-json`), pinned to commits where noted:
- `rat_python50.json` — the 50-repo Python set (default flavour).
- `rat_python50_pinned_m3nothink.json` — the same 50 with per-repo `commit` pins (reproducible).
- `rat_python_hard_subset.json`, `rat_python_medlarge15.json`, `rat_python50_large.json`, `rat_node50.json`.

Pick one with `--repos-json datasets/<file>` (default is the harness's built-in). Filter/limit with
`--limit N`, `--only owner/repo`, `--tier all|smoke|extended`.

## Scoring

- **Inline** (per run): `_result_row.json` per repo; recompute with
  `python harness/scripts/compute_essr.py <variety>=runs/<variety>/<run>`.
- **Reproducible** (`measure/metrics.json`): rebuilds each Dockerfile clean and re-runs pytest.
  `EBSR` = Repo2Run collect-only gate; `ESSR`/`ESSR_all` = RAT pass-rate ÷exec/÷all; plus collection
  and token/image economy. Re-run by hand:
  ```bash
  source env.sh
  python -m bench.unified_bench --harvest john-planner-v3=runs/john-planner-v3/<run>/output \
                                --out runs/john-planner-v3/<run>/measure
  ```

## Adding another agent

1. Put the agent's runnable code under `agents/<name>/` — it must expose
   `multi_docker_eval_adapter.py::MultiDockerEvalAdapter(output_dir).process_single_instance(instance, …)`
   returning `{instance_id: {"dockerfile", "setup_scripts", "base_image", "logs"}}` (see the vendored
   `agents/john-planner-v3/` for the shape). A `dockeragent`-model agent reuses the harness's model
   wrapper — no new model file.
2. Add a block to `harness/varieties.toml`:
   ```toml
   [variety.<name>]
   branch = "<label>"       # provenance only under SKIP_PROVISION
   model  = "dockeragent"
   llm    = "deepseek/deepseek-v4-flash"
   ```
3. `./run_bench.sh <name> …`.

## Notes / gotchas

- **Docker is required and does real work** — the benchmark builds one image per repo. Run on a box
  with a working daemon and a few GB free.
- **Always launch via `./run_bench.sh`** (or `source env.sh` first). Running `harness/bench` with the
  system python fails — the runner needs `python-dotenv`, which lives in `.venv`.
- **`sentence-transformers` is heavy** (pulls torch). If your agent doesn't use embeddings you can
  remove it from `requirements.txt` for a lighter install.
- **Never commit `.env`** — it's gitignored; share keys out of band.
- The RAT framework under `rat/` is vendored from "RunAnyThing"; check its upstream terms before
  redistributing further.
