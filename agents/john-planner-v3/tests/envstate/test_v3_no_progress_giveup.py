"""Integration tests for the no-progress detector (design: residual-giveup-fix.md
Part B): run_v3 must give up honestly (``GIVEUP_NO_PROGRESS`` -> ``planner_giveup``)
once the VERIFIED test-gate outcome signature is an identical FAILURE for
``NO_PROGRESS_CYCLES`` (3) consecutive cycles — the missing invariant that
``_sched_stuck`` cannot see because a targeted-obligation hand-out resets it
every cycle (the "phantom-obligation churn to a watchdog kill" failure mode).

Harness modeled on tests/envstate/test_v3_task_branch.py (patch next_decision
on gs_module, run_structured_repair on orch) + a REAL (empty) dep_graph so
_dep_emit_phase performs a real fresh replay every cycle (bumps
_container_gen -> a fresh VERIFY_TEST_CMD sample every cycle, mirroring
tests/test_v3_test_gate_dedup.py's Model-B replay pattern). The graph is
intentionally EMPTY (no nodes) so _binding_emit never reports a failed/
unsatisfied node -> the main-loop's own _repair_or_route call site
(_dep_emit_phase) never fires, isolating the task-branch repair call driven by
the faked next_decision (an obligation task with target_node_ids set every
cycle, chosen != None every cycle -> _sched_stuck is reset every cycle,
reproducing the phantom-churn dynamics the PRIMARY detector must catch on its
own).
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
from src.envstate.constants import NO_PROGRESS_CYCLES
from src.envstate.ledger import ActionLedger
from src.envstate.repair_loop import RepairOutcome
from src.envstate.world_model import PlannerDecision, Task, initial_map, merge_map
from src.sandbox import InstallResult
from python_deps.depgraph.schema import DepGraph


class _FakeClient:
    """Non-None sentinel for the ``getattr(build_agent, "client", None)`` guard."""


class _RecordingBuildAgent:
    def __init__(self):
        self.client = _FakeClient()
        self.model = "fake-model"
        self.run_calls = 0

    def run(self, task, sandbox_execute, ledger, step_offset=0, check=None, budget=None):
        self.run_calls += 1
        raise AssertionError("build_agent.run must never be called by run_v3")

    def propose(self, scope, exec_readonly=None, **kwargs):
        return None


class _NoopMaintainer:
    def update(self, world_map, report):
        return world_map


def _empty_graph_map():
    """WorldModelMap with a real (empty) dep_graph — enough for _dep_emit_phase
    to perform a genuine fresh replay every cycle (bumping _container_gen), but
    with no nodes at all, so _binding_emit never has a failed/unsatisfied node
    (the main-loop repair site never fires; only the task-branch site, driven
    by the faked obligation decision below, does)."""
    base = initial_map(
        base_image="python:3.11-slim", workdir="/repo", language="python",
        build_system="pip", repo_layout=(),
    )
    return merge_map(base, dep_graph=DepGraph())


def _obligation_decision(*_args, **_kwargs):
    """next_decision replacement: an obligation task targeting a node id every
    cycle, chosen='syslib:x' every cycle (never None) -> _sched_stuck is reset
    every cycle by the orchestrator's own `if chosen is not None: _sched_stuck = 0`
    -- exactly the dynamic that starves _sched_stuck during a phantom-obligation
    churn (design Part A.1) -- so this harness isolates the PRIMARY detector."""
    task = Task(
        goal="install syslib:x", done_when="ldconfig -p | grep -q x",
        layer="system", facts=(), target_node_ids=("syslib:x",),
    )
    return PlannerDecision(action="task", task=task), "syslib:x"


def _noop_reset_to_base() -> None:
    pass


def _ok_run_install_script(script: str) -> InstallResult:
    return InstallResult(rc=0, failing_command=None, lineno=None, stderr="")


def _base_inputs(sandbox_execute, *, max_cycles: int, on_cycle=None) -> dict:
    return dict(
        build_agent=_RecordingBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_empty_graph_map(),
        ledger=ActionLedger(),
        sandbox_execute=sandbox_execute,
        max_cycles=max_cycles,
        exec_readonly=lambda cmd: (1, ""),
        enable_dep_emit=True,
        reset_to_base=_noop_reset_to_base,
        run_install_script=_ok_run_install_script,
        on_cycle=on_cycle,
    )


def test_residual_churn_gives_up_cleanly_at_k_not_max_cycles(monkeypatch):
    """A stable, non-AssertionError failure (RuntimeError — routes AMBIGUOUS,
    not RESIDUAL, so run_structured_repair *is* invoked every cycle: repair
    activity is real) that never moves must give up at cycle NO_PROGRESS_CYCLES
    (3), NOT run_to max_cycles (12)."""
    monkeypatch.setattr(gs_module, "next_decision", _obligation_decision)

    repair_calls = {"n": 0}

    def _fake_repair(graph, failed_id, bundle, cycle, **kwargs):
        repair_calls["n"] += 1
        return RepairOutcome(
            graph=graph, still_failing_id=None, manual_blocks=(),
            known_invalid=frozenset(), turns_spent=1, budget_exhausted=False,
        )

    monkeypatch.setattr(orch, "run_structured_repair", _fake_repair)

    _STABLE_FAIL = (
        "FAILED tests/t.py::test_x - RuntimeError\n=== 1 failed in 0.10s ==="
    )

    def sandbox_execute(cmd):
        if cmd == orchestrator.VERIFY_TEST_CMD:
            return (False, _STABLE_FAIL)
        return (True, "ok")

    cycles_seen: list[int] = []
    inputs = _base_inputs(
        sandbox_execute, max_cycles=12,
        on_cycle=lambda cycle, *_a: cycles_seen.append(cycle),
    )

    final_map, stop = orchestrator.run_v3(**inputs)

    assert stop == "planner_giveup", (
        f"expected an honest no-progress giveup, got {stop!r}"
    )
    assert max(cycles_seen) == NO_PROGRESS_CYCLES == 3, (
        f"expected the giveup to fire at cycle {NO_PROGRESS_CYCLES}, "
        f"got cycles {cycles_seen!r}"
    )
    assert repair_calls["n"] == 2, (
        "expected repair to have run on cycles 1-2 (proving real repair "
        f"activity happened) before cycle 3's giveup, got {repair_calls['n']}"
    )
    assert inputs["build_agent"].run_calls == 0, (
        "build_agent.run must never be called by run_v3"
    )


def test_real_progress_is_never_cut_off(monkeypatch):
    """A gate that keeps moving (a DIFFERENT failing test every cycle) must
    never trip the no-progress detector — the run must go the distance to
    max_cycles, proving a genuinely-progressing suite is never falsely stalled."""
    monkeypatch.setattr(gs_module, "next_decision", _obligation_decision)

    def _fake_repair(graph, failed_id, bundle, cycle, **kwargs):
        # turns_spent=0: this test isolates the SIGNATURE-changes-every-cycle
        # behavior from the separate, unrelated _repair_turns/GIVEUP_BUDGET
        # bound (which would otherwise exhaust — by construction, budget seeds
        # from max_cycles — on the very last cycle of an N-cycle run that
        # spends 1 turn/cycle, masking the max_cycles assertion below).
        return RepairOutcome(
            graph=graph, still_failing_id=None, manual_blocks=(),
            known_invalid=frozenset(), turns_spent=0, budget_exhausted=False,
        )

    monkeypatch.setattr(orch, "run_structured_repair", _fake_repair)

    state = {"n": 0}

    def sandbox_execute(cmd):
        if cmd == orchestrator.VERIFY_TEST_CMD:
            state["n"] += 1
            return (
                False,
                f"FAILED tests/t.py::test_{state['n']} - RuntimeError\n"
                f"=== 1 failed in 0.10s ===",
            )
        return (True, "ok")

    cycles_seen: list[int] = []
    inputs = _base_inputs(
        sandbox_execute, max_cycles=5,
        on_cycle=lambda cycle, *_a: cycles_seen.append(cycle),
    )

    final_map, stop = orchestrator.run_v3(**inputs)

    assert stop == "max_cycles", (
        f"a moving gate must never be falsely stalled, got {stop!r}"
    )
    assert max(cycles_seen) == 5, (
        f"expected the run to go the full distance past K={NO_PROGRESS_CYCLES}, "
        f"got cycles {cycles_seen!r}"
    )


def test_task_branch_evidence_threaded_on_failing_gate(monkeypatch):
    """Part D (design §D): when the verified test-gate FAILED this cycle, the
    task-branch's ``_fb is None`` call site must thread that failing
    VERIFY_TEST_CMD output as citable evidence (reusing the ``_gate_out``/
    ``_gate_passed_now`` already sampled by the no-progress detector) instead
    of an empty ``EvidenceBundle()`` — collapsing the wasted hallucination
    turns a proposer would otherwise burn with nothing to cite."""
    monkeypatch.setattr(gs_module, "next_decision", _obligation_decision)

    captured_bundles = []

    def _fake_repair(graph, failed_id, bundle, cycle, **kwargs):
        captured_bundles.append(bundle)
        return RepairOutcome(
            graph=graph, still_failing_id=None, manual_blocks=(),
            known_invalid=frozenset(), turns_spent=1, budget_exhausted=True,
        )

    monkeypatch.setattr(orch, "run_structured_repair", _fake_repair)

    _STABLE_FAIL = (
        "FAILED tests/t.py::test_x - RuntimeError\n=== 1 failed in 0.10s ==="
    )

    def sandbox_execute(cmd):
        if cmd == orchestrator.VERIFY_TEST_CMD:
            return (False, _STABLE_FAIL)
        return (True, "ok")

    inputs = _base_inputs(sandbox_execute, max_cycles=1)
    orchestrator.run_v3(**inputs)

    assert len(captured_bundles) == 1
    bundle = captured_bundles[0]
    assert bundle.items, (
        "expected a non-empty EvidenceBundle citing the failing test-gate "
        "output, got an empty bundle"
    )
    ev = bundle.items[0]
    assert ev.command == orchestrator.VERIFY_TEST_CMD
    assert _STABLE_FAIL in ev.output_excerpt, (
        "the threaded evidence must cite the ACTUAL failing gate output"
    )
    assert ev.rc == 1


def test_passing_gate_fast_terminates_before_task_branch(monkeypatch):
    """Fast-termination (design: fast-termination) supersedes the old
    'evidence stays empty on a passing gate' behavior.

    Previously a frontier obligation was handed out and repaired even while the
    verified gate would pass (next_decision returns the obligation before
    testing), and the task branch threaded an empty EvidenceBundle(). The
    fast-termination fix now short-circuits to DONE the moment the VERIFIED gate
    passes — a node still MISSING while tests pass without it is an
    over-prediction — so the task-branch repair is NEVER reached on a passing
    cycle. This test pins that: run_structured_repair is not called and the run
    reports planner_done. (The empty-EvidenceBundle path still exists, but is now
    reachable only behind the service anti-hollow guard, which blocks
    fast-termination — covered by the graph_scheduler service tests.)"""
    monkeypatch.setattr(gs_module, "next_decision", _obligation_decision)

    captured_bundles = []

    def _fake_repair(graph, failed_id, bundle, cycle, **kwargs):
        captured_bundles.append(bundle)
        return RepairOutcome(
            graph=graph, still_failing_id=None, manual_blocks=(),
            known_invalid=frozenset(), turns_spent=1, budget_exhausted=True,
        )

    monkeypatch.setattr(orch, "run_structured_repair", _fake_repair)

    def sandbox_execute(cmd):
        if cmd == orchestrator.VERIFY_TEST_CMD:
            return (True, "1 passed in 0.01s")
        return (True, "ok")

    inputs = _base_inputs(sandbox_execute, max_cycles=1)
    _final_map, stop = orchestrator.run_v3(**inputs)

    assert stop == "planner_done", (
        f"a verified test pass must fast-terminate to DONE, got {stop!r}"
    )
    assert captured_bundles == [], (
        "fast-termination must short-circuit BEFORE the task-branch repair — "
        "run_structured_repair should never run when the gate already passes"
    )


def test_hollow_pass_churn_is_caught(monkeypatch):
    """A raw rc=0 'pass' that the anti-hollow gate rejects (no execution
    evidence) must sign as a STABLE FAILURE, not a real pass — exercising the
    VERIFIED-``passed`` keying (not the raw rc) from outcome_signature."""
    monkeypatch.setattr(gs_module, "next_decision", _obligation_decision)

    def _fake_repair(graph, failed_id, bundle, cycle, **kwargs):
        return RepairOutcome(
            graph=graph, still_failing_id=None, manual_blocks=(),
            known_invalid=frozenset(), turns_spent=1, budget_exhausted=False,
        )

    monkeypatch.setattr(orch, "run_structured_repair", _fake_repair)

    def sandbox_execute(cmd):
        if cmd == orchestrator.VERIFY_TEST_CMD:
            return (True, "no tests ran")   # raw pass, hollow -> verified False
        return (True, "ok")

    cycles_seen: list[int] = []
    inputs = _base_inputs(
        sandbox_execute, max_cycles=12,
        on_cycle=lambda cycle, *_a: cycles_seen.append(cycle),
    )

    final_map, stop = orchestrator.run_v3(**inputs)

    assert stop == "planner_giveup", (
        f"a stable hollow-pass gate must be caught as no-progress, got {stop!r}"
    )
    assert max(cycles_seen) == NO_PROGRESS_CYCLES == 3
