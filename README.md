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

`_make_model` routes **produce-able** methods (`dockeragent`, `repo2run`, `claudecode-dockerfile`) to a
`_ProducerModel` that drives the producer registry directly, and **native-lane** methods (`rat`,
`sweagent`, `claudecode`) to the live models under `runner/live/`.

### Method lanes (the `measure` tag)

- **conforming** — the agent's Dockerfile already homes the repo at `/testbed` (e.g. `dockeragent`).
- **rehome** — the Dockerfile homes the repo at `/repo`; the producer moves it to `/testbed` (`repo2run`).
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

Run any with `./run_bench.sh <variety> …`. Override the LLM with `--llm <slug>`.

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
branch  = "my-agent"                     # your pushed branch; provisioned to /opt/agents/my-agent
model   = "dockeragent"                  # routes to _ProducerModel → your adapter
venv    = "/opt/rat_venv"                # (optional) venv your agent runs under
llm     = "deepseek/deepseek-v4-flash"   # forwarded to your adapter as `model=`
measure = "conforming"                   # your Dockerfile homes the repo at /testbed
```

The variety **name** is also the run bucket (`runs/my-agent`) and the checkout dir
(`agents/my-agent` / `/opt/agents/my-agent`) — keep it equal to your branch.

### 3. Provisioning

`provision_agent` runs `git fetch origin <branch> && git reset --hard FETCH_HEAD` in the checkout dir
(never `git clean` — it preserves your `workplace/`). So **push your branch**, and the first time clone
it into the checkout dir once (fetch needs an existing git repo there). `SKIP_PROVISION=1` skips the
fetch/reset — use it to run **local uncommitted WIP** in a vendored checkout (this is what `run_bench.sh`
does).

Then: `./run_bench.sh my-agent --only owner/repo`.

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
