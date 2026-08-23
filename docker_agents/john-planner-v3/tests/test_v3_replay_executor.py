"""Phase 4: run_v3's dep-emit phase has exactly ONE executor — fresh full-script
replay from base (Model B). Confirms the collapsed `_dep_emit_phase` body:

  - every cycle renders the WHOLE certified graph and replays it via
    `reset_to_base` + `run_install_script` (`orchestrator._binding_emit`), NOT
    `block_emit`/`emit_drain` (both removed from `_dep_emit_phase`; they
    survive only in `run_v1` / a future ablation entry point).
  - there is NO render-hash memoization/skip: every cycle does a real fresh
    replay from base, unconditionally. Each cycle must produce a fresh
    evidence bundle for `run_structured_repair`, so a byte-identical render
    is still replayed for real (caching is deferred to a future cached
    `docker build`, out of scope here).

The harness is a discover-task-only scenario (VERIFY_TEST_CMD always "fails")
so the scheduler never hands out a targeted obligation task — the task
branch's typed-repair path never fires, keeping this test's block_emit
assertion unambiguous.

Phase 5 (`orchestrator._run_discover_gate`): discover tasks now run the
deterministic VERIFY_TEST_CMD gate instead of `build_agent.run` (free text),
and — being a mechanical host check, not an LLM call — no longer spend
`_repair_turns`. So this 2-cycle harness runs to `max_cycles` rather than
exhausting the (now-untouched) LLM-repair budget; see
`tests/envstate/test_v3_task_branch.py` for the dedicated discover-gate
routing/evidence/give-up coverage.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

import src.envstate.block_emit as be
import src.envstate.depgraph_live as dl
from src.envstate.orchestrator import VERIFY_TEST_CMD, run_v3
from src.envstate.ledger import ActionLedger
from src.envstate.world_model import TaskReport, initial_map, merge_map
from src.sandbox import InstallResult
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Layer, Node, NodeType, State,
)


class _RecordingBuildAgent:
    """No-`.client` build agent — discover tasks route through the
    deterministic gate (`_run_discover_gate`), not `.run`/`.propose`, so this
    test never exercises either (kept only so run_v3's guards are satisfied)."""

    def __init__(self):
        self.tasks = []

    def run(self, task, sandbox_execute, ledger, step_offset=0, check=None, budget=None):
        self.tasks.append(task)
        return TaskReport(task_goal=task.goal, status="blocked", commands=(), learning="discover")

    def run_recipe(self, recipe, sandbox_execute, ledger, step_offset=0):
        return TaskReport(task_goal="r", status="done", commands=(), learning="ok")


class _NoopMaintainer:
    def update(self, world_map, report):
        return world_map


def _pkg_map():
    """WorldModelMap with one MISSING, reciped PACKAGE node (pip-installable)."""
    node = Node(
        id="pkg:requests", type=NodeType.PACKAGE, name="requests", version="2.31.0",
        layer=Layer.PIP, discovered_by=DiscoveredBy.STATIC_SCAN, state=State.MISSING,
        check_command="python3 -c 'import requests'",
    )
    base = initial_map(base_image="python:3.11-slim", workdir="/repo", language="python",
                       build_system="pip", repo_layout=())
    return merge_map(base, dep_graph=DepGraph().with_node(node))


def _build_harness():
    """Stateful fakes: exec_readonly reports the node MISSING until
    run_install_script flips `installed`. VERIFY_TEST_CMD always fails, so the
    scheduler stays on the discover-gate path every cycle (frontier empty
    once the node is satisfied, tests never "pass") — the discover gate is
    deterministic and does not spend `_repair_turns`, so the run terminates
    via `max_cycles` (not the LLM-turn budget), giving the test 2 full
    `_dep_emit_phase` invocations to observe."""
    state = {"installed": False}
    calls = {"reset": 0, "install": 0}

    def sandbox_execute(cmd):
        if cmd == VERIFY_TEST_CMD:
            return (False, "no tests ran")
        return (True, "ok")

    def exec_readonly(cmd):
        if "import requests" in cmd:
            return (0, "") if state["installed"] else (1, "ModuleNotFoundError")
        return (1, "")

    def reset_to_base():
        calls["reset"] += 1

    def run_install_script(script):
        calls["install"] += 1
        state["installed"] = True
        return InstallResult(rc=0, failing_command=None, lineno=None, stderr="")

    inputs = dict(
        build_agent=_RecordingBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_pkg_map(),
        ledger=ActionLedger(),
        sandbox_execute=sandbox_execute,
        max_cycles=2,
        exec_readonly=exec_readonly,
        enable_dep_emit=True,
        reset_to_base=reset_to_base,
        run_install_script=run_install_script,
    )
    return inputs, calls


def test_run_v3_uses_fresh_replay_each_cycle(monkeypatch):
    """Every cycle (both cycle 1 and cycle 2) does a real fresh replay: no
    memoization/skip, even though cycle 2's render is byte-identical to
    cycle 1's (graph + manual_blocks unchanged). reset_to_base and
    run_install_script are invoked once per _dep_emit_phase call — 2 calls
    total across the 2-cycle run — and block_emit/emit_drain are never
    called from run_v3's dep-emit phase.
    """
    block_calls = {"n": 0}
    drain_calls = {"n": 0}

    def _spy_block(*a, **k):
        block_calls["n"] += 1
        return be.block_emit(*a, **k)

    def _spy_drain(*a, **k):
        drain_calls["n"] += 1
        return dl.emit_drain(*a, **k)

    monkeypatch.setattr(be, "block_emit", _spy_block)
    monkeypatch.setattr(dl, "emit_drain", _spy_drain)

    inputs, calls = _build_harness()
    final_map, stop = run_v3(**inputs)

    # The scheduler never hands out a targeted obligation (frontier empties
    # after cycle 1's install; tests never "pass"), so it stays on the
    # discover-gate path every cycle. The discover gate is deterministic (no
    # LLM call), so it does not spend _repair_turns — the run exhausts
    # max_cycles=2 instead of the LLM-repair budget (Phase 5).
    assert stop == "max_cycles", f"expected max_cycles (discover gate spends no LLM budget), got {stop!r}"

    # Two real replays across the 2 cycles: no memoization, so cycle 2's
    # byte-identical re-render is STILL replayed for real (every cycle must
    # produce a fresh evidence bundle for run_structured_repair).
    assert calls["reset"] == 2, (
        f"expected a real replay every cycle (no memoization), got {calls['reset']} reset_to_base call(s)"
    )
    assert calls["install"] == 2, (
        f"expected a real replay every cycle (no memoization), got {calls['install']} run_install_script call(s)"
    )

    # block_emit/emit_drain are not part of run_v3's dep-emit executor anymore
    # (Phase 4) — the fresh-replay body (_binding_emit) is the sole executor.
    assert block_calls["n"] == 0, "block_emit must NOT run inside run_v3's dep-emit phase (Phase 4)"
    assert drain_calls["n"] == 0, "emit_drain must NOT run inside run_v3's dep-emit phase (Phase 4)"

    # The fresh-replay install actually certified the node.
    assert final_map.dep_graph.get("pkg:requests").state is State.SATISFIED, (
        "the fresh-replay install must have certified the node"
    )
