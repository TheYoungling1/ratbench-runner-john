# claudecode-dockerfile Telemetry + Runner-Image Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a 50-repo `claudecode-dockerfile` run produce per-repo cost, token, turn and trajectory data, and make a missing `claude-runner:latest` image fail loudly at run start instead of silently scoring 50 zeros.

**Architecture:** The Claude Code CLI already runs with `--output-format stream-json --verbose`; its stdout carries `total_cost_usd`, `num_turns`, a `usage` block and every `tool_use`/`tool_result` event. `run_claudecode_dockerfile` currently discards that stdout and returns `economy: {}`. We add a pure stream parser to `producers/_claudecode_helpers.py`, persist the raw stream + a readable action log per repo, and populate `economy` — which the existing `write_env_packet → measure() → compute_metrics` chain already consumes. Separately we vendor the `claude-runner` Dockerfile into this repo and add an idempotent build preflight in `runner/cli.py`.

**Tech Stack:** Python 3.10+, stdlib only (`json`, `os`, `subprocess`), pytest, Docker CLI.

## Global Constraints

- **Branch from `origin/main` (`4626e95`), NOT from local `HEAD` (`6b4eaab`).** Local `main` is 10 commits ahead of `origin/main` with the undeployed multi-language measure seam (Node/Rust/Java `bench/languages/`, plus the `RepoSpec.language` threading fix). Those commits have not had a real dataset run and must not ride into the 50-repo ccdf run as a side effect. Work on a branch `ccdf-telemetry` based on `origin/main`.
- **Additive only to the produce→measure contract.** Every new field defaults to `None`. `dockeragent`, `repo2run`, `rat`, `sweagent`, `claudecode` rows must be byte-identical apart from a new `cost_usd: null`.
- **Anti-vanish invariant (design §1) is inviolable.** `produce()` must still return a `ProducedEnv` on every path. Telemetry persistence is best-effort and must never turn a successful Dockerfile into an error.
- **Token accounting matches `/opt/runs/ccdf_costs.py`:** `tokens_in = input_tokens + cache_creation_input_tokens + cache_read_input_tokens`, `tokens_out = output_tokens`. Do not silently change this — the earlier ccdf baseline numbers used it.
- **Repo root for all paths:** `/Users/john/ratbench-runner-john`.
- **Test command:** `python3 -m pytest <path> -q` from the repo root (no venv needed; `producers/tests/conftest.py` and `bench/tests/bench/conftest.py` fix `sys.path`).
- **No new third-party dependencies.**

## Background: why `economy` is empty today

`producers/claudecode_dockerfile.py:100-113` builds `claude_cmd` with `--output-format stream-json --verbose`, then:

```python
try:
    subprocess.run(claude_cmd, capture_output=True, text=True, timeout=ctx.timeout)
except subprocess.TimeoutExpired:
    pass
...
return {"dockerfile": text or None, "base_image": base_image, "economy": {}}
```

The `CompletedProcess` is never bound. Two signals this is an accidental regression from the M4.5b consolidation rather than a design choice:

1. `_as_text` is imported at `producers/claudecode_dockerfile.py:45` and never used — its only purpose is decoding that stdout.
2. The sibling native lane `runner/live/claudecode.py:98-148` (`_summarize_stream`) and `:266-285` (`_write_agent_logs`) already do the whole job for the `claudecode` variety.

Downstream is already wired and needs no changes for tokens/turns:

```
economy{tokens_in,tokens_out,llm_calls,turns_used}
  → producers/base.py:286-289   write_env_packet   → _meta.json
  → bench/bench/measure.py:175-178  base_row       → MeasureRow
  → bench/bench/metrics.py:87-101  compute_metrics → mean_tokens, tokens_per_ebsr,
                                                      tokens_per_real_success, mean_turns
```

Only `cost_usd` needs a new contract field (Task 3).

## Reference: the real `result` event

Captured from a verified smoke run on this box (`sonnet`, trivial prompt):

```json
{ "type": "result", "subtype": "success", "is_error": false,
  "num_turns": 1, "total_cost_usd": 0.0468775, "stop_reason": "end_turn",
  "duration_ms": 3059, "session_id": "1bd8a54b-...",
  "usage": { "input_tokens": 2575, "output_tokens": 4,
             "cache_creation_input_tokens": 5284, "cache_read_input_tokens": 22745 },
  "modelUsage": {
    "claude-sonnet-5":         {"inputTokens": 2575, "outputTokens": 4, "costUSD": 0.0463125},
    "claude-haiku-4-5-20251001": {"inputTokens": 505, "outputTokens": 12, "costUSD": 0.000565}
  } }
```

Note Claude Code silently used **two** models. `total_cost_usd` is the authoritative figure — do not re-derive cost from tokens and a single rate card.

## File Structure

