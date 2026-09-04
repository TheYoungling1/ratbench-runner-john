# SetupX Baseline Variety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `setupx` as a `measure="rehome"` baseline variety that runs OpenDataBox/SetupX against DeepSeek's direct API with its 600-entry warm XPU store loaded read-only, and folds the agent's trajectory into one rebuildable Dockerfile homed at `/testbed`.

**Architecture:** SetupX emits no Dockerfile — it mutates a live container and writes a JSON report whose `setup.history` is the complete list of shell commands it ran. The producer replays that history (honouring SetupX's snapshot/rollback semantics) into a single build layer on top of a pinned clone, then re-homes `/workspace/repo` to `/testbed`. This is the `producers/executionagent.py` pattern with a different runner and trace format; `bench` rebuilds and scores the result exactly as it does for `repo2run` / `executionagent`.

**Relationship to the metering ledger plan.** `docs/superpowers/plans/2026-09-03-metering-ledger-stage1-stage2.md` Stage 2 stands a loopback proxy in front of each arm and derives `tokens_in`/`tokens_out`/`llm_calls`/`cost_usd` from its ledger. When it lands, **`setupx` should be added to its Stage 2 scope** — it is the cheapest arm to redirect, because it reads `OPENAI_BASE_URL` directly (`src/config.py:94`) and POSTs to `f"{base_url}/chat/completions"`, with none of the base-URL sniffing that makes `dockeragent` risky (that plan's verified facts 2 and 7). The ledger then supplies the token and cost fields this plan leaves `None`. It does **not** replace the in-process cap: that plan's own Q4 decided the proxy records and warns rather than enforces, and its first global constraint is that metering must not change behaviour — a budget is a behaviour change. Enforcement stays in-process; the ledger's request count and this arm's `llm_calls` then cross-check each other, which is exactly the self-report-independent check that plan's Phase 2.2 asks for.

**Tech Stack:** Python 3.10 (runner venv), Python 3.10+ separate venv for the SetupX checkout, Docker (base image `python:3.10`), PostgreSQL 16 + pgvector, DeepSeek direct API (`https://api.deepseek.com/v1`), OpenRouter's OpenAI-compatible embeddings endpoint (`openai/text-embedding-3-small`, 1536 dims).

**Spec:** This document. Section **Upstream Facts** below is the verified research this plan argues from — every claim there was read out of the SetupX source at commit `main` on 2026-09-03, not inferred.

## Global Constraints

- **Runner venv is Python 3.10.** SetupX runs in its own venv at `$SETUPX_ROOT/.venv`; the producer only ever subprocesses it. Do not add SetupX's dependencies to this repo's `requirements.txt`.
- **No new dependencies in `requirements.txt`.** The producer uses stdlib `subprocess`/`json`/`glob` only.
- **Commit pinning is enforced at every clone site** (README, "Commit pinning"). Both the produce-phase clone and the emitted Dockerfile's clone must land on `repo.commit`.
- **Sweep the docker image store between runs.** Measured on the first smoke run: a single
`initial_clone` checkpoint is **2.07 GB**, and SetupX commits another before every XPU trial.
`_sweep_checkpoints` clears them on the normal and crashed paths, but a SIGKILL bypasses it — an
interrupted 50-repo run can leave tens of gigabytes behind. Run
`docker images --filter 'reference=setup_agent_checkpoint*' -q | xargs -r docker rmi -f` and
`docker image prune` after an interrupted run; the per-repo `setupx-mirror:*` images accumulate too.

**The XPU store never accumulates across runs.** The 600 entries from `data/xpu_warm.jsonl` are the corpus. Each run works on a `CREATE DATABASE ... TEMPLATE` copy so nothing it learns survives into the next run. A SELECT-only role cannot be used — see Task 5 Step 4.
- **LLM slug:** `deepseek/deepseek-v4-flash`, served by DeepSeek's own API, key `DEEPSEEK_API_KEY` (already in `.env`). SetupX takes a bare model name, so the `deepseek/` provider prefix is stripped before it is passed on.
- **Anti-vanish invariant** (`producers/base.py` §1): `produce()` never raises. Every failure path returns `ProducedEnv(status="error", ...)`.
- **The SetupX checkout is pinned to `85de35515c45954b72afb9678dfd172855bfb847`** and carries `tools/setupx-bench.patch`. Without that patch the arm is correct only at `--concurrency 1`.
- **File size:** keep `producers/setupx.py` under 400 lines. If it grows past that, split the pure transforms into `producers/setupx_replay.py`.

---

## Upstream Facts

Verified by reading the SetupX source. The implementation depends on all of these; re-check them if you re-clone at a newer commit.

**Entry point.** `scripts/run.sh` requires a `.env` and `exec`s `python -m src.main`. The plan calls `python -m src.main` directly so no `.env` is needed.

**CLI:** `main(repo_url, --max-steps 9999, --phase1-timeout 1800, --no-xpu, --output-dir log, --meta-json)`. Exit **0** = phase 2 `not_guilty`, **1** = `guilty` or phase 1 unfinished, **2** = error.

**Report:** `<output-dir>/<basename-of-repo-url>_result.json`, shaped
`{"repo_url", "setup": {"repo_url", "container_id", "completed", "steps_taken", "final_message", "history": [...], "last_verify_messages": [...]}, "phase2": {"success", "reason", "prosecution", "judgment"}}`.

**Container** (`src/environment_manager.py`): base image `DOCKER_BASE_IMAGE` (code default `ubuntu:22.04`, but `.env.example` advertises `python:3.10`), work dir `DOCKER_WORK_DIR` (default `/workspace`). Bootstrap runs `which git || (apt-get update && apt-get install -y git)`, `mkdir -p /workspace`, `git config --global http.version HTTP/1.1`, then `git clone --depth=1 <repo_url> /workspace/repo` (3 retries), then `create_checkpoint("initial_clone")`. **The clone is unpinned** — this is why Task 3 builds a pinned mirror image.

**Every `SHELL_COMMAND` runs as** `timeout 300 bash -c "cd /workspace/repo && <command>"` with `environment=` carrying the `SET_ENV` vars. A `cd` inside one command does not persist to the next.

**History entry:** `AgentState.add_to_history` (`src/models.py:165-170`) prepends two fields, so the real shape is `{"step", "timestamp", "action": {"thought", "action_type", "content": {...}}, "result": {"exit_code", "stdout", "stderr", ...}}`. `action_type` is one of `SHELL_COMMAND`, `TRY_XPU_SUGGESTION`, `SET_ENV`, `ROLLBACK_ENV`, `VERIFY`, `FINISH`. `content` carries `command` for `SHELL_COMMAND`; `command`/`xpu_suggestion_id`/`reasoning` for `TRY_XPU_SUGGESTION`; `env_key`/`env_value` for `SET_ENV`; `n_frames` for `ROLLBACK_ENV`. `VERIFY` and `FINISH` carry no command.

**`SET_ENV` survives a rollback.** `EnvironmentManager._env_vars` is a live dict that `set_env` only adds to; it is never snapshotted and never restored. `rollback_to_checkpoint` re-creates the container with `environment=self._env_vars.copy()` — the *current* vars, not the checkpoint's. So a rollback undoes filesystem state only.

**A phase-1 wall-clock timeout writes no report.** `main.py`'s `except Phase1Timeout` handler returns `1` at line 263; Stage 3, which writes `*_result.json`, is at line 385. Step exhaustion does reach Stage 3. This is why the arm is bounded by `--max-steps`, not by the clock.

**Checkpoint images share one global tag namespace** (`setup_agent_checkpoint:{initial_clone,step_<n>_pre_xpu}`) and `EnvironmentManager.__init__` deletes every one of them on construction (`:74`). Concurrent runs destroy each other's snapshots.

**`XpuVectorStore.__init__` runs DDL** — `self._ensure_table()` → `create_xpu_table` (CREATE EXTENSION / CREATE TABLE / CREATE INDEX) — and `xpu_client.py:237` constructs it with no `try`. A role that cannot execute that DDL crashes every run before Stage 1, unrecoverably.

**bench invokes `python`, never `python3`** (`bench/bench/languages/python.py`: `_ENSURE`, `gate_cmd`, `collect_cmd`, run cmd). `ubuntu:22.04` ships `python3` only and no `pip`, so an ubuntu-based emitted image fails the gate for reasons unrelated to the agent. SetupX's own `.env.example` advertises `DOCKER_BASE_IMAGE=python:3.10`.

**Snapshot semantics** (this is the whole reason a naive linear replay is wrong):
- `create_checkpoint(tag)` pushes onto a LIFO stack. It is called exactly twice in the codebase: once for `initial_clone` after the clone, and once as `step_<n>_pre_xpu` at the top of every `TRY_XPU_SUGGESTION`.
- `rollback_to_checkpoint(n)` returns `False` on an empty stack; otherwise pops `min(n, depth)` tags and restores the container to **the last tag popped**. The popped tags are gone.
- A `TRY_XPU_SUGGESTION` has **four** outcomes, and they differ in what they leave on the stack:
  - `[XPU SUCCESS]` — checkpoint taken, trial kept, frame **stays** on the stack.
  - `[XPU FAIL]` — checkpoint taken, then `rollback_to_checkpoint()` (n=1) pops it and restores (`src/agent.py` §F).
  - `[XPU SKIP]` — the suggestion had no executable command. The checkpoint was **already taken** (`agent.py:346`) and the branch returns at `:373` **without** rolling back, so the frame stays. Popping it here makes every later `ROLLBACK_ENV` truncate one frame too far.
  - `[XPU BLOCKED]` — the id was not in the pool; returns at `agent.py:339`, **before** `create_checkpoint`, so no frame is pushed at all.
- `ROLLBACK_ENV` records `result.exit_code == 0` when the rollback happened, `1` when the stack was empty.

**Lossy case:** when the main agent supplies no adapted command, `TRY_XPU_SUGGESTION` falls back to atom-rendered `suggestion.commands`, which are **not** recorded in `content.command`. A `[XPU SUCCESS]` entry with `content.command == None` is unreplayable — the run is flagged `unreplayed=True`.

**Agent steps are not LLM calls, and stock SetupX caps neither.** `--max-steps` bounds steps; a single step costs one `generate_action` call plus the retriever's refinement and audit calls, and a `VERIFY` step runs the verifier's entire ReAct sub-loop. Phase 2 adds the prosecutor's multi-turn investigation and the judge. Every completion in the system funnels through `LLMClientBase.chat` (`llm_engine.py`, two implementations), which is the single point where a call budget can be counted and enforced; `text_to_embedding` uses the OpenAI SDK directly and is correctly outside it.

**Phase-2 commands are not in `history`.** `VerifierAgent` runs its own ReAct sub-loop (transcript in `last_verify_messages`) and can write files (`verifier_agent.py:211`); `ProsecutorAgent` (`:300`) and `JudgeAgent` (`:191`) also `exec_run` against the same container. None of it is replayed.

**LLM client** (`src/llm_engine.py`): raw `httpx` POST to `f"{OPENAI_BASE_URL}/chat/completions"` with `max_tokens: 4096` and, in JSON mode, `response_format: {"type": "json_object"}`. Config comes from `LLM_PROVIDER` (`openai`|`ark`), `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`. **No token accounting** — `usage` is discarded, so `tokens_in`/`tokens_out`/`cost_usd` cannot be populated.

**`src/config.py` calls `load_dotenv(PROJECT_ROOT / ".env", override=True)` and the same for `.env.local` at import time.** `override=True` means a `.env` in the SetupX checkout **silently overrides the environment the producer passes in**. The checkout ships only `.env.example`. Task 3 fails loudly if either file exists.

**XPU:** `create_xpu_client()` returns a `VectorXPUClient` (pgvector) only when `XPU_VECTOR_ENABLED` is truthy **and** `dns` or `XPU_DB_DNS` is set; otherwise a no-op stub. `increment_telemetry` is skipped entirely when `FREEZE_TELEMETRY=1`. Embeddings come from `text_to_embedding`, an OpenAI-compatible `/embeddings` call configured by `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL`, **falling back to `OPENAI_API_KEY` + `OPENAI_BASE_URL` when `EMBEDDING_API_KEY` is unset**. DeepSeek serves no embeddings endpoint (its pricing page lists only `deepseek-v4-flash`, `-pro` and `-flash-vision-exp`), so `EMBEDDING_API_KEY` must be set explicitly or every retrieval 404s. OpenRouter serves an OpenAI-compatible `/embeddings` route, so the key this repo already uses for `sweagent` / `executionagent` covers it — no new vendor. `EMBEDDING_DIM` (default 1536) is baked into the table schema and must match the model.

**Write paths, and why they cannot simply be revoked:** `submit_feedback` and `increment_telemetry` are both wrapped in `except Exception` and logged, and `_store_xpu_experience` (`main.py`) runs in the `finally` after phase 2, in a process that exits immediately afterwards — so *these* would tolerate a permission error. The connect-time DDL in `XpuVectorStore.__init__` would not: it is unguarded and kills the process. That is why the store is isolated per run by database copy rather than by privilege (Task 5 Step 4).

---

## File Structure

- **`producers/setupx.py`** (create) — the whole producer: two pure transforms (`plan_replay`, `render_dockerfile`), the live runner (`run_setupx`), and `SetupXProducer`. Mirrors `producers/executionagent.py`'s shape so a reader who knows one knows the other.
- **`producers/tests/test_setupx.py`** (create) — unit tests. SetupX is stubbed via the injected `runner=` seam; no docker, no keys, no Postgres.
- **`producers/__init__.py`** (modify) — one import + one `register(...)` + two `__all__` entries.
- **`runner/benchmark.py`** (modify) — add `"setupx"` to `_PRODUCE_ABLE`, to the `num_turn` kwarg branch, and to the `agent_root` selection.
- **`varieties.toml`** (modify) — the `[variety.setupx]` block.
- **`README.md`** (modify) — a "SetupX setup" section under "ExecutionAgent setup", plus a row in the varieties table.
- **`.env.example`** (modify) — the DeepSeek, embedding, and XPU DSN key names.

---

### Task 1: The replay planner

The trajectory-to-steps transform, with SetupX's snapshot semantics. Pure — no docker, no filesystem.

**Files:**
- Create: `producers/setupx.py`
- Test: `producers/tests/test_setupx.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `plan_replay(history: list) -> tuple[list, bool]`. Returns `(steps, lossy)` where `steps` is a list of `("run", command)` and `("env", key, value)` tuples in execution order, and `lossy` is `True` when a surviving `[XPU SUCCESS]` step had no recorded command. Task 2 renders `steps`; Task 4 maps `lossy` to `ProducedEnv.unreplayed`.

- [ ] **Step 1: Write the failing tests**

Create `producers/tests/test_setupx.py`:

```python
# producers/tests/test_setupx.py — the SetupX trajectory transforms + SetupXProducer.
#
# SetupX is STUBBED (injected via runner=) — no SetupX checkout, no docker, no keys, no Postgres.
from producers.setupx import plan_replay


def _shell(cmd, exit_code=0):
    return {"action": {"thought": "", "action_type": "SHELL_COMMAND",
                       "content": {"command": cmd}},
            "result": {"exit_code": exit_code, "stdout": "", "stderr": ""}}


def _set_env(key, value):
    return {"action": {"thought": "", "action_type": "SET_ENV",
                       "content": {"env_key": key, "env_value": value}},
            "result": {"exit_code": 0, "stdout": f"[SET_ENV] {key}={value}", "stderr": ""}}


def _xpu(cmd, ok=True, sid="xpu_1"):
    outcome = "SUCCESS" if ok else "FAIL"
    return {"action": {"thought": "", "action_type": "TRY_XPU_SUGGESTION",
                       "content": {"xpu_suggestion_id": sid, "command": cmd, "reasoning": ""}},
            "result": {"exit_code": 0, "stdout": f"[XPU {outcome}] {sid}\n", "stderr": ""}}


def _rollback(n_frames=1, ok=True):
    return {"action": {"thought": "", "action_type": "ROLLBACK_ENV",
                       "content": {"n_frames": n_frames}},
            "result": {"exit_code": 0 if ok else 1, "stdout": "[ROLLBACK_ENV] ok", "stderr": ""}}


def _verify():
    return {"action": {"thought": "", "action_type": "VERIFY", "content": {"hint": "run pytest"}},
            "result": {"exit_code": 0, "stdout": "collected 12 items", "stderr": ""}}


def test_shell_commands_replay_in_order():
    steps, lossy = plan_replay([_shell("pip install -e ."), _shell("pip install pytest")])
    assert steps == [("run", "pip install -e ."), ("run", "pip install pytest")]
    assert lossy is False


def test_failed_commands_are_kept_because_the_agent_kept_them():
    # SetupX does not undo a non-zero command; only ROLLBACK_ENV and a failed XPU trial undo state.
    # Dropping it here would diverge from the container the agent actually finished with.
    steps, _ = plan_replay([_shell("apt-get install -y libfoo", exit_code=100),
                            _shell("apt-get install -y libfoo-dev")])
    assert steps == [("run", "apt-get install -y libfoo"),
                     ("run", "apt-get install -y libfoo-dev")]


def test_verify_and_finish_entries_contribute_no_steps():
    steps, _ = plan_replay([_shell("pip install pytest"), _verify()])
    assert steps == [("run", "pip install pytest")]


def test_set_env_survives_as_its_own_step():
    steps, _ = plan_replay([_set_env("PIP_NO_CACHE_DIR", "1"), _shell("pip install -e .")])
    assert steps == [("env", "PIP_NO_CACHE_DIR", "1"), ("run", "pip install -e .")]


def test_a_successful_xpu_trial_is_replayed():
    steps, lossy = plan_replay([_xpu("apt-get install -y libpq-dev", ok=True)])
    assert steps == [("run", "apt-get install -y libpq-dev")]
    assert lossy is False


def test_a_failed_xpu_trial_is_dropped_because_the_agent_auto_rolled_back():
    steps, _ = plan_replay([_shell("pip install -e ."),
                            _xpu("pip install broken-pkg", ok=False),
                            _shell("pip install pytest")])
    assert steps == [("run", "pip install -e ."), ("run", "pip install pytest")]


def test_rollback_truncates_back_to_the_checkpoint_the_trial_created():
    # ROLLBACK_ENV(1) pops the surviving pre-XPU checkpoint and restores to it, discarding the
    # trial's own command and everything after it.
    steps, _ = plan_replay([_shell("a"), _xpu("b", ok=True), _shell("c"), _rollback(1)])
    assert steps == [("run", "a")]


def test_rollback_of_more_frames_than_exist_lands_on_the_initial_clone():
    steps, _ = plan_replay([_shell("a"), _shell("b"), _rollback(5)])
    assert steps == []


def test_a_rollback_that_upstream_reported_as_failed_changes_nothing():
    steps, _ = plan_replay([_shell("a"), _rollback(1, ok=False)])
    assert steps == [("run", "a")]


def test_env_set_before_a_rollback_survives_it():
    # EnvironmentManager._env_vars is never snapshotted: rollback_to_checkpoint recreates the
    # container with `environment=self._env_vars.copy()`, the CURRENT dict. Only files are undone.
    steps, _ = plan_replay([_xpu("b", ok=True), _set_env("CFLAGS", "-O0"), _rollback(1)])
    assert steps == [("env", "CFLAGS", "-O0")]


def _xpu_skip(sid="xpu_1"):
    # The suggestion had no executable command: agent.py records this AFTER taking the checkpoint
    # and returns WITHOUT rolling back, so the frame stays on the stack.
    return {"action": {"thought": "", "action_type": "TRY_XPU_SUGGESTION",
                       "content": {"xpu_suggestion_id": sid, "command": None, "reasoning": ""}},
            "result": {"exit_code": 1,
                       "stdout": f"[XPU SKIP] {sid}: no commands; not executed", "stderr": ""}}


def _xpu_blocked(sid="xpu_9"):
    # The id was not in the current pool: agent.py returns BEFORE create_checkpoint, no frame.
    return {"action": {"thought": "", "action_type": "TRY_XPU_SUGGESTION",
                       "content": {"xpu_suggestion_id": sid, "command": None, "reasoning": ""}},
            "result": {"exit_code": 1,
                       "stdout": f"[XPU BLOCKED] suggestion {sid} not available", "stderr": ""}}


def test_a_skipped_xpu_trial_keeps_its_checkpoint_so_a_later_rollback_lands_right():
    # Popping the SKIP frame here would make the rollback truncate one frame too far.
    steps, _ = plan_replay([_shell("a"), _xpu_skip(), _shell("b"), _rollback(1)])
    assert steps == [("run", "a")]


def test_a_blocked_xpu_trial_pushes_no_checkpoint_at_all():
    steps, _ = plan_replay([_shell("a"), _xpu_blocked(), _shell("b"), _rollback(1)])
    assert steps == []


def test_a_successful_xpu_trial_with_no_recorded_command_is_flagged_lossy():
    # The agent fell back to atom-rendered suggestion.commands, which to_dict() never recorded.
    entry = _xpu(None, ok=True)
    steps, lossy = plan_replay([_shell("a"), entry])
    assert steps == [("run", "a")]
    assert lossy is True


def test_empty_history_is_empty_not_an_error():
    assert plan_replay([]) == ([], False)
    assert plan_replay(None) == ([], False)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest producers/tests/test_setupx.py -v`
Expected: FAIL, collection error `ModuleNotFoundError: No module named 'producers.setupx'`.

- [ ] **Step 3: Write the minimal implementation**

Create `producers/setupx.py`:

```python
# producers/setupx.py
#
# The `setupx` producer: OpenDataBox/SetupX (XPU knowledge store + speculative execution +
# prosecutor/judge verification) as a baseline arm.
#
# SetupX emits NO Dockerfile — it mutates a live container and writes a JSON report. What it does
# record is `setup.history`: every shell command it ran, in order. This producer replays that
# history into one build layer on top of a pinned clone and re-homes /workspace/repo -> /testbed,
# so the arm gets a real EBSR/ESSR row comparable with repo2run / executionagent / dockeragent.
# Same shape as producers/executionagent.py, different trace format.
#
# The replay is NOT a linear `for cmd in history` — SetupX keeps a LIFO stack of `docker commit`
# checkpoints and rolls back to them, so work the agent undid must be undone in the replay too.
# `plan_replay` mirrors EnvironmentManager.rollback_to_checkpoint exactly; see the tests.
from __future__ import annotations

from producers.base import ProduceContext, ProducedEnv

# bench.schema is on the path via producers.base's shim (imported above).
from bench.schema import RepoSpec  # noqa: E402


def _truncate(steps: list, mark: int) -> None:
    """Undo the run-steps after `mark` — but KEEP the env-steps.

    Upstream's rollback recreates the container from the checkpoint image with
    `environment=self._env_vars.copy()`: the CURRENT dict, which `set_env` only ever adds to and
    nothing ever restores. A SET_ENV issued after a checkpoint therefore SURVIVES the rollback —
    only filesystem state is undone. Marks still on the stack were pushed at or before `mark`, so
    they stay valid.
    """
    steps[mark:] = [s for s in steps[mark:] if s[0] == "env"]


def plan_replay(history: list | None) -> tuple:
    """Turn SetupX's `setup.history` into the steps that survive its rollbacks.

    Returns ``(steps, lossy)``. `steps` is a list of ``("run", command)`` / ``("env", key, value)``
    tuples in execution order. `lossy` is True when a surviving XPU trial succeeded but recorded no
    command (the agent fell back to atom-rendered `suggestion.commands`, which `AgentAction.to_dict`
    drops) — the caller surfaces that as `unreplayed`.

    Mirrors upstream exactly: `marks` is EnvironmentManager's `_history_snapshots` stack, holding
    the step index each checkpoint was taken at. `initial_clone` is the mark at 0, pushed by
    `_setup_container` after the clone; every TRY_XPU_SUGGESTION pushes another before its trial.
    """
    steps: list = []
    marks: list = [0]                      # the "initial_clone" checkpoint
    lossy = False
    for entry in history or []:
        action = (entry or {}).get("action") or {}
        result = (entry or {}).get("result") or {}
        content = action.get("content") or {}
        kind = action.get("action_type")

        if kind == "SHELL_COMMAND":
            command = content.get("command")
            if command:
                steps.append(("run", command))

        elif kind == "SET_ENV":
            key = content.get("env_key")
            if key:
                value = content.get("env_value")
                steps.append(("env", key, "" if value is None else str(value)))

        elif kind == "TRY_XPU_SUGGESTION":
            # FOUR outcomes, and they differ in what they leave on the snapshot stack.
            stdout = result.get("stdout") or ""
            if stdout.startswith("[XPU BLOCKED]"):
                pass                       # returns BEFORE create_checkpoint; pushes no frame
            else:
                marks.append(len(steps))   # agent.py takes step_<n>_pre_xpu before the trial
                if stdout.startswith("[XPU SUCCESS]"):
                    command = content.get("command")
                    if command:
                        steps.append(("run", command))
                    else:
                        lossy = True       # atom-rendered commands were never recorded
                elif stdout.startswith("[XPU SKIP]"):
                    pass                   # returns without rolling back; the frame SURVIVES
                else:                      # [XPU FAIL] => auto-rollback of one frame (branch F)
                    _truncate(steps, marks.pop())

        elif kind == "ROLLBACK_ENV":
            # exit_code 1 => upstream found an empty stack and returned False without popping.
            if marks and result.get("exit_code") == 0:
                n = min(max(1, int(content.get("n_frames") or 1)), len(marks))
                for _ in range(n):
                    target = marks.pop()   # restore to the LAST tag popped
                _truncate(steps, target)

        # VERIFY / FINISH carry no command. The VerifierAgent's own commands live in
        # `last_verify_messages`, not here, so a fix it made itself is lost — see the README note.
    return steps, lossy
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest producers/tests/test_setupx.py -v`
Expected: PASS — every test in the file.

- [ ] **Step 5: Commit**

```bash
git add producers/setupx.py producers/tests/test_setupx.py
git commit -m "feat(setupx): replay planner honouring SetupX snapshot/rollback semantics"
```

---

### Task 2: The Dockerfile renderer

Turns the surviving steps into a conforming Dockerfile homed at `/testbed`. Pure.

**Files:**
- Modify: `producers/setupx.py`
- Test: `producers/tests/test_setupx.py`

**Interfaces:**
- Consumes: `plan_replay`'s `steps` from Task 1.
- Produces: `render_dockerfile(repo: RepoSpec, steps: list, *, base_image: str = DEFAULT_BASE_IMAGE) -> tuple`, returning `(dockerfile, scripts)` where `scripts` is `{REPLAY_BASENAME: <script text>}` for `ProducedEnv.setup_scripts`. Constants `DEFAULT_BASE_IMAGE = "python:3.10"`, `WORK_DIR = "/workspace/repo"`, `REPLAY_BASENAME = "setupx_replay.sh"`.

- [ ] **Step 1: Write the failing tests**

Append to `producers/tests/test_setupx.py`:

```python
from producers.base import RepoSpec
from producers.setupx import REPLAY_BASENAME, render_dockerfile


def _repo(commit="abc123"):
    return RepoSpec("o/r", "https://github.com/o/r", commit=commit, language="python")


def test_the_clone_is_pinned_to_the_dataset_sha_before_anything_is_installed():
    df, scripts = render_dockerfile(_repo(), [("run", "pip install -e .")])
    assert "git clone https://github.com/o/r /workspace/repo" in df
    assert "fetch --depth 1 origin abc123" in df
    assert "checkout --detach abc123" in df
    assert df.index("checkout --detach abc123") < df.index(REPLAY_BASENAME)


def test_a_repo_with_no_commit_clones_shallow_without_a_pin():
    df, _ = render_dockerfile(_repo(commit=None), [("run", "true")])
    assert "git clone --depth=1 https://github.com/o/r /workspace/repo" in df
    assert "checkout --detach" not in df


def test_the_repo_is_rehomed_to_testbed_with_a_reverse_symlink():
    # bench probes `git -C /testbed rev-parse --show-toplevel` and compares it to "/testbed"
    # (bench/bench/contract.py); a symlinked /testbed resolves to its physical path, mismatches, and
    # is classified non_conforming. So /testbed must be the real directory; the reverse symlink
    # keeps paths the replay baked in resolving.
    df, _ = render_dockerfile(_repo(), [("run", "true")])
    assert "mv /workspace/repo /testbed" in df
    assert "ln -sfn /testbed /workspace/repo" in df
    assert df.rstrip().endswith("WORKDIR /testbed") or "WORKDIR /testbed" in df
    assert df.index("mv /workspace/repo /testbed") > df.index(REPLAY_BASENAME)


def test_safe_directory_is_declared_for_the_new_worktree_path():
    # bench's contract probe classifies the container by `git -C /testbed rev-parse --show-toplevel`,
    # which fails closed to non_conforming if git refuses the directory as dubiously owned.
    df, _ = render_dockerfile(_repo(), [("run", "true")])
    assert "safe.directory /testbed" in df
    assert df.index("mv /workspace/repo /testbed") < df.index("safe.directory /testbed")


def test_env_steps_become_both_an_export_in_the_replay_and_a_dockerfile_env():
    # export: so later replayed commands see it, as they did under SetupX's `environment=`.
    # ENV: so it is still set in the container bench measures.
    # Use a value that actually needs quoting: shlex.quote("-O0") is a no-op, so asserting on
    # "ENV CFLAGS='-O0'" would fail against a correct renderer.
    df, scripts = render_dockerfile(_repo(), [("env", "CFLAGS", "-O0 -g"), ("run", "pip install -e .")])
    assert "ENV CFLAGS='-O0 -g'" in df
    assert "export CFLAGS='-O0 -g'" in scripts[REPLAY_BASENAME]


def test_a_multiline_env_value_is_left_out_of_the_dockerfile_but_kept_in_the_replay():
    # ENV has no line continuation, so a newline in the value is a Dockerfile parse error. The
    # export in the replay script still carries it, and bash handles it fine.
    df, scripts = render_dockerfile(_repo(), [("env", "PATCH", "line1\nline2")])
    assert "ENV PATCH" not in df
    assert "export PATCH=" in scripts[REPLAY_BASENAME]


def test_every_replayed_command_reenters_the_work_dir_like_upstream_did():
    # Upstream runs each command as `bash -c "cd /workspace/repo && <cmd>"`, so a `cd` in one
    # command never leaked into the next. Re-cd-ing before each step reproduces that.
    _, scripts = render_dockerfile(_repo(), [("run", "cd docs"), ("run", "pip install -e .")])
    body = scripts[REPLAY_BASENAME]
    assert body.count("cd /workspace/repo &&") == 2


def test_the_replay_tolerates_failing_steps():
    # The agent's own run continued past non-zero exits; a `set -e` replay would abort the build on
    # the first probe command that upstream shrugged off.
    _, scripts = render_dockerfile(_repo(), [("run", "false")])
    assert "set +e" in scripts[REPLAY_BASENAME]
    assert scripts[REPLAY_BASENAME].rstrip().endswith("exit 0")


def test_multiline_commands_survive_rendering():
    heredoc = "cat > conftest.py <<'PY'\nimport sys\nPY"
    _, scripts = render_dockerfile(_repo(), [("run", heredoc)])
    assert heredoc in scripts[REPLAY_BASENAME]


def test_the_base_image_provides_the_python_binary_bench_invokes():
    # Every bench command is `python -m ...` (bench/bench/languages/python.py); ubuntu:22.04 ships
    # python3 only and no pip, which would fail the gate for a reason that is not the environment.
    df, _ = render_dockerfile(_repo(), [("run", "true")])
    assert df.startswith("FROM python:3.10\n")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest producers/tests/test_setupx.py -v -k 'render or clone or rehome or safe or env_steps or replay or base_image'`
Expected: FAIL with `ImportError: cannot import name 'render_dockerfile'`.

- [ ] **Step 3: Write the minimal implementation**

Add to `producers/setupx.py`, after the imports:

```python
import shlex

DEFAULT_BASE_IMAGE = "python:3.10"         # SetupX's own .env.example default. NOT ubuntu: every
                                           # bench command is `python -m ...` (bench/bench/languages/
                                           # python.py) and ubuntu ships python3 only, with no pip.
WORK_DIR = "/workspace/repo"               # DOCKER_WORK_DIR + "/repo"; where the agent worked
REPLAY_BASENAME = "setupx_replay.sh"
```

and after `plan_replay`:

```python
def _clone_lines(repo: RepoSpec) -> str:
    """The pinned clone, in the exact shape the README's commit-pinning invariant prescribes."""
    if not repo.commit:
        # No dataset SHA: shallow-clone the default branch, same as SetupX's own bootstrap.
        return f"RUN git clone --depth=1 {repo.repo_url} {WORK_DIR}\n"
    return (f"RUN git clone {repo.repo_url} {WORK_DIR} \\\n"
            f" && git -C {WORK_DIR} fetch --depth 1 origin {repo.commit} \\\n"
            f" && git -C {WORK_DIR} checkout --detach {repo.commit}\n")


def render_replay(steps: list) -> str:
    """Render the surviving steps as ONE bash script.

    `set +e` because the agent's own run continued past non-zero exits — a `set -e` replay would
    abort the build on the first probe command upstream shrugged off. Each step re-enters the work
    dir because upstream ran every command in its own `bash -c "cd /workspace/repo && ..."`.
    """
    lines = ["#!/usr/bin/env bash",
             "# SetupX replay: the agent's surviving trajectory, folded into one build layer.",
             "set +e",
             ""]
    for step in steps:
        if step[0] == "env":
            lines.append(f"export {step[1]}={shlex.quote(step[2])}")
        else:
            lines.append(f"cd {WORK_DIR} && {step[1]}")
    # ponytail: no per-step timeout; bench's build_timeout bounds the whole layer. Add one
    # (upstream's DOCKER_TIMEOUT is 300s) if a hung step ever becomes a real failure mode.
    lines += ["", "exit 0"]
    return "\n".join(lines) + "\n"


def render_dockerfile(repo: RepoSpec, steps: list, *,
                      base_image: str = DEFAULT_BASE_IMAGE) -> tuple:
    """Render the conforming Dockerfile + its COPY siblings. Returns ``(dockerfile, scripts)``."""
    # A newline inside an ENV value breaks the instruction (ENV has no line continuation). The
    # `export` in the replay script still carries it, so drop only the ENV line.
    env_lines = "".join(f"ENV {k}={shlex.quote(v)}\n"
                        for _, k, v in (s for s in steps if s[0] == "env")
                        if "\n" not in v)
    df = (
        f"FROM {base_image}\n"
        "RUN apt-get update \\\n"
        " && apt-get install -y --no-install-recommends git ca-certificates \\\n"
        " && rm -rf /var/lib/apt/lists/*\n"
        + _clone_lines(repo) +
        f"WORKDIR {WORK_DIR}\n"
        + env_lines +
        f"COPY {REPLAY_BASENAME} /tmp/{REPLAY_BASENAME}\n"
        f"RUN bash /tmp/{REPLAY_BASENAME}\n"
        # No pytest guard here: bench runs its own `_ENSURE` (`python -m pip install -q pytest
        # pytest-timeout`) before the gate, and it needs `python` on PATH — which is why the base
        # image is python:3.10 and not ubuntu.
        # bench compares `git -C /testbed rev-parse --show-toplevel` to "/testbed"
        # (bench/bench/contract.py): a symlinked /testbed resolves to its physical path and is
        # classified non_conforming. So /testbed is the real dir; the reverse symlink keeps
        # venv/editable-install paths the replay baked in resolving.
        f"RUN mv {WORK_DIR} /testbed && ln -sfn /testbed {WORK_DIR}\n"
        # The probe is `git -C /testbed rev-parse --show-toplevel`, which fails closed to
        # non_conforming when git refuses the directory as dubiously owned.
        "RUN git config --global --add safe.directory /testbed\n"
        # ponytail: two guessed venv names. The agent may create a venv anywhere; a non-existent
        # PATH entry is harmless. Parse the replay for the real path if this ever misses.
        "ENV PATH=/testbed/.venv/bin:/testbed/venv/bin:$PATH\n"
        "WORKDIR /testbed\n"
    )
    return df, {REPLAY_BASENAME: render_replay(steps)}
```

- [ ] **Step 4: Run the full test file**

Run: `python3 -m pytest producers/tests/test_setupx.py -v`
Expected: PASS — every test in the file.

- [ ] **Step 5: Commit**

```bash
git add producers/setupx.py producers/tests/test_setupx.py
git commit -m "feat(setupx): render the replay as a conforming Dockerfile homed at /testbed"
```

---

### Task 3: The live runner and the producer

Runs SetupX for one repo against DeepSeek direct with the XPU store attached, and wraps the transforms in the `Producer` protocol.

**Files:**
- Modify: `producers/setupx.py`
- Test: `producers/tests/test_setupx.py`

**Interfaces:**
- Consumes: `plan_replay`, `render_dockerfile` from Tasks 1-2.
- Produces:
  - `child_env(llm: str | None, base_image: str) -> dict` — the environment `python -m src.main` is launched with.
  - `mirror_image(repo: RepoSpec, ctx: ProduceContext) -> str` — builds and returns the tag of a per-repo base image holding a pinned clone at `/mirror`.
  - `run_setupx(repo, ctx, *, llm, num_turn) -> dict` with keys `history`, `completed`, `steps_taken`, `phase2`, `economy`.
  - `class SetupXProducer` with `name = "setupx"`, `needs_llm = True`, `measurable = True`, `conformance = "synthesized"`, `__init__(self, llm=None, num_turn=9999, runner=None)`.

- [ ] **Step 1: Write the failing tests**

Append to `producers/tests/test_setupx.py`:

```python
import json
import os

import pytest

from producers.base import ProduceContext
from producers.setupx import SetupXProducer, child_env


def _ctx(tmp_path):
    return ProduceContext(llm="deepseek/deepseek-v4-flash", workdir=str(tmp_path), num_turn=9999)


def test_child_env_routes_the_bare_model_name_at_deepseeks_own_endpoint(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    env = child_env("deepseek/deepseek-v4-flash", "setupx-mirror:o__r", "o__r_123", 100)
    assert env["LLM_PROVIDER"] == "openai"
    assert env["OPENAI_BASE_URL"] == "https://api.deepseek.com/v1"
    assert env["OPENAI_API_KEY"] == "sk-test"
    # SetupX POSTs {"model": OPENAI_MODEL}; the litellm-style provider prefix is not a model name.
    assert env["OPENAI_MODEL"] == "deepseek-v4-flash"


def test_child_env_refuses_to_start_without_a_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        child_env("deepseek/deepseek-v4-flash", "setupx-mirror:o__r", "o__r_123", 100)


def test_child_env_freezes_the_experience_store(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("SETUPX_DB_DSN", "postgresql://postgres@localhost:5433/xpu_run")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-embed")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
    env = child_env("deepseek/deepseek-v4-flash", "setupx-mirror:o__r", "o__r_123", 100)
    assert env["FREEZE_TELEMETRY"] == "1"
    assert env["XPU_VECTOR_ENABLED"] == "1"
    # config.py reads `dns` first, XPU_DB_DNS second; set both so neither spelling wins by accident.
    assert env["dns"] == env["XPU_DB_DNS"] == "postgresql://postgres@localhost:5433/xpu_run"


def test_child_env_sends_embeddings_somewhere_other_than_deepseek(monkeypatch):
    # text_to_embedding falls back to OPENAI_API_KEY + OPENAI_BASE_URL, which now point at
    # DeepSeek — and DeepSeek serves no /embeddings route. An unset EMBEDDING_API_KEY would 404
    # every retrieval and silently turn the XPU arm into the no-XPU arm.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("SETUPX_DB_DSN", "postgresql://postgres@localhost:5433/xpu_run")
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="EMBEDDING_API_KEY"):
        child_env("deepseek/deepseek-v4-flash", "setupx-mirror:o__r", "o__r_123", 100)


def test_child_env_namespaces_the_checkpoint_images_per_run(monkeypatch):
    # Without this, two concurrent repos share the `setup_agent_checkpoint` repository: each one's
    # startup deletes the other's snapshots and their step_<n>_pre_xpu tags collide, so a rollback
    # restores the wrong repo's container. Requires tools/setupx-bench.patch.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    env = child_env("deepseek/deepseek-v4-flash", "setupx-mirror:o__r", "o__r_123", 100)
    assert env["SETUPX_CKPT_NS"] == "o__r_123"
    assert env["SETUPX_NETWORK_MODE"] == "bridge"


def test_child_env_passes_the_budget_as_llm_calls_not_steps(monkeypatch):
    # num_turn is a completion budget, matching sweagent_repo2run's per_instance_call_limit. It must
    # NOT be forwarded as --max-steps: a single VERIFY step spawns the verifier's whole ReAct loop,
    # so steps and calls differ by an unbounded factor and would not be comparable across arms.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    env = child_env("deepseek/deepseek-v4-flash", "setupx-mirror:o__r", "o__r_123", 100)
    assert env["SETUPX_MAX_LLM_CALLS"] == "100"


def test_child_env_marks_the_store_read_only_when_a_dsn_is_present(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("SETUPX_DB_DSN", "postgresql://postgres@localhost:5433/xpu_run")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-embed")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
    env = child_env("deepseek/deepseek-v4-flash", "setupx-mirror:o__r", "o__r_123", 100)
    assert env["XPU_READONLY"] == "1"


def test_child_env_points_setupx_at_the_pinned_mirror(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    env = child_env("deepseek/deepseek-v4-flash", "setupx-mirror:o__r", "o__r_123", 100)
    assert env["DOCKER_BASE_IMAGE"] == "setupx-mirror:o__r"
    assert env["DOCKER_WORK_DIR"] == "/workspace"


def test_producer_emits_a_packet_from_a_stubbed_run(tmp_path):
    def stub(repo, ctx, *, llm, num_turn):
        return {"history": [{"action": {"action_type": "SHELL_COMMAND",
                                        "content": {"command": "pip install -e ."}},
                             "result": {"exit_code": 0, "stdout": "", "stderr": ""}}],
                "completed": True, "steps_taken": 7,
                "phase2": {"success": True, "reason": "prosecutor found no substantive issue"},
                "economy": {"turns_used": 7}}

    env = SetupXProducer(llm="deepseek/deepseek-v4-flash", runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.status == "produced"
    assert env.conformance == "synthesized"
    assert env.producer_name == "setupx"
    assert env.head_sha == "abc123"
    assert env.base_image == "python:3.10"
    assert "pip install -e ." in env.setup_scripts[REPLAY_BASENAME]
    assert env.economy["turns_used"] == 7
    assert "produce_s" in env.economy
    assert env.unreplayed is False


def test_producer_still_emits_for_an_unfinished_phase_one(tmp_path):
    # A guilty verdict or an exhausted step budget still leaves a real environment. Scoring it is
    # more honest than recording status="error" and hiding the row. (Reachable only via step
    # exhaustion — a wall-clock timeout writes no report for the producer to read.)
    def stub(repo, ctx, *, llm, num_turn):
        return {"history": [{"action": {"action_type": "SHELL_COMMAND",
                                        "content": {"command": "pip install -e ."}},
                             "result": {"exit_code": 0, "stdout": "", "stderr": ""}}],
                "completed": False, "steps_taken": 40,
                "phase2": {"success": False, "reason": "setup agent timed out"},
                "economy": {}}

    env = SetupXProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.status == "produced"
    assert "timed out" in env.note


def test_producer_reports_an_empty_trajectory_as_an_error_not_an_empty_image(tmp_path):
    def stub(repo, ctx, *, llm, num_turn):
        return {"history": [], "completed": False, "steps_taken": 0,
                "phase2": {"success": None, "reason": "[error] phase 2 execution failed"},
                "economy": {}}

    env = SetupXProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.status == "error"
    assert env.dockerfile is None


def test_producer_never_raises_when_the_runner_blows_up(tmp_path):
    def stub(repo, ctx, *, llm, num_turn):
        raise RuntimeError("docker daemon is not running")

    env = SetupXProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.status == "error"
    assert "docker daemon" in env.note
    assert env.dockerfile is None


def test_the_written_packet_matches_the_shared_contract(tmp_path):
    def stub(repo, ctx, *, llm, num_turn):
        return {"history": [{"action": {"action_type": "SHELL_COMMAND",
                                        "content": {"command": "pip install -e ."}},
                             "result": {"exit_code": 0, "stdout": "", "stderr": ""}}],
                "completed": True, "steps_taken": 3, "phase2": {"success": True, "reason": "ok"},
                "economy": {}}

    from producers.base import write_env_packet
    out = tmp_path / "out"
    env = SetupXProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    write_env_packet(str(out), env)
    build = out / "o" / "r" / "eval_build"
    assert (build / "Dockerfile").exists()
    assert (build / REPLAY_BASENAME).exists()
    meta = json.loads((out / "o" / "r" / "_meta.json").read_text())
    assert meta["producer"] == "setupx"
    assert meta["status"] == "produced"
    assert meta["conformance"] == "synthesized"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest producers/tests/test_setupx.py -v`
Expected: FAIL with `ImportError: cannot import name 'SetupXProducer'`.

- [ ] **Step 3: Write the minimal implementation**

Add these imports at the top of `producers/setupx.py`:

```python
import glob
import json
import os
import subprocess
import time
```

and append:

```python
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
# A BACKSTOP, not the intended bound. main.py's Phase1Timeout handler returns before Stage 3, so a
# wall-clock timeout writes no report and the run is lost entirely; the step budget (--max-steps,
# from varieties.toml) is what should end a run, because step exhaustion still reports.
DEFAULT_PHASE1_TIMEOUT = 3600


def _setupx_root(ctx: ProduceContext | None = None) -> str:
    # ctx.agent_root first, matching producers/executionagent.py; benchmark.py maps SETUPX_ROOT
    # into it. Falling back to the env var keeps the producer usable standalone.
    root = (ctx.agent_root if ctx else None) or os.environ.get("SETUPX_ROOT")
    if not root or not os.path.isfile(os.path.join(root, "src", "main.py")):
        raise RuntimeError(
            "cannot locate the SetupX checkout (src/main.py). Set SETUPX_ROOT to a clone of "
            "https://github.com/OpenDataBox/SetupX — see README, 'SetupX setup'.")
    # src/config.py runs load_dotenv(..., override=True) at import, so a dotenv file in the
    # checkout SILENTLY overrides the environment we pass in — including the model and the DSN.
    # Fail loudly rather than pay for a run that quietly used the wrong endpoint.
    for name in (".env", ".env.local"):
        if os.path.isfile(os.path.join(root, name)):
            raise RuntimeError(
                f"{os.path.join(root, name)} exists. SetupX loads it with override=True, which "
                "would silently replace the model/endpoint/DSN this producer passes in. Remove it "
                "— the producer supplies the whole environment.")
    return root


def child_env(llm: str | None, base_image: str, ckpt_ns: str, max_llm_calls: int) -> dict:
    """The environment `python -m src.main` runs under. Validated here, at the boundary.

    `ckpt_ns` namespaces SetupX's checkpoint images so concurrent repos cannot delete or overwrite
    each other's snapshots, and gives the caller a handle to sweep the leftovers with.
    `max_llm_calls` is the arm's budget in COMPLETIONS, not agent steps: SetupX's own --max-steps
    counts steps, and one step can cost many completions. Both switches come from
    tools/setupx-bench.patch.
    """
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set; the setupx variety routes through "
                           "DeepSeek's own API (see README, 'SetupX setup').")
    # `deepseek/deepseek-v4-flash` is a litellm-style slug; SetupX POSTs the value verbatim as
    # {"model": ...} to an OpenAI-compatible endpoint, which wants the bare name.
    model = (llm or "deepseek/deepseek-v4-flash").split("/", 1)[-1]
    env = dict(os.environ,
               LLM_PROVIDER="openai",
               OPENAI_API_KEY=key,
               OPENAI_BASE_URL=os.environ.get("SETUPX_BASE_URL", DEEPSEEK_BASE_URL),
               OPENAI_MODEL=model,
               DOCKER_BASE_IMAGE=base_image,
               DOCKER_WORK_DIR="/workspace",
               SETUPX_CKPT_NS=ckpt_ns,
               # num_turn means LLM CALLS here, matching sweagent_repo2run's per_instance_call_limit
               # and claudecode's cap. SetupX's own --max-steps counts agent STEPS, and one step can
               # cost many completions (a VERIFY step runs the verifier's whole ReAct sub-loop;
               # phase 2 adds the prosecutor and judge), so steps are not a comparable budget.
               SETUPX_MAX_LLM_CALLS=str(max_llm_calls),
               # bridge networking: host networking shares the host port space, so two concurrent
               # repos collide the moment either binds a port.
               SETUPX_NETWORK_MODE=os.environ.get("SETUPX_NETWORK_MODE", "bridge"))
    dsn = os.environ.get("SETUPX_DB_DSN")
    if dsn:
        # All THREE, not just the key. text_to_embedding takes the dedicated branch as soon as
        # EMBEDDING_API_KEY is set, and then passes base_url=None straight through — sending an
        # OpenRouter key to api.openai.com. VectorXPUClient.query swallows the 401 and returns [],
        # so the arm would silently degrade to no-XPU, which is exactly what this guard exists to
        # prevent. (The OPENAI_* fallback is no better: those now point at DeepSeek, which serves
        # no /embeddings route at all.)
        missing = [k for k in ("EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL")
                   if not os.environ.get(k)]
        if missing:
            raise RuntimeError(
                f"{', '.join(missing)} not set, but SETUPX_DB_DSN is — XPU retrieval embeds every "
                "query and would fail silently, leaving an arm that reports as XPU-on while "
                "retrieving nothing. See README, 'SetupX setup'.")
        env.update(XPU_ENABLED="1", XPU_VECTOR_ENABLED="1", dns=dsn, XPU_DB_DNS=dsn,
                   FREEZE_TELEMETRY="1",     # native switch: skips every telemetry write
                   XPU_READONLY="1")         # patched-in switch: skips _store_xpu_experience, so
                                             # concurrent repos cannot race on dedup_and_store and
                                             # results do not depend on repo order
    return env


def mirror_image(repo: RepoSpec, ctx: ProduceContext) -> str:
    """Build a per-repo base image holding the repo pinned at /mirror, and return its tag.

    SetupX clones `git clone --depth=1 <repo_url> /workspace/repo` at the live default-branch HEAD
    — it has no notion of a dataset SHA. Handing it a local path as the "repo url" makes its own
    clone land the pinned tree instead, so the agent works on the same commit the emitted
    Dockerfile builds, with no patch to the SetupX checkout.
    """
    tag = "setupx-mirror:" + repo.full_name.replace("/", "__").lower()
    ctx_dir = os.path.join(ctx.workdir, "mirror", repo.full_name)
    os.makedirs(ctx_dir, exist_ok=True)
    clone = (f"RUN git clone --depth=1 {repo.repo_url} /mirror\n" if not repo.commit else
             f"RUN git clone {repo.repo_url} /mirror \\\n"
             f" && git -C /mirror fetch --depth 1 origin {repo.commit} \\\n"
             # a branch, not a detached HEAD: cloning FROM a detached-HEAD repo checks out its
             # default branch, which would put the agent back on an unpinned tree.
             f" && git -C /mirror checkout -B pinned {repo.commit}\n")
    with open(os.path.join(ctx_dir, "Dockerfile"), "w", encoding="utf-8") as fh:
        fh.write(f"FROM {DEFAULT_BASE_IMAGE}\n"
                 "RUN apt-get update \\\n"
                 " && apt-get install -y --no-install-recommends git ca-certificates \\\n"
                 " && rm -rf /var/lib/apt/lists/*\n"
                 + clone)
    try:
        subprocess.run(["docker", "build", "-t", tag, ctx_dir],
                       check=True, capture_output=True, text=True, timeout=1800)
    except subprocess.CalledProcessError as exc:
        # CalledProcessError's repr() — which produce() stores as `note` — carries only the rc and
        # argv, so every mirror failure would look identical. Surface the build's own stderr.
        raise RuntimeError(f"mirror build failed for {repo.full_name}: "
                           f"{(exc.stderr or '')[-2000:]}") from exc
    return tag


def _sweep_checkpoints(ckpt_ns: str) -> None:
    """Remove the checkpoint images this run leaked.

    `agent.run()` calls `cleanup_snapshots()` on the happy path, but a crash or a phase-1 timeout
    skips it — and each snapshot is a full `docker commit` of the container, so a 50-repo run would
    otherwise leave dozens of multi-GB images behind. Never raises: this is housekeeping, not the run.
    """
    try:
        out = subprocess.run(["docker", "images", "-q", f"setup_agent_checkpoint_{ckpt_ns}"],
                             capture_output=True, text=True, timeout=60)
        ids = sorted(set((out.stdout or "").split()))
        if ids:
            subprocess.run(["docker", "rmi", "-f", *ids], capture_output=True, timeout=300)
    except Exception:                        # noqa: BLE001 — housekeeping must never fail a run
        pass


def run_setupx(repo: RepoSpec, ctx: ProduceContext, *, llm: str | None, num_turn: int) -> dict:
    """LIVE-ONLY: run SetupX for one repo and return its report. Raises on failure; the producer
    wraps it for the anti-vanish invariant. Never exercised by unit tests (they inject a stub) —
    it needs docker, keys, and the SetupX checkout."""
    root = _setupx_root(ctx)
    python = os.environ.get("SETUPX_PYTHON") or os.path.join(root, ".venv", "bin", "python")
    if not os.path.isfile(python):
        raise RuntimeError(f"cannot run {python!r}: SetupX needs its own venv with "
                           "requirements.txt installed. Point SETUPX_PYTHON at it — see README, "
                           "'SetupX setup'.")
    out_dir = os.path.join(ctx.workdir, "output", repo.full_name, "setupx_log")
    os.makedirs(out_dir, exist_ok=True)
    phase1 = int(os.environ.get("SETUPX_PHASE1_TIMEOUT", DEFAULT_PHASE1_TIMEOUT))

    # The positional argument is the REPO URL. `/mirror` is the pinned clone baked into
    # base_image, so SetupX's own `git clone --depth=1 /mirror /workspace/repo` lands the dataset
    # SHA. (git warns "--depth is ignored in local clones"; harmless.) One consequence: SetupX
    # names its report after the url basename, so every report here is `mirror_result.json` — the
    # glob below handles that, and out_dir is per-repo, so there is no collision.
    base_image = mirror_image(repo, ctx)
    ckpt_ns = f"{repo.full_name.replace('/', '__').lower()}_{os.getpid()}"
    start = time.time()
    try:
        proc = subprocess.run(
            [python, "-m", "src.main", "/mirror",
             # Steps are deliberately unbounded: the budget is SETUPX_MAX_LLM_CALLS, and every step
             # costs at least one completion, so the call cap binds first and binds comparably.
             "--max-steps", "9999",
             "--phase1-timeout", str(phase1),
             "--output-dir", out_dir],
            cwd=root, env=child_env(llm, base_image, ckpt_ns, num_turn),
            capture_output=True, text=True, timeout=ctx.timeout)
    finally:
        _sweep_checkpoints(ckpt_ns)
    try:
        with open(os.path.join(out_dir, "setupx_stdout.txt"), "w", encoding="utf-8") as fh:
            fh.write((proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""))
    except OSError:
        pass                                     # telemetry must never fail a paid-for run

    reports = sorted(glob.glob(os.path.join(out_dir, "*_result.json")))
    if not reports:
        raise RuntimeError(f"SetupX wrote no *_result.json (rc={proc.returncode}); "
                           "see setupx_stdout.txt")
    with open(reports[-1], encoding="utf-8") as fh:
        report = json.load(fh)
    setup = report.get("setup") or {}
    return {"history": setup.get("history") or [],
            "completed": bool(setup.get("completed")),
            "steps_taken": setup.get("steps_taken"),
            "phase2": report.get("phase2") or {},
            # SetupX discards the API `usage` block, so there are no token counts to harvest.
            # llm_calls comes from the patched main.py, which reports llm_engine.llm_calls_used().
            # Stock SetupX discards the API `usage` block entirely, so tokens/cost stay None until
            # the metering ledger lands (see the note in Task 3's header).
            "economy": {"turns_used": setup.get("steps_taken"),
                        "llm_calls": report.get("llm_calls"),
                        "produce_s": round(time.time() - start, 2)}}


class SetupXProducer:
    """SetupX (XPU + speculative execution + prosecutor/judge), replayed into one image at /testbed."""
    name = "setupx"
    needs_llm = True
    measurable = True
    conformance = "synthesized"

    def __init__(self, llm: str | None = None, num_turn: int = 9999, runner=None):
        self.llm = llm
        self.num_turn = num_turn
        self._runner = runner   # injectable for tests; None => the real live runner

    def produce(self, repo: RepoSpec, ctx: ProduceContext) -> ProducedEnv:
        # Anti-vanish invariant (design §1): a SetupX crash yields ProducedEnv(status="error"),
        # never a raised exception.
        start = time.time()
        try:
            runner = self._runner or run_setupx
            num_turn = ctx.num_turn if ctx.num_turn is not None else self.num_turn
            res = runner(repo, ctx, llm=(ctx.llm or self.llm), num_turn=num_turn)

            economy = dict(res.get("economy") or {})
            economy.setdefault("produce_s", round(time.time() - start, 2))
            steps, lossy = plan_replay(res.get("history"))
            phase2 = res.get("phase2") or {}
            note = f"phase2={phase2.get('success')}: {(phase2.get('reason') or '')[:200]}"
            if not res.get("completed"):
                note = "phase1 incomplete; " + note
            if lossy:
                note += "; XPU trial commands not recorded (atom-rendered fallback)"

            if not steps:
                return ProducedEnv(repo=repo, dockerfile=None, status="error",
                                   note="no replayable steps survived; " + note,
                                   conformance=self.conformance, producer_name=self.name,
                                   economy=economy)

            dockerfile, scripts = render_dockerfile(repo, steps)
            return ProducedEnv(repo=repo, dockerfile=dockerfile, setup_scripts=scripts,
                               base_image=DEFAULT_BASE_IMAGE, head_sha=repo.commit or "",
                               status="produced", conformance=self.conformance,
                               unreplayed=lossy, producer_name=self.name,
                               economy=economy, note=note)
        except Exception as exc:                # noqa: BLE001 — boundary guard, never propagate
            return ProducedEnv(repo=repo, dockerfile=None, status="error", note=repr(exc),
                               conformance=self.conformance, producer_name=self.name,
                               economy={"produce_s": round(time.time() - start, 2)})
```

- [ ] **Step 4: Run the full test file**

Run: `python3 -m pytest producers/tests/test_setupx.py -v`
Expected: PASS — every test in the file.

- [ ] **Step 5: Commit**

```bash
git add producers/setupx.py producers/tests/test_setupx.py
git commit -m "feat(setupx): live runner on DeepSeek direct + pinned mirror image + producer"
```

---

### Task 4: Wire it into the registry, the runner, and the variety table

**Files:**
- Modify: `producers/__init__.py`
- Modify: `runner/benchmark.py:112-113` (`_PRODUCE_ABLE`), `runner/benchmark.py:148-158` (kwargs + `agent_root`)
- Modify: `varieties.toml`
- Test: `producers/tests/test_setupx.py`

**Interfaces:**
- Consumes: `SetupXProducer` from Task 3.
- Produces: the registry key `"setupx"`, resolvable via `producers.get("setupx", llm=..., num_turn=...)` and `resolve_variety(registry, "setupx")`.

- [ ] **Step 1: Write the failing tests**

Append to `producers/tests/test_setupx.py`:

```python
def test_setupx_is_registered_and_constructible():
    import producers
    assert "setupx" in producers.PRODUCERS
    prod = producers.get("setupx", llm="deepseek/deepseek-v4-flash", num_turn=9999)
    assert prod.name == "setupx"
    assert prod.measurable is True
    assert prod.needs_llm is True


def test_the_runner_treats_setupx_as_produce_able():
    # Not derived from the registry — the set is hardcoded, so a producer missing from it is
    # silently routed to _make_model's else-branch and raises "Unknown model name".
    from runner.benchmark import _PRODUCE_ABLE
    assert "setupx" in _PRODUCE_ABLE


def test_the_variety_resolves_to_the_rehome_lane_with_an_explicit_budget():
    import os
    from runner.registry import load_registry, resolve_variety
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = resolve_variety(load_registry(os.path.join(root, "varieties.toml")), "setupx")
    assert spec.model == "setupx"
    assert spec.measure == "rehome"
    assert spec.llm == "deepseek/deepseek-v4-flash"
    # 100 LLM CALLS (SETUPX_MAX_LLM_CALLS), not agent steps — the same unit and value as
    # sweagent_repo2run's per_instance_call_limit, so the budgets are comparable.
    assert spec.num_turn == 100
    assert spec.is_baseline is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest producers/tests/test_setupx.py -v -k 'registered or produce_able or variety_resolves'`
Expected: FAIL — `KeyError: 'setupx'` on the registry, `'setupx' not in _PRODUCE_ABLE`, `KeyError: unknown variety 'setupx'`.

- [ ] **Step 3: Register the producer**

In `producers/__init__.py`, after the `sweagent_repo2run` import line:

```python
from producers.setupx import SetupXProducer  # noqa: E402
```

after `register(SweAgentRepo2RunProducer)`:

```python
register(SetupXProducer)
```

and add `"SetupXProducer"` to `__all__`.

- [ ] **Step 4: Wire the runner**

In `runner/benchmark.py`, extend `_PRODUCE_ABLE`:

```python
_PRODUCE_ABLE = {"dockeragent", "repo2run", "claudecode-dockerfile", "executionagent",
                 "sweagent_repo2run", "setupx"}
```

extend the kwargs branch in `_ProducerModel.predict`:

```python
        elif self.name in ("repo2run", "executionagent", "sweagent_repo2run", "setupx"):
            kw.update(num_turn=self.num_turn)
```

and extend the `agent_root` selection just below it:

```python
        # The checkout that owns the agent: DOCKERAGENT_ROOT for our branches (set by run.py),
        # EXECUTIONAGENT_ROOT / SETUPX_ROOT for the vendored-elsewhere baseline clones.
        _ROOT_VAR = {"executionagent": "EXECUTIONAGENT_ROOT", "setupx": "SETUPX_ROOT"}
        agent_root = os.environ.get(_ROOT_VAR.get(self.name, "DOCKERAGENT_ROOT"))
```

and add `setupx` to the `_make_model` docstring's produce-able list and to the `ValueError` message's choices.

- [ ] **Step 5: Add the variety**

Append to `varieties.toml`:

```toml
# SetupX (OpenDataBox/SetupX): XPU experience store + speculative execution (docker-commit
# snapshots with rollback) + a prosecutor/judge trial that re-checks the agent's own success claim.
# It emits NO Dockerfile — producers/setupx.py replays the surviving `setup.history` onto a pinned
# clone and re-homes /workspace/repo -> /testbed, so the arm gets a real EBSR/ESSR row.
# Needs a checkout at $SETUPX_ROOT and a warm XPU store; see README, "SetupX setup".
[variety.setupx]
model = "setupx"
# DeepSeek's OWN API. SetupX speaks raw OpenAI-compatible HTTP (not litellm), so the producer
# strips the `deepseek/` prefix and passes the bare model name as OPENAI_MODEL. Reads
# DEEPSEEK_API_KEY, base https://api.deepseek.com/v1.
llm   = "deepseek/deepseek-v4-flash"
# LLM CALLS, not agent steps — forwarded as SETUPX_MAX_LLM_CALLS by the producer, enforced at
# LLMClientBase.chat by tools/setupx-bench.patch. SetupX's own --max-steps counts steps, and one
# step can cost many completions (a VERIFY step runs the verifier's whole sub-loop; phase 2 adds
# the prosecutor and judge), so steps are not comparable with the other arms' budgets. 100 matches
# sweagent_repo2run's per_instance_call_limit and claudecode's cap.
num_turn = 100
measure = "rehome"
```

- [ ] **Step 6: Run the whole producer suite to check nothing regressed**

Run: `python3 -m pytest producers/tests/ -v`
Expected: PASS — the new tests plus every pre-existing one.

- [ ] **Step 7: Commit**

```bash
git add producers/__init__.py runner/benchmark.py varieties.toml producers/tests/test_setupx.py
git commit -m "feat(setupx): register the producer and add the setupx variety"
```

---

### Task 5: Provision the checkout, the warm XPU store, and document it

No new code — this is the operator-facing half, and the arm cannot run without it.

**Files:**
- Modify: `README.md` (new "SetupX setup" section after "ExecutionAgent setup"; new row in the varieties table)
- Modify: `.env.example`

- [ ] **Step 1: Clone and provision SetupX**

```bash
git clone https://github.com/OpenDataBox/SetupX ~/agents/SetupX
# Pin the baseline. A benchmark arm that tracks someone else's default branch is not reproducible,
# and tools/setupx-bench.patch is cut against exactly this commit.
git -C ~/agents/SetupX checkout 85de35515c45954b72afb9678dfd172855bfb847
git -C ~/agents/SetupX apply /Users/john/ratbench-runner-john/tools/setupx-bench.patch
python3 -m venv ~/agents/SetupX/.venv
~/agents/SetupX/.venv/bin/pip install -r ~/agents/SetupX/requirements.txt
export SETUPX_ROOT=~/agents/SetupX

# What the patch changes (three things, all required for concurrent runs):
#  1. environment_manager.py: checkpoint images move from the single global
#     `setup_agent_checkpoint` repository to `setup_agent_checkpoint_$SETUPX_CKPT_NS`. As shipped,
#     every EnvironmentManager deletes ALL images in that repository on construction (:74) and tags
#     them `initial_clone` / `step_<n>_pre_xpu` (:430), so concurrent repos delete and overwrite
#     each other's snapshots and a rollback restores the wrong container.
#  2. environment_manager.py: `network_mode` becomes $SETUPX_NETWORK_MODE (default unchanged,
#     "host"). The producer sets "bridge", because host networking shares the host port space and
#     two concurrent repos collide as soon as either binds one.
#  3. main.py: `_store_xpu_experience` returns early when XPU_READONLY=1, so the shared store is
#     never written — no dedup_and_store race between concurrent repos, no dependence on repo
#     order, and one fewer extractor LLM call per repo.
#  4. llm_engine.py: a per-run completion counter at LLMClientBase.chat, capped by
#     $SETUPX_MAX_LLM_CALLS (0/unset = unlimited = stock). Raises LLMCallLimit, a BaseException,
#     because the retriever/verifier/prosecutor/judge all wrap chat() in `except Exception` and
#     would otherwise swallow the limit. Also exposes llm_calls_used() so the report can carry a
#     real llm_calls figure — stock SetupX discards the API usage block entirely.
#  5. main.py: Stage 3 is now reachable after a phase-1 abort. The stock handler `return 1`s on
#     Phase1Timeout BEFORE writing *_result.json, so a run that hit its wall clock produced no
#     report and was lost — the exact failure a replay-based harness cannot tolerate. The handler
#     now synthesizes a partial SetupResult (completed=False) and falls through to the report.
# Verify it took:
grep -q "_ckpt_repo" ~/agents/SetupX/src/environment_manager.py \
  && grep -q "SETUPX_MAX_LLM_CALLS" ~/agents/SetupX/src/llm_engine.py \
  && echo "patch applied"

# Do NOT create ~/agents/SetupX/.env — src/config.py loads it with override=True and would
# silently replace the model, endpoint and DSN the producer passes in. The producer refuses to
# start if it exists.
```

- [ ] **Step 2: Confirm the embeddings endpoint answers before spending anything**

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://openrouter.ai/api/v1/embeddings \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" -H 'content-type: application/json' \
  -d '{"model":"openai/text-embedding-3-small","input":"probe"}'
# expect: 200
```

OpenRouter's embedding-models collection lists `openai/text-embedding-3-small` at $0.02/M, but this
is the one premise of the XPU arm that cannot be checked against either repo's source, and a 404
here would waste 600 embedding calls in Step 3 and then return `[]` from every query at run time.
If it is not 200, switch `EMBEDDING_BASE_URL`/`EMBEDDING_MODEL` to a vendor that does serve
embeddings (OpenAI direct, Voyage, Cohere) and keep `EMBEDDING_DIM` matched to the model.

- [ ] **Step 3: Stand up pgvector and load the warm store as a TEMPLATE database**

```bash
docker run -d --name setupx-xpu -p 5433:5432 \
  -e POSTGRES_PASSWORD=setupx -e POSTGRES_DB=xpu_warm pgvector/pgvector:pg16
sleep 5 && docker exec setupx-xpu psql -U postgres -d xpu_warm \
  -c 'CREATE EXTENSION IF NOT EXISTS vector;'

# Import the 600 shipped entries. The JSONL carries no vectors — the importer embeds every entry,
# so this costs one embeddings call per entry and must use the same model the runs will query with.
cd ~/agents/SetupX
EMBEDDING_API_KEY=$OPENROUTER_API_KEY \
  EMBEDDING_BASE_URL=https://openrouter.ai/api/v1 \
  EMBEDDING_MODEL=openai/text-embedding-3-small EMBEDDING_DIM=1536 \
  dns=postgresql://postgres:setupx@localhost:5433/xpu_warm \
  .venv/bin/python scripts/import_xpu_jsonl.py data/xpu_warm.jsonl --clear

# Measured: 600 entries, ~162k tokens of embeddable text (the advice_nl is CJK-heavy) => ~$0.003
# once, at $0.02/M. Query side is one embedding per agent step, ~$0.01 per 50-repo run.
```

`xpu_warm` is now the immutable master. Nothing ever runs against it again.

- [ ] **Step 4: Take a fresh copy of the store before each benchmark run**

```bash
docker exec setupx-xpu psql -U postgres -d postgres -c 'DROP DATABASE IF EXISTS xpu_run;'
docker exec setupx-xpu psql -U postgres -d postgres \
  -c 'CREATE DATABASE xpu_run TEMPLATE xpu_warm;'
export SETUPX_DB_DSN=postgresql://postgres:setupx@localhost:5433/xpu_run
```

**Why a template copy and not a read-only role.** A SELECT-only role does not work: `XpuVectorStore.__init__`
calls `self._ensure_table()` → `create_xpu_table` (CREATE EXTENSION / CREATE TABLE / CREATE INDEX)
on every connect, and `src/xpu_client.py:237` constructs it with no `try`. A role that cannot run
that DDL raises out of `create_xpu_client()` → `SpeculativeSetupAgent.__init__` → `main()`, which
means **every repo dies before Stage 1** and produces no report. `CREATE DATABASE ... TEMPLATE` is
a Postgres file-level copy: instant, no re-embedding, no ACL guesswork, and it gives exactly the
requested semantics — every benchmark run starts from the same 600 entries and nothing survives
into the next run. Re-run this step before each run; the copy requires no live connections to
`xpu_warm`.

What this does *not* isolate is writes *within* one run: `_store_xpu_experience` still appends what
each repo learned, so a later repo can retrieve an earlier one's extraction. `FREEZE_TELEMETRY=1`
(set by `child_env`) keeps the telemetry counters frozen regardless. If you want per-repo
independence too, take the copy per repo instead of per run, or patch out `_store_xpu_experience`.

- [ ] **Step 5: Verify the copy is loaded and independent**

```bash
docker exec setupx-xpu psql -U postgres -d xpu_run -c 'SELECT count(*) FROM xpu_entries;'
# expect: 600

# and the master is untouched no matter what a run does:
docker exec setupx-xpu psql -U postgres -d xpu_warm -c 'SELECT count(*) FROM xpu_entries;'
# expect: 600
```

- [ ] **Step 6: Add the env keys**

Append to `.env.example`:

```
#
# DeepSeek key — used by `sweagent_repo2run` and `setupx`, which route through DeepSeek's own API
# rather than OpenRouter:
DEEPSEEK_API_KEY=sk-...
#
# `setupx` only. The XPU experience store (pgvector) and the embeddings endpoint that queries it.
# DeepSeek serves no /embeddings route, so EMBEDDING_API_KEY must point somewhere else; leaving it
# unset would 404 every retrieval and silently turn the XPU arm into a no-XPU arm, so the producer
# refuses to start without it. EMBEDDING_DIM must match the model AND the dimension the store was
# imported at (1536 = text-embedding-3-small).
SETUPX_DB_DSN=postgresql://postgres:setupx@localhost:5433/xpu_run
EMBEDDING_API_KEY=sk-or-...                        # same OpenRouter key the sweagent arms use
EMBEDDING_BASE_URL=https://openrouter.ai/api/v1
EMBEDDING_MODEL=openai/text-embedding-3-small      # 1536 dims, $0.02/M
EMBEDDING_DIM=1536
```

- [ ] **Step 7: Document the variety**

Add to the varieties table in `README.md`:

```
| `setupx` | `setupx` | SetupX baseline (XPU store + speculative execution + prosecutor/judge; trajectory replayed and re-homed to `/testbed`) |
```

and a `### SetupX setup` section after `### ExecutionAgent setup` containing Steps 1-4 above plus these notes:

```
**What is being measured.** SetupX emits no Dockerfile — it mutates a live container and writes a
JSON report. `producers/setupx.py` replays the surviving `setup.history` (SetupX rolls back to
`docker commit` checkpoints, so undone work is dropped rather than replayed) onto a clone pinned at
the dataset SHA, then re-homes `/workspace/repo` to `/testbed`. That makes the row
`conformance="synthesized"`, not `native` — the same status `executionagent` carries.

**Two fidelity limits, both surfaced in `_meta.json`:**
- Every phase-2 agent runs commands against the same live container — `VerifierAgent`
  (`verifier_agent.py:172`, plus a `write_file` action at `:211`), `ProsecutorAgent` (`:300`) and
  `JudgeAgent` (`:191`). None of it lands in `history`, so anything they install or write is in the
  container the agent finished with but is not replayed.
- A successful XPU trial that fell back to atom-rendered `suggestion.commands` records no command
  in the trajectory. Those runs are flagged `unreplayed=true`.

**Commit pinning.** SetupX clones `--depth=1` at the live default-branch HEAD and has no notion of
a dataset SHA. Rather than patch the checkout, the producer builds a per-repo base image holding
the repo pinned at `/mirror` and hands SetupX `/mirror` as the repo URL, so its own clone lands the
pinned tree. `git` warns `--depth is ignored in local clones`; that is expected.

**No token accounting.** `src/llm_engine.py` discards the API `usage` block, so `_meta.json`
carries `turns_used` and `produce_s` but not `tokens_in` / `tokens_out` / `cost_usd`.

**Sweep the docker image store between runs.** Measured on the first smoke run: a single
`initial_clone` checkpoint is **2.07 GB**, and SetupX commits another before every XPU trial.
`_sweep_checkpoints` clears them on the normal and crashed paths, but a SIGKILL bypasses it — an
interrupted 50-repo run can leave tens of gigabytes behind. Run
`docker images --filter 'reference=setup_agent_checkpoint*' -q | xargs -r docker rmi -f` and
`docker image prune` after an interrupted run; the per-repo `setupx-mirror:*` images accumulate too.

**The XPU store never accumulates across runs.** Each run gets a `CREATE DATABASE ... TEMPLATE`
copy of the immutable 600-entry `xpu_warm`, and `FREEZE_TELEMETRY=1` freezes the telemetry counters
on top of that. A SELECT-only role is NOT a workable alternative: `XpuVectorStore.__init__` runs
CREATE TABLE/INDEX DDL on every connect, unguarded, so a role without those rights kills every run
before Stage 1.
```

- [ ] **Step 8: Commit**

```bash
git add README.md .env.example
git commit -m "docs(setupx): setup, warm-store provisioning, and what the arm measures"
```

---

### Task 6: End-to-end smoke run

The first task that spends money and needs docker. Everything before this is offline.

**Files:** none — this is a verification gate.

- [ ] **Step 1: Confirm the environment**

```bash
source env.sh
export SETUPX_ROOT=~/agents/SetupX
export SETUPX_DB_DSN=postgresql://postgres:setupx@localhost:5433/xpu_run   # from Task 5 Step 4
docker info >/dev/null && echo "docker ok"
test ! -e "$SETUPX_ROOT/.env" && echo "no dotenv shadow ok"
grep -c . <<<"$DEEPSEEK_API_KEY$SETUPX_DB_DSN$EMBEDDING_API_KEY" >/dev/null && echo "keys set"
```

- [ ] **Step 2: Run one repo**

`bruin-data/ingestr` is the lightest repo in `rat_python50.json` at 19 tests, which makes it the
cheapest end-to-end signal. Do **not** use `psf/requests` — it is not in the dataset, so `--only`
would match nothing and the run would exit having measured zero repos.

```bash
./run_bench.sh setupx --repos-json datasets/rat_python50.json --only bruin-data/ingestr --tier all
```

- [ ] **Step 3: Check the produce side**

```bash
RUN=$(ls -td runs/setupx/* | head -1)
cat "$RUN/output/bruin-data/ingestr/_meta.json"
cat "$RUN/output/bruin-data/ingestr/eval_build/Dockerfile"
head -30 "$RUN/output/bruin-data/ingestr/eval_build/setupx_replay.sh"
```

Expected: `_meta.json` has `"producer": "setupx"`, `"status": "produced"`, `"conformance": "synthesized"`, a non-null `head_sha` matching the dataset row, and a `note` carrying the phase-2 verdict. The Dockerfile pins that SHA, `COPY`s the replay script, and ends at `WORKDIR /testbed`.

- [ ] **Step 4: Check the measure side**

```bash
cat "$RUN/measure/metrics.json"
```

Expected: one row for `bruin-data/ingestr` with `build_ok: true` and a `status` that is not `non_conforming` (that would mean the `/testbed` probe failed) and not `empty_testbed` (that would mean the re-home moved the wrong directory).

- [ ] **Step 5: Confirm the master store did not grow**

```bash
docker exec setupx-xpu psql -U postgres -d xpu_warm -c 'SELECT count(*) FROM xpu_entries;'
```

Expected: still 600. (`xpu_run` may have grown — that copy is discarded and recreated before the
next run.)

- [ ] **Step 6: Run the arm**

```bash
./run_bench.sh setupx --repos-json datasets/rat_python50.json --tier all --concurrency 4
```

Concurrency is safe **only with `tools/setupx-bench.patch` applied** (Task 5 Step 1). Without
it the arm must run at `--concurrency 1`: SetupX's checkpoint images live in one global repository
that every `EnvironmentManager` wipes on construction, so parallel repos silently destroy each
other's snapshots. Confirm the patch is in place before raising concurrency:

```bash
grep -q "_ckpt_repo" "$SETUPX_ROOT/src/environment_manager.py" && echo "safe to parallelise"
```

Then confirm no checkpoint images leaked once the run finishes:

```bash
docker images --filter 'reference=setup_agent_checkpoint*' -q | wc -l   # expect: 0
```

- [ ] **Step 7: Commit nothing**

This task produces run artifacts under `runs/`, which is not tracked. If Steps 3-5 surfaced a defect, fix it in `producers/setupx.py` under Task 1-3's test discipline — add the failing test first.

---

## Self-Review

**Spec coverage.** The `Upstream Facts` section's claims each map to a task: entry point and report shape → Task 3; history/action schema and snapshot semantics → Task 1; container paths → Task 2; `load_dotenv(override=True)` → Task 3's `_setupx_root`; LLM config → Task 3's `child_env`; XPU client gating, `FREEZE_TELEMETRY`, embeddings fallback → Tasks 3 and 5; unpinned clone → Task 3's `mirror_image`; no token accounting → documented in Tasks 3 and 5.

**Known gaps, deliberately out of scope.** (1) An `unreplayed=true` run is still measured; deciding whether to exclude those rows from the headline is a scoring question, not a producer question. (2) The `ENV PATH` line guesses two venv names — see the `ponytail:` comment. (3) No XPU-off comparison arm; adding `setupx-noxpu` later is a `varieties.toml` block plus an `--no-xpu` flag in `run_setupx`, no new transforms.

**Type consistency.** `plan_replay` returns `(steps, lossy)` and is called that way in Task 3. `render_dockerfile` returns `(dockerfile, scripts)` keyed by `REPLAY_BASENAME`, consumed as `ProducedEnv.setup_scripts`. `run_setupx`'s return keys (`history`, `completed`, `steps_taken`, `phase2`, `economy`) match every stub in the tests and every read in `produce`. `child_env(llm, base_image)` is called with both arguments at its one call site.
