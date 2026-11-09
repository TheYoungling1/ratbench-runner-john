# pipreqs Baseline Producer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `pipreqs` (static import-scanning `requirements.txt` generator) as a new,
non-agentic, zero-LLM-cost baseline variety in this environment-construction benchmark, so the
paper has a deterministic cost/EBSR floor to compare the agentic arms against.

**Architecture:** A new `Producer` (`producers/base.py::Producer` protocol) that clones the
pinned commit, runs the `pipreqs` CLI as pure static analysis (no docker, no LLM, no network
calls except `pipreqs`'s own live PyPI lookups), and emits a Dockerfile + a
`requirements_pipreqs.txt` sibling file. Registered exactly like every other produce-able
variety (`dockeragent`, `repo2run`, ...) — `bench/` independently rebuilds and scores it; this
producer never builds, runs pytest, or scores anything itself.

**Tech Stack:** Python 3.10+ (matches the runner venv), `pipreqs` 0.4.13 (PyPI), `git`, `pytest`
for unit tests. No new infra — no docker, no separate venv (unlike `sweagent`'s py3.11 split),
no API keys.

**Spec:** This document is self-contained; there is no separate spec doc. Background research
and the design rationale (why pipreqs, why this Dockerfile shape, why `--mode no-pin`) live in
this conversation's transcript — the relevant facts are restated inline below where a task
needs them, so the plan does not depend on reading that transcript.

## Global Constraints

- Python target: 3.10+ (the runner venv's floor — see `requirements.txt` header comment).
- `pipreqs` version: pin `pipreqs==0.4.13` in `requirements.txt` (the version this plan was
  researched and tested against; a floor pin `>=0.4.13` risks CLI flag changes going unnoticed).
- Version-pinning mode: **`--mode no-pin`** (bare package names, no version constraint). Decision
  rationale: `pipreqs`'s default mode queries live PyPI and pins to whatever's newest *right now*
  — running this baseline on the same pinned commit weeks apart would silently produce a
  different `requirements_pipreqs.txt`. `no-pin` removes that specific drift; residual
  nondeterminism moves to `pip`'s own resolver at build time, which is an accepted, disclosed
  tradeoff (see Task 2's docstring), not something later tasks should try to eliminate.
- Producer conformance lane: **`ProducedEnv.conformance = "native"`** (the Dockerfile clones
  straight to `/testbed`), not `"rehomed"`. This intentionally diverges from the Repo2Run paper's
  own reference template (`rat/eval/pipreqs/pipreqs_ref.md`, which clones to `/repo` and needs a
  rehome step, matching `sweagent_repo2run`'s paper-fidelity requirement) — this baseline is not
  reproducing the paper's literal Dockerfile, only its dependency-detection *method*, so the
  simpler direct-clone shape is correct and no rehome transform is needed.
- **Two different enums, do not conflate them.** `ProducedEnv.conformance` is
  `"native" | "rehomed" | "synthesized"` (`producers/base.py:217`) — a producer-side description of
  the emitted Dockerfile's shape; every existing direct-clone-to-`/testbed` producer sets
  `"native"` (`producers/dockeragent.py:142`, `producers/claudecode_dockerfile.py:240`). The
  `varieties.toml` `measure =` key is a *separate* vocabulary, `"conforming" | "rehome" | "none"`
  (`runner/registry.py:20`), selecting the runner's measure lane. So this arm is
  `conformance="native"` in the producer AND `measure = "conforming"` in the toml. Nothing
  validates `ProducedEnv.conformance` at runtime, so getting it wrong fails silently and only
  shows up as a corrupted `_meta.json` field in the paper's grouping.
- Base image: `python:3.10` (full, not `-slim` — needs `git` present for the in-Dockerfile
  clone; matches the convention every other arm's Dockerfile template uses).
- `needs_llm = False`. This is the first producer in the registry with this flag — every other
  registered producer (`dockeragent`, `repo2run`, `claudecode_dockerfile`, `executionagent`,
  `sweagent_repo2run`, `sweagent_repo2run_modern`, `setupx`, `rat`, `sweagent`,
  `claudecode_live`) has `needs_llm = True`. No special handling is required for this — the
  registry already treats it as a plain boolean per-producer flag — in fact nothing in the repo
  branches on `needs_llm` at all today (verified by grepping every reference), so a reviewer
  unfamiliar with the registry might assume `needs_llm=False` needs new plumbing. It does not.
- **`__init__` must still accept `llm` even though this arm never uses it.**
  `runner/benchmark.py:148` (`_ProducerModel.predict`) unconditionally builds `kw = {"llm":
  self.llm}` and calls `producers.get(self.name, **kw)` for EVERY produce-able model — there is no
  `needs_llm` check on that path. A constructor without an `llm` parameter raises
  `TypeError: PipreqsProducer.__init__() got an unexpected keyword argument 'llm'` on the first
  real run, and no unit test that instantiates the class directly will catch it. Every sibling
  producer already takes `llm: str | None = None` for this reason
  (`producers/setupx.py:393`, `producers/repo2run.py:186`, `producers/dockeragent.py:69`).
  `pipreqs` is not in `predict`'s `num_turn` branch list, so it receives `llm` and nothing else —
  same shape as `claudecode-dockerfile`.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `requirements.txt` | modify | Add the `pipreqs` dependency |
| `producers/pipreqs_baseline.py` | create | `run_pipreqs()` (clone + scan) + `PipreqsProducer` (the `Producer`) |
| `producers/tests/test_pipreqs_baseline.py` | create | Unit tests, stubbed — no real git/pipreqs/network |
| `producers/__init__.py` | modify | Import + register `PipreqsProducer` |
| `runner/benchmark.py` | modify | Add `"pipreqs"` to `_PRODUCE_ABLE` |
| `varieties.toml` | modify | Add `[variety.pipreqs]` |

One new file for the producer (`pipreqs_baseline.py`, not `pipreqs.py` — avoids shadowing the
real `pipreqs` package if it's ever imported by name inside this package's own directory,
mirroring the exact problem `producers/sweagent_repo2run_runner.py` already documents for its
own directory-shadowing situation with the real `sweagent` package).

---

### Task 1: `run_pipreqs()` — clone, pin, scan (pure logic, no Producer yet)

**Files:**
- Create: `producers/pipreqs_baseline.py` (this task writes only `run_pipreqs()` and its
  imports/constants — not `PipreqsProducer`, that's Task 2)
- Test: `producers/tests/test_pipreqs_baseline.py` (this task writes the tests for
  `run_pipreqs()` only)

**Interfaces:**
- Consumes: `bench.schema.RepoSpec` (`full_name: str`, `repo_url: str`, `language: str`,
  `commit: Optional[str]`), `producers.base.ProduceContext` (`.workdir: str`).
- Produces: `run_pipreqs(repo: RepoSpec, ctx: ProduceContext, *, runner=subprocess.run,
  timeout: int = 120) -> dict` returning `{"requirements": str, "produce_s": float}` on success.
  Raises on failure (clone failure, pipreqs CLI failure, timeout) — Task 2's `PipreqsProducer`
  catches and converts to `ProducedEnv(status="error")`. The `runner` keyword is the seam Task
  1's own tests use to stub every `subprocess.run` call — never a real git clone or real
  `pipreqs` invocation in this task's tests.

- [x] **Step 1: Write the failing tests for `run_pipreqs()`**

```python
# producers/tests/test_pipreqs_baseline.py
import os

from producers.base import ProduceContext, RepoSpec
from producers.pipreqs_baseline import run_pipreqs


def _ctx(tmp_path):
    return ProduceContext(llm=None, workdir=str(tmp_path))


class _FakeCompletedProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = b""
        self.stderr = b""


def test_run_pipreqs_clones_pins_and_scans(tmp_path):
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        # The 3rd call is the `pipreqs` invocation itself — write the file it's told to write,
        # since run_pipreqs() reads that file back after the subprocess call returns.
        if isinstance(cmd, list) and cmd and cmd[0] == "pipreqs":
            savepath = cmd[cmd.index("--savepath") + 1]
            with open(savepath, "w") as f:
                f.write("Flask==3.1.3\nrequests==2.34.2\n")
        return _FakeCompletedProcess()

    repo = RepoSpec("o/r", "https://github.com/o/r", commit="c0ffee")
    result = run_pipreqs(repo, _ctx(tmp_path), runner=fake_runner)

    assert result["requirements"] == "Flask==3.1.3\nrequests==2.34.2\n"
    assert result["produce_s"] >= 0

    # Call 1: clone. Call 2 & 3: pin (fetch, checkout). Call 4: pipreqs itself.
    assert len(calls) == 4
    clone_cmd = calls[0][0]
    assert "git" in clone_cmd and "clone" in clone_cmd
    assert "https://github.com/o/r.git" in clone_cmd
    fetch_cmd = calls[1][0]
    assert "fetch" in fetch_cmd and "c0ffee" in fetch_cmd
    checkout_cmd = calls[2][0]
    assert "checkout" in checkout_cmd and "c0ffee" in checkout_cmd
    pipreqs_cmd = calls[3][0]
    assert pipreqs_cmd[0] == "pipreqs"
    assert "--mode" in pipreqs_cmd and "no-pin" in pipreqs_cmd
    assert "--force" in pipreqs_cmd


def test_run_pipreqs_no_commit_skips_pin(tmp_path):
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        if isinstance(cmd, list) and cmd and cmd[0] == "pipreqs":
            savepath = cmd[cmd.index("--savepath") + 1]
            with open(savepath, "w") as f:
                f.write("\n")
        return _FakeCompletedProcess()

    repo = RepoSpec("o/r", "https://github.com/o/r")  # commit=None
    result = run_pipreqs(repo, _ctx(tmp_path), runner=fake_runner)

    # "\n" is what real pipreqs 0.4.13 writes for a stdlib-only repo (verified: the savepath file
    # is one byte, b"\n", not zero bytes) — a VALID result, not an error. The stub above writes
    # the same thing so the test asserts a true fact about the tool.
    assert result["requirements"] == "\n"
    assert len(calls) == 2                        # clone + pipreqs only — no fetch/checkout


def test_run_pipreqs_missing_output_file_raises(tmp_path):
    # pipreqs "succeeds" (returncode 0) but never writes the savepath — must raise, not silently
    # return an empty dict (Task 2 depends on this raising to know the difference between
    # "genuinely no third-party imports" (Task above, an empty string) and "something is wrong").
    def fake_runner(cmd, **kwargs):
        return _FakeCompletedProcess()   # never writes the savepath file

    repo = RepoSpec("o/r", "https://github.com/o/r")
    try:
        run_pipreqs(repo, _ctx(tmp_path), runner=fake_runner)
        assert False, "expected an exception"
    except FileNotFoundError:
        pass


def test_run_pipreqs_clone_failure_propagates(tmp_path):
    import subprocess

    def fake_runner(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    repo = RepoSpec("o/r", "https://github.com/o/r")
    try:
        run_pipreqs(repo, _ctx(tmp_path), runner=fake_runner)
        assert False, "expected an exception"
    except subprocess.CalledProcessError:
        pass
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest producers/tests/test_pipreqs_baseline.py -v`
Expected: `ModuleNotFoundError: No module named 'producers.pipreqs_baseline'` (the file doesn't
exist yet) — all 4 tests fail on collection.

- [x] **Step 3: Write `run_pipreqs()`**

```python
# producers/pipreqs_baseline.py
#
# The PRODUCE half of a static, non-agentic baseline: run `pipreqs` (import-scanning
# requirements.txt generator) against the dataset-pinned commit, then emit a Dockerfile that
# installs whatever it found. NO docker build, NO pytest, NO scoring — bench/ rebuilds and
# scores from the packet write_env_packet lands, exactly like every other producer.
#
# This is the first producer with needs_llm=False: no API key, no agent, no LLM turns at all.
# `pipreqs` itself still makes network calls (it queries PyPI live to resolve an import name it
# doesn't recognize locally — see the WARNING lines it prints), so this is not fully offline,
# but it spends zero LLM tokens and zero dollars.
from __future__ import annotations

import os
import subprocess
import time

from producers.base import ProduceContext, ProducedEnv, inject_clone_pin

# bench.schema is on the path via producers.base's shim (imported above).
from bench.schema import RepoSpec  # noqa: E402

# --mode no-pin (bare package names, no version): pipreqs's DEFAULT mode queries live PyPI and
# pins to whatever is newest RIGHT NOW. Re-running this baseline on the identical pinned commit
# weeks apart would then silently produce a DIFFERENT requirements_pipreqs.txt — the same
# "alias drift" reproducibility risk this repo already has to manage for LLM model slugs.
# no-pin removes that specific drift; pip's own resolver still varies at install time, which is
# an accepted, disclosed tradeoff, not a bug to fix later.
_PIPREQS_MODE = "no-pin"
_IGNORE_DIRS = ".venv,venv,env"


def run_pipreqs(repo: RepoSpec, ctx: ProduceContext, *, runner=subprocess.run,
                timeout: int = 120) -> dict:
    """Clone `repo` pinned to `repo.commit` (if set), run `pipreqs` against the pinned tree, and
    return {"requirements": <file content, possibly empty>, "produce_s": float}.

    `runner` is the injectable subprocess seam (tests stub it; production leaves it as
    subprocess.run). Never swallows a failure — a clone error, a pipreqs CLI error, or a missing
    output file all raise, so the caller (PipreqsProducer.produce) can distinguish "genuinely no
    third-party imports found" (a valid empty string) from "something actually broke" (raise).
    """
    start = time.time()
    repo_path = os.path.join(ctx.workdir, "repo")
    os.makedirs(ctx.workdir, exist_ok=True)

    runner(["git", "clone", "--depth=1", f"{repo.repo_url}.git", repo_path],
           check=True, capture_output=True, timeout=timeout)

    if repo.commit:
        # Pin BEFORE scanning: pipreqs must see the dataset-pinned tree, not live HEAD, or the
        # detected imports (and therefore the whole point of pinning the dataset) are wrong.
        runner(["git", "fetch", "--depth", "1", "origin", repo.commit],
               cwd=repo_path, check=True, capture_output=True, timeout=timeout)
        runner(["git", "checkout", "--detach", repo.commit],
               cwd=repo_path, check=True, capture_output=True, timeout=timeout)

    req_path = os.path.join(repo_path, "requirements_pipreqs.txt")
    runner(["pipreqs", repo_path, "--savepath", req_path, "--force",
            "--ignore", _IGNORE_DIRS, "--mode", _PIPREQS_MODE],
           check=True, capture_output=True, timeout=timeout)

    with open(req_path) as f:               # raises FileNotFoundError if pipreqs never wrote it
        requirements = f.read()

    return {"requirements": requirements, "produce_s": round(time.time() - start, 2)}
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. python3 -m pytest producers/tests/test_pipreqs_baseline.py -v`
Expected: all 4 tests PASS.

- [x] **Step 5: Commit**

```bash
git add producers/pipreqs_baseline.py producers/tests/test_pipreqs_baseline.py
git commit -m "feat(pipreqs): add run_pipreqs() clone+scan, stubbed unit tests

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: `PipreqsProducer` — the `Producer` protocol implementation

**Files:**
- Modify: `producers/pipreqs_baseline.py` (add `PipreqsProducer` below `run_pipreqs`)
- Test: `producers/tests/test_pipreqs_baseline.py` (append)

**Interfaces:**
- Consumes: `run_pipreqs(repo, ctx, runner=..., timeout=...) -> dict` (Task 1), `producers.base`:
  `ProduceContext`, `ProducedEnv`, `inject_clone_pin(dockerfile: str, commit: str, repo_url:
  Optional[str]) -> tuple[str, bool]`.
- Produces: `PipreqsProducer(llm=None, runner=run_pipreqs)` — a class satisfying
  `producers.base.Producer`
  (`name: str`, `needs_llm: bool`, `measurable: bool`, `.produce(repo, ctx) -> ProducedEnv`).
  Task 3 imports this class by name (`from producers.pipreqs_baseline import PipreqsProducer`).

- [x] **Step 1: Write the failing tests for `PipreqsProducer`**

Append to `producers/tests/test_pipreqs_baseline.py`:

```python
from producers.base import ProducedEnv
from producers.pipreqs_baseline import PipreqsProducer


def test_producer_success_is_native(tmp_path):
    def _stub(repo, ctx, **kw):
        return {"requirements": "Flask==3.1.3\n", "produce_s": 1.23}

    p = PipreqsProducer(runner=_stub)
    env = p.produce(RepoSpec("o/r", "https://github.com/o/r", commit="c0ffee"), _ctx(tmp_path))

    assert env.status == "produced"
    assert env.conformance == "native"      # ProducedEnv's enum, NOT varieties.toml's "conforming"
    assert env.producer_name == "pipreqs"
    assert env.base_image == "python:3.10"
    assert env.head_sha == "c0ffee"
    assert env.setup_scripts == {"requirements_pipreqs.txt": "Flask==3.1.3\n"}
    assert "COPY requirements_pipreqs.txt /requirements_pipreqs.txt" in env.dockerfile
    assert "pip install -r /requirements_pipreqs.txt" in env.dockerfile
    assert env.economy["produce_s"] == 1.23


def test_producer_empty_requirements_still_produces(tmp_path):
    # A stdlib-only repo is a VALID pipreqs result, not an error: the Dockerfile still installs
    # pytest via its own fixed lines, and `pip install -r` on an empty file is a no-op.
    def _stub(repo, ctx, **kw):
        return {"requirements": "", "produce_s": 0.5}

    env = PipreqsProducer(runner=_stub).produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "produced"
    assert env.setup_scripts == {"requirements_pipreqs.txt": ""}


def test_producer_pins_the_emitted_dockerfiles_own_clone(tmp_path):
    def _stub(repo, ctx, **kw):
        return {"requirements": "Flask==3.1.3\n", "produce_s": 1.0}

    env = PipreqsProducer(runner=_stub).produce(
        RepoSpec("o/r", "https://github.com/o/r", commit="c0ffee"), _ctx(tmp_path))
    assert "git -C /testbed fetch --depth 1 origin c0ffee" in env.dockerfile
    assert "git -C /testbed checkout --detach c0ffee" in env.dockerfile
    assert env.note == ""


def test_producer_no_commit_leaves_dockerfile_unpinned(tmp_path):
    def _stub(repo, ctx, **kw):
        return {"requirements": "Flask==3.1.3\n", "produce_s": 1.0}

    env = PipreqsProducer(runner=_stub).produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))   # commit=None
    assert "checkout --detach" not in env.dockerfile


def test_producer_run_pipreqs_failure_is_error_not_raise(tmp_path):
    def _boom(repo, ctx, **kw):
        raise RuntimeError("pipreqs exploded")

    env = PipreqsProducer(runner=_boom).produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "error" and env.dockerfile is None
    assert "pipreqs exploded" in env.note


def test_producer_is_needs_llm_false():
    assert PipreqsProducer.needs_llm is False
    assert PipreqsProducer.measurable is True
    assert PipreqsProducer.name == "pipreqs"


def test_producer_is_registered():
    from producers import PRODUCERS
    assert PRODUCERS["pipreqs"] is PipreqsProducer


def test_producer_accepts_llm_kwarg_from_the_runner():
    # runner/benchmark.py:148 calls producers.get(name, llm=...) for EVERY produce-able model,
    # with no needs_llm check. A constructor without `llm` blows up with TypeError on the first
    # real run and NO direct-instantiation test catches it. This is that test.
    import producers
    prod = producers.get("pipreqs", llm="anything")
    assert isinstance(prod, PipreqsProducer)
```

Note: `test_producer_is_registered` will fail until Task 3 registers the producer — that is
expected and correct (TDD across tasks); do not skip or delete it now.

- [x] **Step 2: Run the tests to verify the new ones fail**

Run: `PYTHONPATH=. python3 -m pytest producers/tests/test_pipreqs_baseline.py -v`
Expected: the 7 new tests FAIL (`ImportError: cannot import name 'PipreqsProducer'`);
`test_producer_is_registered` will keep failing even after Step 4 below — that's fixed in Task 3.

- [x] **Step 3: Write `PipreqsProducer`**

Append to `producers/pipreqs_baseline.py`:

```python
_DOCKERFILE_TEMPLATE = """FROM python:3.10
WORKDIR /
RUN pip install pytest pytest-xdist
RUN git clone {repo_url}.git /testbed
WORKDIR /testbed
COPY requirements_pipreqs.txt /requirements_pipreqs.txt
RUN pip install -r /requirements_pipreqs.txt || true
"""
# `|| true`: a bad pin/resolved-version in the generated list must not abort the build before
# `pytest --collect-only` runs — that would score an EBSR-0 for "the Dockerfile didn't build"
# rather than "the collected tests failed", conflating two different failure signals. The
# tradeoff is a half-installed environment can silently reach the collect step; distinguishing
# "pip install actually failed" from "pip install succeeded but the deps were wrong" needs a
# separate pass over the build log if that distinction matters later — not handled here.


class PipreqsProducer:
    """Static, non-agentic baseline: pipreqs-detected requirements + a fixed Dockerfile shape.
    No docker build, no pytest, no scoring here — bench/ owns MEASURE, same as every producer."""

    name = "pipreqs"
    needs_llm = False       # the first producer in the registry with this flag — see the plan's
                            # Global Constraints for why that needs no special registry handling
    measurable = True

    def __init__(self, llm: str | None = None, runner=run_pipreqs):
        # `llm` is accepted and ignored: runner/benchmark.py:148 passes llm= to EVERY produce-able
        # producer with no needs_llm check, so dropping the parameter is a TypeError on the first
        # real run. Same signature shape as every sibling producer.
        self._runner = runner

    def produce(self, repo: RepoSpec, ctx: ProduceContext) -> ProducedEnv:
        try:
            result = self._runner(repo, ctx)
        except Exception as e:                        # noqa: BLE001 — anti-vanish, never raise
            return ProducedEnv(repo=repo, dockerfile=None, status="error",
                                note=repr(e), producer_name=self.name)

        dockerfile = _DOCKERFILE_TEMPLATE.format(repo_url=repo.repo_url)
        note = ""
        if repo.commit:
            dockerfile, injected = inject_clone_pin(dockerfile, repo.commit, repo.repo_url)
            if not injected:
                note = "pin_warning: could not pin the emitted Dockerfile's clone"

        return ProducedEnv(
            repo=repo,
            dockerfile=dockerfile,
            setup_scripts={"requirements_pipreqs.txt": result["requirements"]},
            base_image="python:3.10",
            head_sha=repo.commit,
            status="produced",
            conformance="native",   # ProducedEnv's enum; the toml's measure="conforming" is separate
            note=note,
            producer_name=self.name,
            economy={"produce_s": result.get("produce_s")},
            # No tokens_in/out, llm_calls, turns_used, cost_usd: nothing to report — this arm
            # spends zero LLM tokens and zero dollars. write_env_packet already handles these
            # as None when absent from `economy` (see producers/base.py::write_env_packet).
        )
```

- [x] **Step 4: Run the tests — all but registration should now pass**

Run: `PYTHONPATH=. python3 -m pytest producers/tests/test_pipreqs_baseline.py -v`
Expected: every test PASSES except `test_producer_is_registered`, which still fails
(`KeyError: 'pipreqs'`) — that is correct at this point; Task 3 fixes it.

- [x] **Step 5: Commit**

```bash
git add producers/pipreqs_baseline.py producers/tests/test_pipreqs_baseline.py
git commit -m "feat(pipreqs): add PipreqsProducer (needs_llm=False, native lane)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Register the producer and the variety

**Files:**
- Modify: `producers/__init__.py`
- Modify: `runner/benchmark.py`
- Modify: `varieties.toml`
- Modify: `requirements.txt`
- Test: `producers/tests/test_pipreqs_baseline.py::test_producer_is_registered` (already written
  in Task 2 — this task makes it pass), plus the existing parametrized
  `producers/tests/test_measure_config.py` (no new test file — the new variety is picked up
  automatically because that file's tests are parametrized over every `[variety.*]` block, per
  its own docstring: *"Every current varieties.toml model is a registered producer; assert it
  rather than skip so a future config/registry omission fails loudly instead of vanishing into a
  silent skip"*).

**Interfaces:**
- Consumes: `PipreqsProducer` (Task 2), `producers.register()`, `producers.PRODUCERS` dict.
- Produces: `producers.PRODUCERS["pipreqs"]` resolvable; `[variety.pipreqs]` resolvable by
  `runner/registry.py::resolve_variety`; `./run_bench.sh pipreqs ...` accepted as a valid variety
  name by the CLI's `choices=` (via `_PRODUCE_ABLE`).

- [x] **Step 1: Add the dependency**

In `requirements.txt`, under the `# --- baselines ---` section (after the existing comment
block about `sweagent` not being listed there), add:

```
pipreqs==0.4.13
```

- [x] **Step 2: Register the producer**

In `producers/__init__.py`, add the import alongside the other producer imports (after the
`from producers.setupx import SetupXProducer` line, before the "Non-producers" comment block —
`pipreqs` is measurable, so it belongs with the measurable group, not the native-lane group):

```python
from producers.pipreqs_baseline import PipreqsProducer  # noqa: E402
```

Add the registration call alongside the others (after `register(SetupXProducer)`):

```python
register(PipreqsProducer)
```

Add `"PipreqsProducer"` to the `__all__` list.

- [x] **Step 3: Add to `_PRODUCE_ABLE`**

In `runner/benchmark.py`, find the existing set (around line 111):

```python
_PRODUCE_ABLE = {"dockeragent", "repo2run", "claudecode-dockerfile", "executionagent",
                 "sweagent_repo2run", "sweagent_repo2run_modern", "setupx"}
```

Change to:

```python
_PRODUCE_ABLE = {"dockeragent", "repo2run", "claudecode-dockerfile", "executionagent",
                 "sweagent_repo2run", "sweagent_repo2run_modern", "setupx", "pipreqs"}
```

- [x] **Step 4: Add the variety**

In `varieties.toml`, add a new block. Place it near `[variety.repo2run]` (both are baselines
with no agent branch) — after the `[variety.repo2run]` block and before the
`# General coding agents` comment that precedes `[variety.sweagent]`:

```toml
# Static, non-agentic baseline: pipreqs scans the repo's imports and writes a
# requirements.txt; no LLM, no API key, near-zero produce_s. A cost/EBSR floor for the
# agentic arms above — see producers/pipreqs_baseline.py for the version-pinning-mode
# rationale (--mode no-pin, to avoid live-PyPI-version drift across reruns of this arm).
[variety.pipreqs]
model = "pipreqs"
measure = "conforming"
```

No `llm =` line: this arm never uses a model slug, and `resolve_variety` resolves a missing
`llm` to `None` with no error. The runner still *passes* that `None` to the constructor as
`llm=None` (see Global Constraints) — the parameter is accepted and ignored, not absent.

- [x] **Step 5: Run the full producers test suite**

Run: `PYTHONPATH=. python3 -m pytest producers/tests/ -v`
Expected: every test passes, including the previously-failing
`test_pipreqs_baseline.py::test_producer_is_registered` and every parametrized case in
`test_measure_config.py` (the new `pipreqs` variety now appears as one of its parametrized
cases — `test_every_variety_has_valid_measure[pipreqs-...]` and
`test_measure_matches_producer_measurability[pipreqs-...]` — and both must pass because
`measure="conforming"` in the toml matches `PipreqsProducer.measurable=True`).

- [x] **Step 6: Install the new dependency locally and re-verify**

Run: `.venv/bin/pip install -r requirements.txt`
Run: `PYTHONPATH=. .venv/bin/python -m pytest producers/tests/ -v`
Expected: same as Step 5, now against the real installed `.venv`, confirming `pipreqs==0.4.13`
installs cleanly alongside the existing pinned dependencies (no version conflict).

- [x] **Step 7: Commit**

```bash
git add producers/__init__.py runner/benchmark.py varieties.toml requirements.txt
git commit -m "feat(pipreqs): register the pipreqs variety and producer

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: Real smoke test (one repo, real git/pipreqs/docker — no stubs)

**Files:** none (this task runs the harness; it does not modify code, unless the smoke test
surfaces a real bug, in which case fix it in the file that owns the bug and re-run this task).

**Interfaces:** none new — this task exercises the full stack Tasks 1-3 built, end to end.

- [x] **Step 1: Confirm `pipreqs` is on PATH in the venv that will run the smoke test**

Run: `.venv/bin/pipreqs --help | head -3`
Expected: prints the pipreqs usage banner. If this fails, Task 3 Step 6 was skipped or failed —
go back and fix that before continuing.

- [x] **Step 2: Run the smoke test against the smallest repo in the dataset**

`jhao104/proxy_pool` is the smallest repo in `datasets/rat_python50.json` (72KB) — the same repo
used for the `sweagent_repo2run_modern` OpenRouter/DeepInfra smoke test earlier in this project's
history, for a fast, cheap, known-good signal.

Run:
```bash
source env.sh
python -m runner.cli pipreqs --repos-json datasets/rat_python50.json --only jhao104/proxy_pool --tier all
```

Expected: exits 0. A run directory appears under `runs/pipreqs/<timestamp>/`.

- [x] **Step 3: Inspect the produced artifact**

Run:
```bash
cat runs/pipreqs/*/output/jhao104/proxy_pool/eval_build/Dockerfile
cat runs/pipreqs/*/output/jhao104/proxy_pool/eval_build/requirements_pipreqs.txt
```

Expected: the Dockerfile matches `_DOCKERFILE_TEMPLATE` with the pin lines inserted after the
`git clone` (matching the real output shape confirmed manually earlier in this project's
research: `apscheduler`, `click`, `fakeredis`, `Flask`, `gunicorn`, `lxml`, `pytest`, `redis`,
`Requests`, `Werkzeug`, unpinned since `--mode no-pin`). The exact package list may differ
slightly from that manual run if PyPI's import-name resolution has changed since, which is
expected and not a bug.

- [x] **Step 4: Run the measure stage and confirm a real score**

```bash
python -m bench.unified_bench --harvest pipreqs=runs/pipreqs/<timestamp>/output \
                               --out runs/pipreqs/<timestamp>/measure
cat runs/pipreqs/<timestamp>/measure/metrics.json
```
(substitute the actual timestamp from Step 2's run directory)

Expected: `metrics.json` contains an `EBSR`/`ESSR` entry for `n=1`. The score itself may be 0 or
1 — either is a valid outcome for a single-repo smoke test of a static baseline; what this step
verifies is that the pipeline runs end-to-end (produce → measure → score) without erroring, not
that the score is high.

- [x] **Step 5: If it EBSR-0'd, check why before concluding something is broken**

Read `runs/pipreqs/<timestamp>/output/jhao104/proxy_pool/eval_build/requirements_pipreqs.txt`
and the docker build log bench writes under `measure/`. Two expected (not-a-bug) reasons a
static baseline can legitimately fail this specific repo: a resolved package version pip can't
find (`no-pin` mode should prevent this, but PyPI's import→package mapping can still resolve to
the wrong package name entirely), or a system-level dependency pipreqs has no way to know about
(this repo has no such dependency as of the manual research earlier, but PyPI package versions
move). A third, and the most likely cause of a SYSTEMATIC low score across the other 49 repos:
the emitted Dockerfile never runs `pip install -e .`, so a repo whose tests import its own
package by name fails to collect for a reason that has nothing to do with pipreqs. That omission
is deliberate and correct — `rat/eval/pipreqs/pipreqs_ref.md`, the Repo2Run paper's own reference
template for this baseline, also installs only the generated requirements — so do NOT "fix" it by
adding an install step; that would stop measuring the baseline the paper defines. Note it as the
cause if you see it, and likewise do not special-case any single repo — that would defeat the
point of a baseline that has to work the same way for all 50.

- [x] **Step 6: Commit the run output — or don't, per repo convention**

Check whether `runs/` is gitignored (`git check-ignore runs/pipreqs` — this repo's `runs/`
directories have not been committed anywhere in this project's history; if `git status` shows
the new `runs/pipreqs/` directory as untracked and every other `runs/<variety>/` directory is
also untracked, leave it untracked — do not `git add` it).

No code commit needed for this task — it's a verification step, not a code change, unless
Step 5 surfaces a real bug in Tasks 1-3's code, in which case: fix it in
`producers/pipreqs_baseline.py`, add a regression test to
`producers/tests/test_pipreqs_baseline.py` reproducing the exact failure mode first, then re-run
this task's Steps 2-4 to confirm the fix, then commit:

```bash
git add producers/pipreqs_baseline.py producers/tests/test_pipreqs_baseline.py
git commit -m "fix(pipreqs): <describe the specific bug the smoke test found>

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage.** Every element from the design conversation this plan is based on has a
task: the `run_pipreqs()` clone+pin+scan logic (Task 1), the `Producer` wrapper with the
`native`-lane Dockerfile and `inject_clone_pin` call (Task 2), the `--mode no-pin` decision
(Global Constraints + Task 1's docstring), registration in all three places the README's
"Escape hatch" section calls out — `producers/__init__.py`, `_PRODUCE_ABLE`, `varieties.toml`
(Task 3) — the new `requirements.txt` dependency (Task 3 Step 1), and a real end-to-end smoke
test against the same smallest-repo pick used elsewhere in this project (Task 4). No gaps found.

**2. Placeholder scan.** No "TBD"/"handle edge cases"/"similar to Task N" patterns — every step
has literal, runnable code or an exact shell command. `_DOCKERFILE_TEMPLATE`'s `|| true` and
Task 4 Step 5's "don't special-case one repo" are explicit, reasoned decisions with stated
tradeoffs, not deferred work.

**3. Type/name consistency.** `run_pipreqs(repo, ctx, *, runner=..., timeout=...) -> dict` (Task
1) is called identically inside `PipreqsProducer.produce()` (Task 2) via
`self._runner(repo, ctx)` — matches. `PipreqsProducer(runner=run_pipreqs)` (Task 2's
`__init__`) is imported by exact name (`from producers.pipreqs_baseline import
PipreqsProducer`) in Task 3's registration steps — matches. `env.setup_scripts` key
`"requirements_pipreqs.txt"` (Task 2) matches the Dockerfile's `COPY requirements_pipreqs.txt`
(same task, same template) — matches, and both match the real file name pipreqs itself
generates when told `--savepath .../requirements_pipreqs.txt` (Task 1). `name = "pipreqs"`
(Task 2) matches `model = "pipreqs"` in `varieties.toml` (Task 3) and the `"pipreqs"` string
added to `_PRODUCE_ABLE` (Task 3) — matches throughout.