| File | Change | Responsibility |
| --- | --- | --- |
| `producers/_claudecode_helpers.py` | Modify (append) | `summarize_stream()` — pure stream→economy parser. Stdlib only, no IO. |
| `producers/tests/test_claudecode_stream.py` | Create | Unit tests for `summarize_stream`. |
| `producers/claudecode_dockerfile.py` | Modify (`:45`, `:110-116`; add `_persist_stream`) | Capture stdout, persist stream + action log, return populated `economy`. |
| `producers/tests/test_claudecode_dockerfile.py` | Modify (append) | `_persist_stream` IO test + economy passthrough test. |
| `producers/base.py` | Modify (`:291`) | Add `cost_usd` to the `_meta.json` payload. |
| `bench/bench/schema.py` | Modify (`:68`) | Add `MeasureRow.cost_usd`. |
| `bench/bench/measure.py` | Modify (`:175-178`) | Thread `cost_usd` into `base_row`. |
| `bench/bench/metrics.py` | Modify (`:87-101`) | Aggregate `total_cost_usd`, `mean_cost_usd`, `cost_per_ebsr`, `cost_per_real_success`. |
| `bench/tests/bench/test_metrics_cost.py` | Create | Cost aggregation tests. |
| `producers/tests/test_roundtrip.py` | Modify (append) | Producer→packet→MeasureRow cost/token roundtrip. |
| `docker/claude-runner.Dockerfile` | Create | Vendored agent workbench image recipe (currently stranded in `/opt/harness`). |
| `runner/cli.py` | Modify (add `_ensure_claude_runner`, call at `:134`) | Idempotent preflight build; fail loudly if it can't. |
| `runner/tests/test_claude_runner_image.py` | Create | Preflight unit tests. |
| `README.md` | Modify | Document the ccdf artifacts + the preflight. |

---

### Task 1: Pure stream parser

**Files:**
- Modify: `producers/_claudecode_helpers.py` (append after `build_prompt`, currently ends at line 59)
- Test: `producers/tests/test_claudecode_stream.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `summarize_stream(stream_text: str) -> dict` with keys
  `turns: int|None`, `cost_usd: float|None`, `is_error: bool|None`, `stop_reason: str|None`,
  `tokens_in: int|None`, `tokens_out: int|None`, `total_tokens: int|None`,
  `llm_calls: int`, `tool_calls: int`, `model_usage: dict`, `rate_limited: bool`, `actions: str`.
  Task 2 consumes this.

- [ ] **Step 1: Write the failing test**

Create `producers/tests/test_claudecode_stream.py`:

```python
# producers/tests/test_claudecode_stream.py — summarize_stream (pure; no docker, no keys).
import json

from producers._claudecode_helpers import summarize_stream

# A realistic 2-turn stream: init -> assistant(tool_use) -> user(tool_result) -> assistant(text)
# -> result. Field shapes copied from a live `claude --output-format stream-json` capture.
_RESULT = {
    "type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
    "total_cost_usd": 0.0468775, "stop_reason": "end_turn", "session_id": "1bd8a54b-x",
    "usage": {"input_tokens": 2575, "output_tokens": 4,
              "cache_creation_input_tokens": 5284, "cache_read_input_tokens": 22745},
    "modelUsage": {"claude-sonnet-5": {"inputTokens": 2575, "outputTokens": 4,
                                       "costUSD": 0.0463125}},
}
_STREAM = "\n".join(json.dumps(o) for o in [
    {"type": "system", "subtype": "init", "session_id": "1bd8a54b-x", "cwd": "/testbed"},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "pip install -e ."}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": False, "content": "ok"}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Wrote the Dockerfile."}]}},
    _RESULT,
])


def test_extracts_cost_turns_and_stop_reason():
    s = summarize_stream(_STREAM)
    assert s["cost_usd"] == 0.0468775
    assert s["turns"] == 2
    assert s["is_error"] is False
    assert s["stop_reason"] == "end_turn"


def test_token_accounting_matches_ccdf_costs_definition():
    # tokens_in = input + cache_creation + cache_read (the definition ccdf_costs.py uses, so
    # ccdf numbers stay comparable with the earlier ccdf baselines).
    s = summarize_stream(_STREAM)
    assert s["tokens_in"] == 2575 + 5284 + 22745
    assert s["tokens_out"] == 4
    assert s["total_tokens"] == 2575 + 5284 + 22745 + 4


def test_counts_llm_calls_and_tool_calls():
    s = summarize_stream(_STREAM)
    assert s["llm_calls"] == 2        # two `assistant` events
    assert s["tool_calls"] == 1       # one tool_use block


def test_action_log_records_tool_use_and_text():
    s = summarize_stream(_STREAM)
    assert "Bash" in s["actions"]
    assert "pip install -e ." in s["actions"]
    assert "Wrote the Dockerfile." in s["actions"]


def test_per_model_usage_is_preserved():
    # Claude Code silently mixes models (a haiku for side tasks); a single rate card would
    # mis-price the run, so the per-model breakdown must survive.
    s = summarize_stream(_STREAM)
    assert s["model_usage"]["claude-sonnet-5"]["costUSD"] == 0.0463125


def test_truncated_stream_degrades_to_none_not_raise():
    # A budget-capped or timed-out run emits no `result` event. Everything the agent DID do
    # must still be recoverable, and no field may raise.
    truncated = _STREAM.split("\n")[0] + "\n{not json at all\n" + "\n".join(
        _STREAM.split("\n")[1:3])
    s = summarize_stream(truncated)
    assert s["cost_usd"] is None and s["turns"] is None and s["tokens_in"] is None
    assert s["tool_calls"] == 1       # the work it did before the wall is still counted


def test_empty_stream_is_safe():
    s = summarize_stream("")
    assert s["cost_usd"] is None and s["llm_calls"] == 0 and s["actions"] == ""


def test_rate_limit_event_is_flagged():
    stream = json.dumps({"type": "rate_limit_event",
                         "rate_limit_info": {"status": "rejected"}})
    assert summarize_stream(stream)["rate_limited"] is True


