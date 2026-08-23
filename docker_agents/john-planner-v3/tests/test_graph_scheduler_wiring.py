"""Wiring test: verify the graph scheduler gates run_v1's decision.

Uses synthetic planner/build_agent/maintainer stubs (no Docker/LLM). Confirms:
  - run_v1  -> planner.decide drives the cycle (byte-identical to today)
  - run_v3  -> the scheduler drives; planner.decide is never called (run_v3
    has no planner param at all); a targeted obligation task with no
    build_agent.client gives up honestly (GIVEUP_CONFIG) rather than
    silently falling back to build_agent.run — Task 5a removed run_v3's
    free-text fallback entirely (see tests/envstate/test_v3_task_branch.py
    for the full 3-way-dispatch coverage)
  - run_v1  -> the deterministic emit_drain runs as a prefix (Phase 4: run_v3
    has no emit_drain branch at all — it has exactly one executor, fresh
    full-script replay; Phase 9 removed the vestigial deprecation-raise
    flags/tests that used to pin this)
  - run_v3  -> a sufficiency-stuck run gives up (does not run to max_cycles)
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from src.envstate.ledger import ActionLedger
from src.envstate.orchestrator import run_v1, run_v3
from src.envstate.world_model import (
    PlannerDecision,
    Task,
    TaskReport,
    WorldModelMap,
    initial_map,
    merge_map,
)
from src.sandbox import InstallResult
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Layer, Node, NodeType, State,
)


# run_v3 is fresh-replay-only (Phase 4): reset_to_base/run_install_script are
# mandatory. These fixtures build no-op fakes so tests unrelated to the
# install-executor mechanics don't need a real sandbox — the run_v3 fakes in
# this file target the graph-scheduler decision loop, not the emit executor.
def _noop_reset_to_base():
    pass


def _noop_run_install_script(script):
    return InstallResult(rc=0, failing_command=None, lineno=None, stderr="")


# ── Stubs ────────────────────────────────────────────────────────────────────

class _QueuePlanner:
    def __init__(self, decisions):
        self._queue = list(decisions)
        self.called = False

    def decide(self, world_map):
        self.called = True
        assert self._queue, "_QueuePlanner.decide called more times than expected"
        return self._queue.pop(0)


class _RecordingBuildAgent:
    """Records the task/check it receives; returns a blocked report."""
    def __init__(self):
        self.tasks = []
        self.checks = []

    def run(self, task, sandbox_execute, ledger, step_offset=0, check=None, budget=None):
        self.tasks.append(task)
        self.checks.append(check)
        return TaskReport(task_goal="task", status="blocked", commands=(), learning="blocked")

    def run_recipe(self, recipe, sandbox_execute, ledger, step_offset=0):
        return TaskReport(task_goal="recipe", status="done", commands=(), learning="ok")


class _NoopMaintainer:
    def update(self, world_map, report):
        return world_map


def _task() -> Task:
    return Task(goal="install deps", done_when="pip exits 0", layer="deps", facts=())


def _sandbox_ok(cmd):
    return True, "ok"


def _sandbox_fail(cmd):
    return False, "boom"


def _missing_node_map() -> WorldModelMap:
    """A WorldModelMap carrying a single MISSING package node with a check_command."""
    node = Node(
        id="pkg:requests",
        type=NodeType.PACKAGE,
        name="requests",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.STATIC_SCAN,
        state=State.MISSING,
        check_command="python -c 'import requests'",
    )
    base = initial_map(
        base_image="python:3.11",
        workdir="/repo",
        language="python",
        build_system="pip",
        repo_layout=(),
    )
    return merge_map(base, dep_graph=DepGraph().with_node(node))


def _no_check_node_map() -> WorldModelMap:
    """A WorldModelMap whose node has NO check_command (frontier always empty)."""
    node = Node(
        id="pkg:mystery",
        type=NodeType.PACKAGE,
        name="mystery",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.STATIC_SCAN,
        state=State.MISSING,
        check_command=None,
    )
    base = initial_map(
        base_image="python:3.11",
        workdir="/repo",
        language="python",
        build_system="pip",
        repo_layout=(),
    )
    return merge_map(base, dep_graph=DepGraph().with_node(node))


# ── 1. flag OFF: planner drives ──────────────────────────────────────────────

def test_flag_off_planner_drives():
    planner = _QueuePlanner([PlannerDecision(action="done")])
    final_map, stop = run_v1(
        planner=planner,
        build_agent=_RecordingBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_missing_node_map(),
        ledger=ActionLedger(),
        sandbox_execute=_sandbox_ok,
        max_cycles=1,
    )
    assert planner.called, "run_v1 must consult planner.decide"
    assert stop == "planner_done"


# ── 2. flag ON: scheduler drives, no free-text fallback ──────────────────────
#
# Historically this test asserted that a targeted obligation task with no
# build_agent.client fell back to build_agent.run (free-text path) — the old
# run_v3 dispatch condition silently downgraded when `.client` was absent.
# Task 5a (orchestrator.py task-dispatch consolidation) removed that fallback
# entirely: run_v3 now has ZERO free-text mutation. A target-bearing task
# with no client (or no exec_readonly) gives up honestly via GIVEUP_CONFIG
# instead. build_agent.run is asserted NEVER called — this is the correct
# updated pin for "scheduler drives" (there is no planner param on run_v3 to
# begin with, so "planner untouched" was always structurally guaranteed).

def test_flag_on_scheduler_gives_up_without_client():
    build_agent = _RecordingBuildAgent()
    final_map, stop = run_v3(
        build_agent=build_agent,
        maintainer=_NoopMaintainer(),
        initial_world_map=_missing_node_map(),
        ledger=ActionLedger(),
        sandbox_execute=_sandbox_ok,
        max_cycles=1,
        exec_readonly=lambda cmd: (1, "missing"),   # keep node MISSING -> frontier non-empty
        enable_dep_emit=True,
        reset_to_base=_noop_reset_to_base,
        run_install_script=_noop_run_install_script,
    )
    assert stop == "planner_giveup", (
        f"targeted obligation task with no build_agent.client must give up "
        f"honestly (GIVEUP_CONFIG), got {stop!r}"
    )
    assert build_agent.tasks == [], (
        "build_agent.run must never be called from run_v3 (Task 5a removed the "
        "free-text fallback entirely)"
    )


# ── 3. v1's deterministic drain runs as a prefix (unchanged) ────────────────
#
# Spec: docs/superpowers/specs/2026-06-26-unified-executor-loop-delta.md §0
#
# Phase 4 (fresh-replay-only run_v3): the "emit_drain under the flag" half of
# this test is gone — run_v3 has exactly one executor (fresh full-script
# replay) and never had an emit_drain branch. Phase 9 removed the vestigial
# enable_script_materialization/enable_binding_install deprecation-raise
# flags (and tests/test_v3_block_emit_wiring.py, which existed solely to pin
# that raise) entirely. Only the v1 half survives, renamed to make the split
# explicit.

def test_v1_drain_runs_as_prefix(monkeypatch):
    """emit_drain MUST run under run_v1's dep_emit phase (emit-prefix path).

    Rationale: the batch drain handles all reciped/emittable nodes deterministically
    before the LLM turn; the LLM only receives the non-emittable residual.
    See emit-prefix-plan.md Edit A and spec §0.
    """
    calls = {"n": 0}
    import src.envstate.depgraph_live as live
    real_drain = live.emit_drain

    def _spy(*args, **kwargs):
        calls["n"] += 1
        return real_drain(*args, **kwargs)

    monkeypatch.setattr(live, "emit_drain", _spy)

    run_v1(
        planner=_QueuePlanner([PlannerDecision(action="done")]),
        build_agent=_RecordingBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_missing_node_map(),
        ledger=ActionLedger(),
        sandbox_execute=_sandbox_ok,
        max_cycles=1,
        exec_readonly=lambda cmd: (1, "missing"),
        enable_dep_emit=True,
    )
    assert calls["n"] >= 1, "emit_drain MUST run with dep_emit on in run_v1"


# ── 4. stuck -> giveup ───────────────────────────────────────────────────────

def test_stuck_yields_giveup_before_max_cycles():
    final_map, stop = run_v3(
        build_agent=_RecordingBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_no_check_node_map(),
        ledger=ActionLedger(),
        sandbox_execute=_sandbox_fail,         # run_tests() stays red
        max_cycles=4,
        exec_readonly=lambda cmd: (1, ""),     # certify reveals nothing new
        enable_dep_emit=True,
        reset_to_base=_noop_reset_to_base,
        run_install_script=_noop_run_install_script,
    )
    assert stop == "planner_giveup", f"expected stuck->giveup, got {stop!r}"


def test_stuck_path_fires_on_cycle_callback():
    """GIVEUP_STUCK must invoke on_cycle for the final cycle (telemetry parity).

    The pre-split monolith fired on_cycle on this path; the v3 arm was missing
    the callback, so the stuck cycle was absent from the trace.  This test pins
    the fix: on_cycle is called at least once when run_v3 gives up due to
    two consecutive discover rounds with no new obligations.
    """
    calls: list[tuple] = []

    final_map, stop = run_v3(
        build_agent=_RecordingBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_no_check_node_map(),
        ledger=ActionLedger(),
        sandbox_execute=_sandbox_fail,
        max_cycles=4,
        exec_readonly=lambda cmd: (1, ""),
        enable_dep_emit=True,
        on_cycle=lambda *a: calls.append(a),
        reset_to_base=_noop_reset_to_base,
        run_install_script=_noop_run_install_script,
    )
    assert stop == "planner_giveup"
    assert calls, "on_cycle must be called on the GIVEUP_STUCK path"
    # The final entry must correspond to a 'task' decision (discover task = stuck sentinel)
    final_cycle_num, _map, final_decision, _report = calls[-1]
    assert final_decision.action == "task", (
        f"stuck path decision must be 'task', got {final_decision.action!r}"
    )
