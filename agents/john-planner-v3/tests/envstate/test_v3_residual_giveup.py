"""Integration tests for the handout-immune residual-stall giveup (design:
residual-node-drop.md, Part B) plus the frontier-drop (Part A) it depends on.

Harness modeled on tests/envstate/test_v3_no_progress_giveup.py (empty-graph
map, _noop_reset_to_base, _ok_run_install_script, a build_agent with a
non-None .client so the task branch never downgrades to GIVEUP_CONFIG).

IT1 — a FRESH phantom obligation is re-minted every cycle (defeating part
(a)'s per-id exclusion) and the VERIFY_TEST_CMD failure signature is unique
every cycle (defeating the existing GIVEUP_NO_PROGRESS detector, which reads
outcome_signature). Only the new handout-immune _residual_stall counter can
converge this: it must fire GIVEUP_RESIDUAL at cycle RESIDUAL_GIVEUP_CYCLES
(3), never spending a repair turn (build_agent.propose is never called).

IT2 — the same fresh-id harness, but every cycle's failure is a genuine
ModuleNotFoundError (a different module name each cycle, so the gate keeps
moving) -> _repair_or_route routes ENVIRONMENT and calls (a monkeypatched)
run_structured_repair every cycle -> _cycle_had_env_repair resets the
counter every cycle -> the run must reach max_cycles, proving a real,
progressing repair is never cut off.

IT3 — a single STABLE phantom node, seeded once in a real dep_graph, driven
through the REAL next_decision (no monkeypatch) so part (a)'s exclusion is
exercised directly: handed out once, diagnosed RESIDUAL, dropped from the
frontier -> the pre-existing GIVEUP_STUCK detector (now un-starved) fires
well before max_cycles.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

import src.envstate.graph_scheduler as gs_module
import src.envstate.orchestrator as orch
from src.envstate import orchestrator
from src.envstate.constants import RESIDUAL_GIVEUP_CYCLES
from src.envstate.ledger import ActionLedger
from src.envstate.repair_loop import RepairOutcome
from src.envstate.world_model import PlannerDecision, Task, initial_map, merge_map
from src.sandbox import InstallResult
from python_deps.depgraph.schema import DepGraph, DiscoveredBy, Layer, Node, NodeType, State


class _FakeClient:
    """Non-None sentinel for the ``getattr(build_agent, "client", None)`` guard."""


class _RecordingBuildAgent:
    def __init__(self):
        self.client = _FakeClient()
        self.model = "fake-model"
        self.run_calls = 0
        self.propose_calls = 0

    def run(self, task, sandbox_execute, ledger, step_offset=0, check=None, budget=None):
        self.run_calls += 1
        raise AssertionError("build_agent.run must never be called by run_v3")

    def propose(self, scope, exec_readonly=None, **kwargs):
        self.propose_calls += 1
        return None


class _NoopMaintainer:
    def update(self, world_map, report):
        return world_map


def _empty_graph_map():
    """WorldModelMap with a real (empty) dep_graph — enough for _dep_emit_phase
    to perform a genuine fresh replay every cycle, but with no nodes at all, so
    the main-loop's own _repair_or_route call site (_dep_emit_phase) never
    fires; only the task-branch site, driven by the faked obligation decision
    below, does."""
    base = initial_map(
        base_image="python:3.11-slim", workdir="/repo", language="python",
        build_system="pip", repo_layout=(),
    )
    return merge_map(base, dep_graph=DepGraph())


def _single_tool_graph_map():
    """WorldModelMap seeded with one real MISSING tool:less node — actionable
    (no deps, non-emittable: no chosen_fix) so it lands on scheduler_frontier
    and the REAL next_decision hands it out."""
    base = initial_map(
        base_image="python:3.11-slim", workdir="/repo", language="python",
        build_system="pip", repo_layout=(),
    )
    graph = DepGraph().with_node(Node(
        id="tool:less", type=NodeType.TOOL, name="less", layer=Layer.TOOLCHAIN,
        discovered_by=DiscoveredBy.RUNTIME, state=State.MISSING,
        check_command="command -v less",
    ))
    return merge_map(base, dep_graph=graph)


def _fresh_phantom_decision_factory():
    """next_decision replacement: a BRAND-NEW obligation id every call — the
    scheduler never repeats an id, so part (a)'s per-id _residual_ids
    exclusion can never converge this on its own (isolates the counter)."""
    calls = {"n": 0}

    def _decision(*_args, **_kwargs):
        calls["n"] += 1
        node_id = f"tool:phantom_{calls['n']}"
        task = Task(
            goal=f"install {node_id}", done_when=f"command -v phantom_{calls['n']}",
            layer="system", facts=(), target_node_ids=(node_id,),
        )
        return PlannerDecision(action="task", task=task), node_id

    return _decision


def _noop_reset_to_base() -> None:
    pass


def _ok_run_install_script(script: str) -> InstallResult:
    return InstallResult(rc=0, failing_command=None, lineno=None, stderr="")


def _base_inputs(sandbox_execute, initial_world_map, *, max_cycles: int,
                  build_agent=None, on_cycle=None) -> dict:
    return dict(
        build_agent=build_agent or _RecordingBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=initial_world_map,
        ledger=ActionLedger(),
        sandbox_execute=sandbox_execute,
        max_cycles=max_cycles,
        exec_readonly=lambda cmd: (1, ""),
        enable_dep_emit=True,
        reset_to_base=_noop_reset_to_base,
        run_install_script=_ok_run_install_script,
        on_cycle=on_cycle,
    )


def test_fresh_phantom_residual_churn_gives_up_at_k_never_repairs(monkeypatch):
    """IT1: a fresh phantom every cycle + an unstable failing signature must
    give up cleanly at RESIDUAL_GIVEUP_CYCLES (3), NOT run to max_cycles (12),
    and must never spend a repair turn (build_agent.propose is never called)."""
    monkeypatch.setattr(gs_module, "next_decision", _fresh_phantom_decision_factory())

    def _unexpected_repair(graph, failed_id, bundle, cycle, **kwargs):
        raise AssertionError(
            "run_structured_repair must never be called for a RESIDUAL diagnosis"
        )

    monkeypatch.setattr(orch, "run_structured_repair", _unexpected_repair)

    # A unique failing signature every cycle (defeats GIVEUP_NO_PROGRESS,
    # proving the new counter converges regardless of signature stability).
    counter = {"n": 0}

    def sandbox_execute(cmd):
        if cmd == orchestrator.VERIFY_TEST_CMD:
            counter["n"] += 1
            return (
                False,
                f"FAILED tests/t.py::test_{counter['n']} - AssertionError\n"
                f"tests/t.py:{counter['n']}: AssertionError\n=== 1 failed in 0.10s ==="
            )
        return (True, "ok")

    build_agent = _RecordingBuildAgent()
    cycles_seen: list[int] = []
    inputs = _base_inputs(
        sandbox_execute, _empty_graph_map(), max_cycles=12,
        build_agent=build_agent,
        on_cycle=lambda cycle, *_a: cycles_seen.append(cycle),
    )

    final_map, stop = orchestrator.run_v3(**inputs)

    assert stop == "planner_giveup", (
        f"expected an honest residual giveup, got {stop!r}"
    )
    assert max(cycles_seen) == RESIDUAL_GIVEUP_CYCLES == 3, (
        f"expected the giveup to fire at cycle {RESIDUAL_GIVEUP_CYCLES}, "
        f"got cycles {cycles_seen!r}"
    )
    assert build_agent.propose_calls == 0, (
        "a RESIDUAL diagnosis must never spend a repair turn"
    )
    assert build_agent.run_calls == 0


def test_real_env_repair_resets_residual_counter_never_cut_off(monkeypatch):
    """IT2: the same fresh-id harness, but every cycle's failure is a genuine
    (moving) ModuleNotFoundError -> routes ENVIRONMENT -> run_structured_repair
    runs every cycle -> _cycle_had_env_repair resets the residual counter every
    cycle -> the run must reach max_cycles, never GIVEUP_RESIDUAL."""
    monkeypatch.setattr(gs_module, "next_decision", _fresh_phantom_decision_factory())

    def _fake_repair(graph, failed_id, bundle, cycle, **kwargs):
        return RepairOutcome(
            graph=graph, still_failing_id=None, manual_blocks=(),
            known_invalid=frozenset(), turns_spent=0, budget_exhausted=False,
        )

    monkeypatch.setattr(orch, "run_structured_repair", _fake_repair)

    counter = {"n": 0}

    def sandbox_execute(cmd):
        if cmd == orchestrator.VERIFY_TEST_CMD:
            counter["n"] += 1
            return (
                False,
                f"ModuleNotFoundError: No module named 'mod_{counter['n']}'"
            )
        return (True, "ok")

    cycles_seen: list[int] = []
    inputs = _base_inputs(
        sandbox_execute, _empty_graph_map(), max_cycles=5,
        on_cycle=lambda cycle, *_a: cycles_seen.append(cycle),
    )

    final_map, stop = orchestrator.run_v3(**inputs)

    assert stop == "max_cycles", (
        f"a real, progressing env repair must never be cut off, got {stop!r}"
    )
    assert max(cycles_seen) == 5


def test_single_stable_phantom_converges_via_frontier_drop(monkeypatch):
    """IT3: using the REAL next_decision (no monkeypatch), a single stable
    MISSING tool:less node is handed out once, diagnosed RESIDUAL, and
    dropped from the frontier (part a) -> the pre-existing GIVEUP_STUCK
    detector (now un-starved, since the scheduler stops re-handing the node)
    converges well before max_cycles. The failing VERIFY_TEST_CMD signature
    changes every cycle (unstable), so only part (a) + _sched_stuck can be
    responsible for convergence — not GIVEUP_NO_PROGRESS."""
    # No monkeypatch of gs_module.next_decision: this exercises the REAL
    # graph_scheduler.next_decision(..., residual_ids=...) wiring end to end.

    counter = {"n": 0}

    def sandbox_execute(cmd):
        if cmd == orchestrator.VERIFY_TEST_CMD:
            counter["n"] += 1
            return (
                False,
                f"FAILED tests/t.py::test_{counter['n']} - AssertionError\n"
                f"tests/t.py:{counter['n']}: AssertionError\n=== 1 failed in 0.10s ==="
            )
        return (True, "ok")

    cycles_seen: list[int] = []
    targeted_cycles: list[int] = []

    def _on_cycle(cycle, current_map, decision, report):
        cycles_seen.append(cycle)
        task = getattr(decision, "task", None)
        if getattr(task, "target_node_ids", None) == ("tool:less",):
            targeted_cycles.append(cycle)

    inputs = _base_inputs(
        sandbox_execute, _single_tool_graph_map(), max_cycles=12,
        on_cycle=_on_cycle,
    )

    final_map, stop = orchestrator.run_v3(**inputs)

    assert stop == "planner_giveup", (
        f"expected an honest giveup once the residual node is excluded, got {stop!r}"
    )
    assert max(cycles_seen) < 12, (
        f"expected convergence well before max_cycles, got cycles {cycles_seen!r}"
    )
    assert targeted_cycles == [1], (
        "expected tool:less to be handed out exactly once (cycle 1), then "
        f"excluded from the frontier by part (a); got {targeted_cycles!r}"
    )


def test_verified_pass_fast_terminates_over_predicted_frontier_node():
    """Fast-termination (design: fast-termination) — the complement of IT3.

    Same single stable MISSING tool:less node on the frontier (it never
    certifies: exec_readonly returns rc=1), driven through the REAL
    next_decision. But this time the VERIFIED suite PASSES at cycle 1. The loop
    must declare planner_done at cycle 1 — a node still MISSING while tests pass
    WITHOUT it is an over-prediction, not a requirement — instead of handing
    tool:less out attempt_cap (3) times before re-checking tests (the pre-fix
    behavior would reach DONE only at cycle ~4 after burning a repair turn per
    handout). No repair turn is spent; the legacy build path is never used."""
    def sandbox_execute(cmd):
        if cmd == orchestrator.VERIFY_TEST_CMD:
            return (True, "1 passed in 0.01s")
        return (True, "ok")

    build_agent = _RecordingBuildAgent()
    cycles_seen: list[int] = []
    inputs = _base_inputs(
        sandbox_execute, _single_tool_graph_map(), max_cycles=12,
        build_agent=build_agent,
        on_cycle=lambda cycle, *_a: cycles_seen.append(cycle),
    )

    final_map, stop = orchestrator.run_v3(**inputs)

    assert stop == "planner_done", (
        f"a verified test pass must fast-terminate to DONE, got {stop!r}"
    )
    assert max(cycles_seen) == 1, (
        f"expected DONE at cycle 1 (no attempt_cap churn), got cycles {cycles_seen!r}"
    )
    assert build_agent.propose_calls == 0, (
        "fast-termination must short-circuit BEFORE any repair turn is spent"
    )
    assert build_agent.run_calls == 0