def test_allowed_rate_limit_event_is_not_flagged():
    stream = json.dumps({"type": "rate_limit_event",
                         "rate_limit_info": {"status": "allowed"}})
    assert summarize_stream(stream)["rate_limited"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/john/ratbench-runner-john && python3 -m pytest producers/tests/test_claudecode_stream.py -q`
Expected: FAIL — `ImportError: cannot import name 'summarize_stream' from 'producers._claudecode_helpers'`

- [ ] **Step 3: Write minimal implementation**

Add `import json` to the top of `producers/_claudecode_helpers.py` (the module currently imports nothing), then append:

```python
def summarize_stream(stream_text: str) -> dict:
    """Parse claude `--output-format stream-json` (one JSON object per line) into the economy
    numbers plus a readable action log.

    Tolerant of partial/truncated streams by design: a budget-capped or timed-out run emits no
    final `result` event, but the `tool_use` events it did emit are the only surviving record of
    what the agent attempted. Malformed lines are skipped; every result-derived field degrades to
    None rather than raising.

    Token accounting deliberately matches /opt/runs/ccdf_costs.py — tokens_in = input +
    cache_creation + cache_read — so ccdf numbers stay comparable with the earlier ccdf baselines.
    Cache reads dominate Claude Code usage (22k cache-read vs 2.5k fresh input on a trivial
    prompt), so this is NOT comparable to the raw prompt-token counts the deepseek agents report;
    the component split stays available in the persisted claude_stream.jsonl.
    """
    actions: list = []
    info: dict = {"turns": None, "cost_usd": None, "is_error": None, "stop_reason": None,
                  "tokens_in": None, "tokens_out": None, "total_tokens": None,
                  "llm_calls": 0, "tool_calls": 0, "model_usage": {}, "rate_limited": False}
    for raw in (stream_text or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "system" and obj.get("subtype") == "init":
            actions.append(f"[init] session={str(obj.get('session_id', '?'))[:8]} "
                           f"cwd={obj.get('cwd', '?')}")
        elif kind == "assistant":
            info["llm_calls"] += 1
            for block in ((obj.get("message") or {}).get("content") or []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    info["tool_calls"] += 1
                    payload = json.dumps(block.get("input") or {})[:200]
                    actions.append(f"[{info['tool_calls']}] {block.get('name', '?')}: {payload}")
                elif block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        actions.append(f"    say: {text[:240]}")
        elif kind == "user":
            for block in ((obj.get("message") or {}).get("content") or []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tag = "ERR" if block.get("is_error") else "ok"
                    actions.append(f"      -> [{tag}] {str(block.get('content'))[:200]}")
        elif kind == "rate_limit_event":
            status = str((obj.get("rate_limit_info") or obj).get("status", ""))
            if status and status != "allowed":      # only flag ACTUAL throttling/rejection
                info["rate_limited"] = True
            actions.append(f"[rate-limit] status={status}")
        elif kind == "result":
            info["turns"] = obj.get("num_turns")
            info["cost_usd"] = obj.get("total_cost_usd")
            info["is_error"] = obj.get("is_error")
            info["stop_reason"] = obj.get("stop_reason")
            info["model_usage"] = obj.get("modelUsage") or {}
            usage = obj.get("usage")
            if isinstance(usage, dict):
                tin = ((usage.get("input_tokens") or 0)
                       + (usage.get("cache_creation_input_tokens") or 0)
                       + (usage.get("cache_read_input_tokens") or 0))
                tout = usage.get("output_tokens") or 0
                info["tokens_in"], info["tokens_out"] = tin, tout
                info["total_tokens"] = tin + tout
    info["actions"] = "\n".join(actions)
    return info
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/john/ratbench-runner-john && python3 -m pytest producers/tests/test_claudecode_stream.py -q`
Expected: PASS — 9 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/john/ratbench-runner-john
git add producers/_claudecode_helpers.py producers/tests/test_claudecode_stream.py
git commit -m "feat(producers): pure summarize_stream parser for Claude Code stream-json"
```

---

### Task 2: Persist the stream and populate `economy`

**Files:**
- Modify: `producers/claudecode_dockerfile.py` (add `_persist_stream` at module level; edit the CLI call at `:110-113` and the return at `:116`)
- Test: `producers/tests/test_claudecode_dockerfile.py` (append)

**Interfaces:**
- Consumes: `summarize_stream(stream_text) -> dict` from Task 1.
- Produces: `_persist_stream(out_dir: str, stdout: str, stderr: str) -> dict` returning an economy dict with keys `tokens_in`, `tokens_out`, `total_tokens`, `llm_calls`, `turns_used`, `cost_usd`, `tool_calls`, `agent_is_error`, `rate_limited`. Task 3 consumes `cost_usd` from this. Also writes `claude_stream.jsonl`, `claude_actions.log`, `claude_stderr.txt` under `out_dir`.

- [ ] **Step 1: Write the failing test**

Append to `producers/tests/test_claudecode_dockerfile.py`:

```python
# ── telemetry: the Claude Code stream is the ONLY record of cost/turns/trajectory ──────────
import json

from producers.claudecode_dockerfile import _persist_stream

_TELEMETRY_STREAM = "\n".join(json.dumps(o) for o in [
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "pip install -e ."}}]}},
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 7,
     "total_cost_usd": 1.25, "stop_reason": "end_turn",
     "usage": {"input_tokens": 100, "output_tokens": 20,
               "cache_creation_input_tokens": 30, "cache_read_input_tokens": 50}},
])


def test_persist_stream_writes_durable_artifacts(tmp_path):
    out_dir = str(tmp_path / "output" / "o" / "r")
    _persist_stream(out_dir, _TELEMETRY_STREAM, "some stderr")
    assert (tmp_path / "output" / "o" / "r" / "claude_stream.jsonl").read_text() \
        == _TELEMETRY_STREAM
    assert "Bash" in (tmp_path / "output" / "o" / "r" / "claude_actions.log").read_text()
    assert (tmp_path / "output" / "o" / "r" / "claude_stderr.txt").read_text() == "some stderr"


def test_persist_stream_returns_economy_in_write_env_packet_keys(tmp_path):
    econ = _persist_stream(str(tmp_path), _TELEMETRY_STREAM, "")
    assert econ["tokens_in"] == 180          # 100 + 30 + 50
    assert econ["tokens_out"] == 20
    assert econ["total_tokens"] == 200
    assert econ["turns_used"] == 7
    assert econ["cost_usd"] == 1.25
    assert econ["llm_calls"] == 1
    assert econ["tool_calls"] == 1


def test_persist_stream_never_raises_on_unwritable_dir(tmp_path):
    # Anti-vanish (design §1): the Dockerfile is the deliverable. A telemetry IO failure must
    # never fail the produce, so persistence is best-effort and still returns the parsed numbers.
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory")
    econ = _persist_stream(str(blocker / "nested"), _TELEMETRY_STREAM, "")
    assert econ["cost_usd"] == 1.25


def test_producer_passes_economy_through_to_produced_env(tmp_path):
    def _stub(repo, ctx, **kw):
        return {"dockerfile": "FROM python:3.11\nRUN git clone x /testbed\nRUN pip install pytest",
                "base_image": "python:3.11",
                "economy": {"tokens_in": 180, "tokens_out": 20, "turns_used": 7,
                            "cost_usd": 1.25, "llm_calls": 1}}

    env = ClaudeCodeDockerfileProducer(runner=_stub).produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "produced"
    assert env.economy["turns_used"] == 7
    assert env.economy["cost_usd"] == 1.25
    assert env.economy["tokens_in"] == 180
    assert env.economy["produce_s"] is not None      # still stamped by produce()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/john/ratbench-runner-john && python3 -m pytest producers/tests/test_claudecode_dockerfile.py -q`
Expected: FAIL — `ImportError: cannot import name '_persist_stream' from 'producers.claudecode_dockerfile'`

- [ ] **Step 3: Write minimal implementation**

In `producers/claudecode_dockerfile.py`, add `import os` to the module-level imports (the module currently has only `from __future__ import annotations` and `import time`; `os` and `subprocess` are imported *inside* `run_claudecode_dockerfile`, but `_persist_stream` is module-level and needs `os` there). Then add this function just above `def run_claudecode_dockerfile(...)`:

```python
def _persist_stream(out_dir: str, stdout: str, stderr: str) -> dict:
    """Write the agent's raw event stream + a readable action log under the repo's packet dir,
    and return the economy dict `write_env_packet` consumes.

    This stream is the ONLY record of what the ccdf agent cost and did — cost, turn count and
    trajectory cannot be reconstructed from any other artifact after the run, so it is written
    at produce time. Persistence is best-effort: an IO failure must never fail the produce (the
    Dockerfile is the deliverable, design §1), so the parsed numbers are returned regardless.
    """
    from producers._claudecode_helpers import summarize_stream   # stdlib-only, no RAT tree
    info = summarize_stream(stdout)
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "claude_stream.jsonl"), "w") as fh:
            fh.write(stdout or "")
        with open(os.path.join(out_dir, "claude_actions.log"), "w") as fh:
            fh.write(info.get("actions") or "(no parsed actions)")
        if stderr:
            with open(os.path.join(out_dir, "claude_stderr.txt"), "w") as fh:
                fh.write(stderr)
    except OSError:
        pass
    return {
        "tokens_in": info["tokens_in"], "tokens_out": info["tokens_out"],
        "total_tokens": info["total_tokens"], "llm_calls": info["llm_calls"],
        "turns_used": info["turns"], "cost_usd": info["cost_usd"],
        "tool_calls": info["tool_calls"], "agent_is_error": info["is_error"],
        "rate_limited": info["rate_limited"],
    }
```

Replace the CLI invocation block (`producers/claudecode_dockerfile.py:110-113`):

```python
        try:
            subprocess.run(claude_cmd, capture_output=True, text=True, timeout=ctx.timeout)
        except subprocess.TimeoutExpired:
            pass   # partial work may still have written Dockerfile.gen; try to pull it below
```

with:

```python
        try:
            proc = subprocess.run(claude_cmd, capture_output=True, text=True,
                                  timeout=ctx.timeout)
            stdout, stderr = _as_text(proc.stdout), _as_text(proc.stderr)
        except subprocess.TimeoutExpired as exc:
            # Partial work may still have written Dockerfile.gen; the partial stream is also the
            # only record of what the agent did before the wall, so keep both.
            stdout, stderr = _as_text(exc.stdout), _as_text(exc.stderr)
        economy = _persist_stream(out_dir, stdout, stderr)
```

and change the return (`:116`) from:

```python
        return {"dockerfile": text or None, "base_image": base_image, "economy": {}}
```

to:

```python
        return {"dockerfile": text or None, "base_image": base_image, "economy": economy}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/john/ratbench-runner-john && python3 -m pytest producers/tests/ -q`
Expected: PASS — the 7 pre-existing ccdf tests plus the 4 new ones, and no regressions elsewhere in `producers/tests/`.

- [ ] **Step 5: Commit**

```bash
cd /Users/john/ratbench-runner-john
git add producers/claudecode_dockerfile.py producers/tests/test_claudecode_dockerfile.py
git commit -m "fix(producers): persist Claude Code stream + populate economy (cost/turns/tokens)"
```

---

### Task 3: Thread `cost_usd` through the produce→measure contract

**Files:**
- Modify: `producers/base.py:291` (the `_meta.json` payload)
- Modify: `bench/bench/schema.py:68` (`MeasureRow`)
- Modify: `bench/bench/measure.py:175-178` (`base_row`)
- Modify: `bench/bench/metrics.py:87-101` (`compute_metrics`)
- Test: `bench/tests/bench/test_metrics_cost.py` (create)
- Test: `producers/tests/test_roundtrip.py` (append)

**Interfaces:**
- Consumes: `economy["cost_usd"]` from Task 2.
- Produces: `MeasureRow.cost_usd: float | None`, and `compute_metrics` keys
  `total_cost_usd`, `mean_cost_usd`, `cost_per_ebsr`, `cost_per_real_success`, `n_cost_reporting`.

- [ ] **Step 1: Write the failing test**

Create `bench/tests/bench/test_metrics_cost.py`:

```python
# bench/tests/bench/test_metrics_cost.py — USD aggregation (Claude Code reports actual cost).
from bench.metrics import compute_metrics
from bench.schema import MeasureRow


def _row(repo, *, cost=None, ebsr=True, pass_rate=1.0, status="executed"):
    return MeasureRow(agent="a", repo=repo, env_status="produced", build_ok=True,
                      executed=True, ebsr=ebsr, pass_rate=pass_rate, status=status,
                      collect_clean=True, cost_usd=cost)


def test_cost_aggregates_over_reporting_rows():
    m = compute_metrics([_row("o/a", cost=1.0), _row("o/b", cost=3.0)])
    assert m["total_cost_usd"] == 4.0
    assert m["mean_cost_usd"] == 2.0
    assert m["n_cost_reporting"] == 2


def test_cost_per_ebsr_and_per_real_success():
    # b is EBSR but below the 0.8 real-success bar, so it counts for cost_per_ebsr only.
    m = compute_metrics([_row("o/a", cost=1.0), _row("o/b", cost=3.0, pass_rate=0.5)])
    assert m["cost_per_ebsr"] == 2.0            # 4.0 / 2 ebsr rows
    assert m["cost_per_real_success"] == 4.0    # 4.0 / 1 real success


def test_rows_without_cost_are_excluded_not_zeroed():
    # A producer that reports no cost (dockeragent, repo2run) must not drag the mean to 0.
    m = compute_metrics([_row("o/a", cost=2.0), _row("o/b", cost=None)])
    assert m["mean_cost_usd"] == 2.0
    assert m["n_cost_reporting"] == 1


def test_no_cost_anywhere_reports_none_not_zero():
    m = compute_metrics([_row("o/a"), _row("o/b")])
    assert m["total_cost_usd"] is None
    assert m["mean_cost_usd"] is None
    assert m["cost_per_ebsr"] is None
    assert m["n_cost_reporting"] == 0
```

Append to `producers/tests/test_roundtrip.py`:

```python
def test_ccdf_economy_reaches_meta_json_and_measure_row(tmp_path):
    # The full produce->measure seam for the fields a ccdf cost analysis needs: economy ->
    # write_env_packet -> _meta.json -> harvest -> measure() base_row -> MeasureRow.
    import json
    import os

    from producers.base import ProducedEnv, RepoSpec, write_env_packet

    out_root = str(tmp_path / "output")
    env = ProducedEnv(repo=RepoSpec("o/r", "https://github.com/o/r"),
                      dockerfile="FROM python:3.11\nRUN pip install pytest",
                      status="produced", producer_name="claudecode-dockerfile",
                      economy={"tokens_in": 180, "tokens_out": 20, "total_tokens": 200,
                               "llm_calls": 3, "turns_used": 7, "cost_usd": 1.25,
                               "produce_s": 42.0})
    repo_dir = write_env_packet(out_root, env)
    with open(os.path.join(repo_dir, "_meta.json")) as fh:
        meta = json.load(fh)
    assert meta["tokens_in"] == 180 and meta["tokens_out"] == 20
    assert meta["llm_calls"] == 3 and meta["turns_used"] == 7
    assert meta["total_tokens"] == 200 and meta["produce_s"] == 42.0
    assert meta["cost_usd"] == 1.25
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```
cd /Users/john/ratbench-runner-john
python3 -m pytest bench/tests/bench/test_metrics_cost.py producers/tests/test_roundtrip.py -q
```
Expected: FAIL — `TypeError: MeasureRow.__init__() got an unexpected keyword argument 'cost_usd'`, and `KeyError: 'cost_usd'` in the roundtrip test.

- [ ] **Step 3: Write minimal implementation**

**3a.** `bench/bench/schema.py` — insert after `turns_used: int | None = None` (line 68):

```python
    cost_usd: float | None = None   # agent-reported spend (Claude Code total_cost_usd); None
                                    # for producers whose model API reports no cost
```

**3b.** `producers/base.py` — in `write_env_packet`'s payload, insert after `"total_tokens": economy.get("total_tokens"),` (line 291):

```python
        "cost_usd": economy.get("cost_usd"),
```

**3c.** `bench/bench/measure.py` — change `base_row` (lines 175-178) to:

```python
    base_row = dict(agent=agent, repo=repo, env_status=env.status,
                    tokens_in=m.get("tokens_in"), tokens_out=m.get("tokens_out"),
                    llm_calls=m.get("llm_calls"), turns_used=m.get("turns_used"),
                    cost_usd=m.get("cost_usd"),
                    produce_s=m.get("produce_s"), meta=dict(m))
```

**3d.** `bench/bench/metrics.py` — after the `tok_rows`/`tok_total` lines (87-88) add:

```python
    # Cost is reported directly by the agent (Claude Code's total_cost_usd), not derived from
    # tokens and a rate card — Claude Code mixes models within one run, so a single rate would
    # mis-price it. Rows from producers whose API reports no cost stay None and are EXCLUDED
    # from the denominator rather than counted as free.
    cost_rows = [r for r in prod if r.cost_usd is not None]
    cost_total = sum(r.cost_usd for r in cost_rows)
```

and inside the `out.update({...})` block add:

```python
        "total_cost_usd": _r(cost_total) if cost_rows else None,
        "mean_cost_usd": _r(cost_total / len(cost_rows)) if cost_rows else None,
        "cost_per_ebsr": _r(cost_total / n_ebsr) if (cost_rows and n_ebsr) else None,
        "cost_per_real_success": _r(cost_total / n_real) if (cost_rows and n_real) else None,
        "n_cost_reporting": len(cost_rows),
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```
cd /Users/john/ratbench-runner-john
python3 -m pytest bench/tests/bench producers/tests runner/tests -q
```
Expected: PASS — the 4 new cost tests, the new roundtrip test, and every pre-existing test. Watch specifically for `test_schema.py`, `test_metrics_*.py` and `test_roundtrip.py`: `cost_usd` is additive with a `None` default, so no existing assertion should change.

- [ ] **Step 5: Commit**

```bash
cd /Users/john/ratbench-runner-john
git add bench/bench/schema.py bench/bench/measure.py bench/bench/metrics.py producers/base.py \
        bench/tests/bench/test_metrics_cost.py producers/tests/test_roundtrip.py
git commit -m "feat(bench): thread agent-reported cost_usd through the produce->measure contract"
```

---

### Task 4: Vendor the runner image + fail-fast preflight

**Files:**
- Create: `docker/claude-runner.Dockerfile`
- Modify: `runner/cli.py` (add `_ensure_claude_runner`, call it at line 134 right after `native = _is_native_lane(...)`)
- Test: `runner/tests/test_claude_runner_image.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_ensure_claude_runner(model: str, repo_root: str = REPO_ROOT, runner=subprocess.run) -> None` — raises `SystemExit` when the image is required but cannot be built.

- [ ] **Step 1: Write the failing test**

Create `runner/tests/test_claude_runner_image.py`:

```python
# runner/tests/test_claude_runner_image.py — the claude-runner preflight.
#
# claude-runner:latest is a LOCAL-ONLY image (no registry: `docker pull` fails), so any
# `docker system prune` silently removes it. Without it the ccdf producer's anti-vanish guard
# turns all 50 repos into status="error" — which reads on disk as a completed run scoring zero.
# The preflight converts that silent zero into a working run or one loud failure.
import subprocess

import pytest

from runner.cli import _ensure_claude_runner


class _FakeRun:
    """Records docker invocations and replays canned return codes."""

    def __init__(self, *codes):
        self.codes = list(codes)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        rc = self.codes.pop(0) if self.codes else 0
        return subprocess.CompletedProcess(argv, rc)


def test_noop_for_non_claude_model(tmp_path):
    fake = _FakeRun()
    _ensure_claude_runner("dockeragent", repo_root=str(tmp_path), runner=fake)
    assert fake.calls == []


def test_noop_when_image_already_present(tmp_path):
    fake = _FakeRun(0)                       # docker image inspect -> 0
    _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake)
    assert len(fake.calls) == 1
    assert fake.calls[0][:3] == ["docker", "image", "inspect"]


def test_builds_when_image_missing(tmp_path):
    fake = _FakeRun(1, 0)                    # inspect -> 1 (missing), build -> 0
    _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake)
    assert len(fake.calls) == 2
    build = fake.calls[1]
    assert build[:2] == ["docker", "build"]
    assert "claude-runner:latest" in build
    assert any(a.endswith("docker/claude-runner.Dockerfile") for a in build)


def test_build_failure_aborts_the_run(tmp_path):
    # Loud beats silent: a failed build must stop the run BEFORE any repo produces, not let 50
    # repos each error out into a zero row.
    fake = _FakeRun(1, 1)                    # inspect -> missing, build -> fail
    with pytest.raises(SystemExit):
        _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake)


def test_also_guards_the_live_claudecode_lane(tmp_path):
    fake = _FakeRun(0)
    _ensure_claude_runner("claudecode", repo_root=str(tmp_path), runner=fake)
    assert len(fake.calls) == 1


def test_honors_claude_runner_image_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_RUNNER_IMAGE", "my-runner:v2")
    fake = _FakeRun(0)
    _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake)
    assert "my-runner:v2" in fake.calls[0]


def test_vendored_dockerfile_exists_and_installs_the_cli():
    # The recipe used to live only in the legacy /opt/harness tree; it must be in THIS repo so a
    # fresh box can build it.
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # runner/ -> repo root
    path = os.path.join(here, "docker", "claude-runner.Dockerfile")
    text = open(path).read()
    assert "@anthropic-ai/claude-code" in text
    assert "useradd" in text        # bypassPermissions is refused as root
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/john/ratbench-runner-john && python3 -m pytest runner/tests/test_claude_runner_image.py -q`
Expected: FAIL — `ImportError: cannot import name '_ensure_claude_runner' from 'runner.cli'`

- [ ] **Step 3: Write minimal implementation**

**4a.** Create `docker/claude-runner.Dockerfile` (vendored verbatim from `/opt/harness/docker/claude-runner.Dockerfile`, which exists only on that one box):

```dockerfile
# docker/claude-runner.Dockerfile
# The WORKBENCH image for the Claude Code lanes (`claudecode`, `claudecode-dockerfile`).
# This is where the agent explores and installs live; it is NOT the base of the Dockerfile the
# agent emits (that is CLAUDE_DOCKERFILE_BASE, default python:3.11) and it is never measured.
FROM python:3.11-slim

# System basics the agent commonly needs, plus Node for the CLI.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git build-essential ca-certificates sudo \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI.
RUN npm install -g @anthropic-ai/claude-code

# Non-root user: Claude Code refuses --permission-mode bypassPermissions as root.
# Passwordless sudo lets the agent install system-wide where a repo needs it.
RUN useradd -m -u 1000 -s /bin/bash agent \
    && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent \
    && chmod 0440 /etc/sudoers.d/agent

# NOTE: the image default user intentionally stays root. The producer selects the unprivileged
# `agent` user per-exec via `docker exec -u agent` for the Claude Code agent, while container
# setup steps (docker cp of the repo, chown) require root and run as the default user.
WORKDIR /testbed
```

**4b.** `runner/cli.py` — add after the `_is_native_lane` function definition:

```python
# Models whose lane execs the Claude Code CLI inside the claude-runner workbench image.
_CLAUDE_LANES = ("claudecode", "claudecode-dockerfile")


def _ensure_claude_runner(model: str, repo_root: str = REPO_ROOT, runner=subprocess.run) -> None:
    """Build the claude-runner workbench image if absent, BEFORE any repo runs.

    The image is local-only (no registry — `docker pull claude-runner` fails), so a routine
    `docker system prune` silently removes it. When it is gone the ccdf producer's anti-vanish
    guard turns every repo into status="error", which on disk is indistinguishable from a
    completed run that scored zero. Building it up front makes the failure loud and early.

    Idempotent: an existing image is a single `docker image inspect` and no build. `runner` is
    injectable so the preflight is unit-testable without Docker.
    """
    if model not in _CLAUDE_LANES:
        return
    tag = os.environ.get("CLAUDE_RUNNER_IMAGE", "claude-runner:latest")
    if runner(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
        return
    ctx = os.path.join(repo_root, "docker")
    dockerfile = os.path.join(ctx, "claude-runner.Dockerfile")
    print(f"[bench] {tag} missing — building from {dockerfile}", flush=True)
    rc = runner(["docker", "build", "-t", tag, "-f", dockerfile, ctx]).returncode
    if rc != 0:
        raise SystemExit(
            f"[bench] FATAL: could not build {tag} (rc={rc}). The {model} lane cannot run "
            f"without it; every repo would silently record status=\"error\".")
```

Then in `main()`, immediately after line 134 (`native = _is_native_lane(model, spec.measure)`), insert:

```python
    # Preflight BEFORE output_dir/manifest so a failure leaves no half-started run directory.
    _ensure_claude_runner(model)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/john/ratbench-runner-john && python3 -m pytest runner/tests -q`
Expected: PASS — 7 new tests plus the pre-existing `test_producer_dispatch.py`.

- [ ] **Step 5: Commit**

```bash
cd /Users/john/ratbench-runner-john
git add docker/claude-runner.Dockerfile runner/cli.py runner/tests/test_claude_runner_image.py
git commit -m "feat(runner): vendor claude-runner Dockerfile + fail-fast image preflight"
```

---

### Task 5: Document, deploy, and re-verify on the VM

**Files:**
- Modify: `README.md`
- No new tests (this task is deployment + live verification)

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces: a verified `claudecode-dockerfile` smoke run on the VM whose `_meta.json` carries non-null cost/tokens/turns and whose repo dir carries `claude_stream.jsonl`.

- [ ] **Step 1: Document the artifacts**

Add to `README.md` under the artifacts/variety section:

```markdown
### claudecode-dockerfile artifacts

Per repo, under `runs/<variety>/<run>/output/<owner>/<repo>/`:

| File | Contents |
| --- | --- |
| `claude_stream.jsonl` | Raw Claude Code `stream-json` events — the agent's full trajectory (one `tool_use` per action, `tool_result` per observation). The ccdf analogue of dockeragent's `react_trace.jsonl`. |
| `claude_actions.log` | Readable action log rendered from the stream. |
| `claude_stderr.txt` | CLI stderr (only when non-empty). |
| `_meta.json` | `cost_usd` (Claude Code's own `total_cost_usd`), `tokens_in`/`tokens_out`/`total_tokens`, `llm_calls`, `turns_used`. |

Cost is taken from Claude Code's reported `total_cost_usd`, NOT derived from tokens and a rate
card: Claude Code mixes models within a single run (a haiku for side tasks alongside the primary
model), so one rate would mis-price it. `tokens_in` includes cache-creation and cache-read tokens
(matching `ccdf_costs.py`), which makes it NOT directly comparable to the raw prompt-token counts
the deepseek-backed agents report — the component split is preserved in `claude_stream.jsonl`.

The `claudecode*` lanes need the local-only `claude-runner:latest` workbench image. `bench` builds
it automatically from `docker/claude-runner.Dockerfile` when missing, and aborts the run if the
build fails.
```

- [ ] **Step 2: Run the whole suite once before deploying**

Run: `cd /Users/john/ratbench-runner-john && python3 -m pytest bench/tests/bench producers/tests runner/tests -q`
Expected: PASS, no regressions.

- [ ] **Step 3: Commit and push the branch**

```bash
cd /Users/john/ratbench-runner-john
git add README.md
git commit -m "docs: ccdf telemetry artifacts + claude-runner preflight"
git push -u origin ccdf-telemetry
```

- [ ] **Step 4: Deploy to the VM**

The VM harness at `/opt/ratbench` is at `origin/main` (`4626e95`) and can `git fetch` (verified). Check out the branch there — NOT `main`, which would pull in the 10 undeployed multi-language commits:

```bash
ssh -o StrictHostKeyChecking=no root@167.233.64.96 \
  'cd /opt/ratbench && git fetch origin && git checkout ccdf-telemetry && git log --oneline -1'
```

Expected: HEAD is the `docs:` commit from Step 3.

- [ ] **Step 5: Re-run the single-repo smoke and verify telemetry lands**

`fastapi/typer` is the verified-good smoke repo (EBSR 1.0, ESSR 0.9963 on `4626e95`). The image is currently absent, so this also exercises the Task 4 preflight.

```bash
ssh -o StrictHostKeyChecking=no root@167.233.64.96 'cd /opt/ratbench && source env.sh && \
  CLAUDE_MAX_BUDGET_USD=2.0 ./run_bench.sh claudecode-dockerfile \
    --tier all --only fastapi/typer --llm sonnet \
    --repos-json /opt/ratbench/datasets/rat_python50.json --run-name ccdf-telemetry-smoke'
```

Then verify:

```bash
ssh -o StrictHostKeyChecking=no root@167.233.64.96 'R=$(ls -dt /opt/ratbench/runs/claudecode-dockerfile/ccdf-telemetry-smoke-* | head -1); \
  echo "--- preflight built the image? ---"; docker image inspect claude-runner:latest >/dev/null 2>&1 && echo PRESENT || echo MISSING; \
  echo "--- trajectory ---"; wc -l $R/output/fastapi/typer/claude_stream.jsonl; \
  head -20 $R/output/fastapi/typer/claude_actions.log; \
  echo "--- _meta.json economy ---"; python3 -c "import json;d=json.load(open(\"$R/output/fastapi/typer/_meta.json\"));print({k:d.get(k) for k in (\"cost_usd\",\"tokens_in\",\"tokens_out\",\"total_tokens\",\"llm_calls\",\"turns_used\",\"produce_s\")})"; \
  echo "--- metrics ---"; python3 -c "import json;m=json.load(open(\"$R/measure/metrics.json\"))[\"claudecode-dockerfile\"];print({k:m.get(k) for k in (\"EBSR\",\"ESSR\",\"total_cost_usd\",\"mean_cost_usd\",\"cost_per_ebsr\",\"mean_tokens\",\"mean_turns\",\"n_cost_reporting\")})"'
```

Expected: image PRESENT; `claude_stream.jsonl` non-empty with tool-use lines in `claude_actions.log`; `_meta.json` shows non-null `cost_usd`/`tokens_in`/`turns_used`; `metrics.json` shows `n_cost_reporting: 1`, non-null `mean_cost_usd` and `cost_per_ebsr`, and `EBSR: 1.0` / `ESSR ≈ 0.9963` unchanged from the pre-change smoke.

**Stop here if `EBSR`/`ESSR` differ from the baseline** — the telemetry change must be scoring-inert; a difference means something in the produce path changed behavior, not just observability.

- [ ] **Step 6: Commit nothing; record the verification**

No code change in this step. Paste the two JSON dicts from Step 5 into the run notes so the 50-repo launch has a known-good reference.

---

## Launch checklist (after Task 5 passes — not part of the plan's code changes)

- [ ] `CLAUDE_MAX_BUDGET_USD=5.0` — the $2 default truncates the large repos (Qiskit, PostHog, checkmk, azure-cli, baserow). The prior medlarge15 launcher used $5. Worst case 50 × $5 = $250.
- [ ] `--concurrency 2` — matches prior practice; forwarded to both produce and measure.
- [ ] Free disk ≥ 60 GB before launch (`df -h /`). The last run left 23 GB of build cache; `measure` removes each bench image but not the cache.
- [ ] Confirm no other job is running on the box (`ps aux | grep -E "benchmark.py|unified_bench"`).
- [ ] Resume is available if interrupted — `run_produced.json` per repo makes a re-launch skip completed repos.
- [ ] Full launch:
      `CLAUDE_MAX_BUDGET_USD=5.0 ./run_bench.sh claudecode-dockerfile --tier all --concurrency 2 --llm sonnet --repos-json /opt/ratbench/datasets/rat_python50.json --run-name ccdf-full50`

## Out of scope (deliberately)

- **The `RepoSpec.language` drop.** Already fixed locally in `6b4eaab`, undeployed. Irrelevant to `rat_python50` (all Python) and must not ride into this run — deploy it separately with its own multi-language validation.
- **`_meta.json`'s misleading `base_image`.** It records the workbench image (`claude-runner:latest`) rather than the emitted Dockerfile's `FROM` (`python:3.11`). Cosmetic; does not affect scoring. Fixing it changes a field other tooling may read.
- **Retro-fitting `agent_run_summary.json`.** `/opt/runs/ccdf_costs.py` already produces it from `claude_stream.jsonl` for the `bench/report/case_study.py` path, and will work as-is once the stream is persisted.
