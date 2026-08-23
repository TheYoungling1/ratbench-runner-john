"""Tests: run_v3's task-dispatch branch is a single consolidated 3-way split
(Task 5a) — there is NO free-text mutation left in run_v3:

  - obligation tasks (target_node_ids set)   -> typed repair (run_structured_repair,
    emit=_binding_emit replay) when exec_readonly+client are both present, else an
    explicit GIVEUP_CONFIG give-up ("planner_giveup") — never build_agent.run.
  - discover tasks (target_node_ids empty)   -> the deterministic VERIFY_TEST_CMD
    gate (_run_discover_gate, Task 5b): one ledger event as evidence, no LLM call,
    bounded by the existing _sched_stuck counter (Task 5c) rather than the
    LLM-repair budget.

Phase 4 (fresh-replay-only run_v3): the old B3 ablation (obligation task
forced onto the free-text path) is not reachable through run_v3 at all —
there is exactly one executor (fresh full-script replay). Phase 9 removed
the vestigial ``enable_script_materialization``/``enable_binding_install``
deprecation-raise flags (and their dedicated raise-tests) entirely, since
the params no longer exist on run_v3's signature to reject.

Harness mirrors tests/envstate/test_v3_repair_wiring.py.

Patching strategy
-----------------
- ``src.envstate.graph_scheduler.next_decision`` is patched on the SOURCE module
  because run_v3 imports it with a local ``from ... import`` statement inside the
  function body; patching ``orch.next_decision`` would not intercept that lookup.
- ``src.envstate.orchestrator.run_structured_repair`` is patched on the orchestrator
  module because it is bound there at import time (top-level ``from ... import``).
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest

import src.envstate.graph_scheduler as gs_module
import src.envstate.orchestrator as orch
from src.envstate import orchestrator
from src.envstate.ledger import ActionLedger
from src.envstate.repair_loop import RepairOutcome
from src.envstate.world_model import (
    PlannerDecision,
    Task,
    TaskReport,
    initial_map,
)
from src.sandbox import InstallResult


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------

class _FakeClient:
    """Non-None sentinel for the ``getattr(build_agent, "client", None)`` guard."""


class _CountingBuildAgent:
    """Minimal build agent with a non-None .client and a counting .run."""

    def __init__(self):
        self.run_calls = 0
        self.client = _FakeClient()
        self.model = "fake-model"

    def run(self, task, sandbox_execute, ledger,
            step_offset=0, check=None, budget=None):
        self.run_calls += 1
        return TaskReport(task.goal, "done", (), "free-text run")

    def propose(self, scope, exec_readonly=None, **kwargs):
        return None


class _NoopMaintainer:
    def update(self, world_map, report):
        return world_map


def _base_map():
    """WorldModelMap with dep_graph=None — _dep_emit_phase is skipped."""
    return initial_map(
        base_image="python:3.11-slim",
        workdir="/repo",
        language="python",
        build_system="pip",
        repo_layout=(),
    )


def _obligation_task() -> Task:
    """Task with target_node_ids set — triggers the typed-repair path."""
    return Task(
        goal="install syslib:libpq",
        done_when="ldconfig -p | grep -q libpq",
        layer="system",
        facts=(),
        target_node_ids=("syslib:libpq",),
    )


def _discover_task() -> Task:
    """Task with no target_node_ids — routes through the deterministic discover gate."""
    return Task(
        goal="discover missing runtime deps",
        done_when="pytest --collect-only -q --disable-warnings",
        layer="tests",
        facts=(),
        target_node_ids=(),
    )


def _noop_reset_to_base() -> None:
    pass


def _noop_run_install_script(script: str) -> InstallResult:
    return InstallResult(rc=0, failing_command=None, lineno=None, stderr="")


def _make_run_v3_inputs(task: Task) -> dict:
    """Build a dict of kwargs for run_v3.

    dep_graph=None → _dep_emit_phase short-circuits immediately (before it
    would ever touch reset_to_base/run_install_script). enable_dep_emit=False
    → extra safeguard (belt-and-suspenders). max_cycles=1 → loop terminates
    after a single task cycle. reset_to_base/run_install_script are required
    unconditionally by run_v3 (Phase 4) even though this harness's dep-emit
    phase never calls them — no-op fakes just satisfy the guard.
    """
    led = ActionLedger()

    def sandbox(cmd: str):
        return (True, "ok")

    def ro(cmd: str):
        return (1, "")

    return dict(
        build_agent=_CountingBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_base_map(),
        ledger=led,
        sandbox_execute=sandbox,
        max_cycles=1,
        exec_readonly=ro,
        enable_dep_emit=False,
        reset_to_base=_noop_reset_to_base,
        run_install_script=_noop_run_install_script,
    )


# ---------------------------------------------------------------------------
# Fixture-bundle class
# ---------------------------------------------------------------------------

class _FixtureBundle:
    """Wraps a single run_v3 invocation; exposes run_calls + repair_calls after .run()."""

    def __init__(
        self,
        inputs: dict,
        task: Task,
        monkeypatch,
    ) -> None:
        self._inputs = inputs
        self._task = task
        self._mp = monkeypatch
        self.run_calls: int = 0
        self.repair_calls: int = 0
        self.final_map = None
        self.stop_reason: str | None = None

    def events(self):
        """The ActionLedger events recorded during .run() (identity, not a copy)."""
        return self._inputs["ledger"].events()

    def run(self) -> None:
        _repair_ref: dict[str, int] = {"n": 0}
        task = self._task

        # ── patch next_decision on the SOURCE module ──────────────────────────
        def _fake_next_decision(
            graph, run_tests, handed=None, attempt_cap=3, **kwargs
        ):
            return (PlannerDecision(action="task", task=task), None)

        self._mp.setattr(gs_module, "next_decision", _fake_next_decision)

        # ── patch run_structured_repair on the orchestrator module ────────────
        def _fake_repair(graph, failed_id, bundle, cycle, **kwargs):
            _repair_ref["n"] += 1
            return RepairOutcome(
                graph=graph,
                still_failing_id=None,
                manual_blocks=(),
                known_invalid=frozenset(),
                turns_spent=1,
                budget_exhausted=True,   # forces termination after one cycle
            )

        self._mp.setattr(orch, "run_structured_repair", _fake_repair)

        # ── run ───────────────────────────────────────────────────────────────
        self.final_map, self.stop_reason = orchestrator.run_v3(**self._inputs)

        self.run_calls = self._inputs["build_agent"].run_calls
        self.repair_calls = _repair_ref["n"]


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def _v3_task_fixture(monkeypatch):
    """Obligation task (target_node_ids set)."""
    inputs = _make_run_v3_inputs(task=_obligation_task())
    return _FixtureBundle(inputs, _obligation_task(), monkeypatch)


@pytest.fixture
def _v3_discover_fixture(monkeypatch):
    """Discover task (target_node_ids empty)."""
    inputs = _make_run_v3_inputs(task=_discover_task())
    return _FixtureBundle(inputs, _discover_task(), monkeypatch)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_obligation_task_uses_propose_not_run(_v3_task_fixture):
    """Obligation task must call run_structured_repair (typed path) and must
    NOT call build_agent.run.
    """
    _v3_task_fixture.run()
    assert _v3_task_fixture.run_calls == 0, (
        f"build_agent.run was called {_v3_task_fixture.run_calls} time(s); "
        "obligation task should route through typed repair path"
    )
    assert _v3_task_fixture.repair_calls >= 1, (
        "run_structured_repair was never called for an obligation task"
    )


def test_discover_task_runs_gate_not_agent(_v3_discover_fixture):
    """Discover task (no target_node_ids) must run the deterministic VERIFY_TEST_CMD
    gate (Task 5b) — NOT build_agent.run (free text) and NOT run_structured_repair
    (typed repair). This is the Task 5a/5b replacement for the old free-text
    ``test_discover_task_uses_run``.
    """
    _v3_discover_fixture.run()
    assert _v3_discover_fixture.run_calls == 0, (
        "build_agent.run was called for a discover task; run_v3 has no free-text "
        "mutation path left (Task 5a)"
    )
    assert _v3_discover_fixture.repair_calls == 0, (
        "run_structured_repair was called for a discover task (no target_node_ids)"
    )
    events = _v3_discover_fixture.events()
    assert any(e.cmd == orchestrator.VERIFY_TEST_CMD for e in events), (
        "the discover gate must append a VERIFY_TEST_CMD ledger event as evidence "
        "for the next cycle's _runtime_ingest_phase"
    )


def test_discover_gate_records_ledger_evidence(monkeypatch):
    """Task 5b: the discover gate appends exactly ONE ActionEvent per cycle,
    carrying the VERIFY_TEST_CMD command + raw output as evidence, and mutates
    nothing (env_revision does not advance).
    """
    inputs = _make_run_v3_inputs(task=_discover_task())
    fail_out = "E   ModuleNotFoundError: No module named 'requests'"

    def _failing_sandbox(cmd: str):
        return (False, fail_out)

    inputs["sandbox_execute"] = _failing_sandbox

    bundle = _FixtureBundle(inputs, _discover_task(), monkeypatch)
    bundle.run()

    gate_events = [e for e in bundle.events() if e.cmd == orchestrator.VERIFY_TEST_CMD]
    assert len(gate_events) == 1, (
        f"expected exactly one VERIFY_TEST_CMD ledger event, got {len(gate_events)}"
    )
    evt = gate_events[0]
    assert evt.rc == 1
    assert fail_out in evt.stdout
    assert evt.mutation_class is None, "discover gate mutates nothing"
    assert evt.env_revision_before == evt.env_revision_after, (
        "discover gate mutates nothing; env revision must not advance"
    )


def test_repeated_unclassified_discover_gives_up(monkeypatch):
    """Task 5c: repeated discover-gate failures that never grow the graph must
    terminate via the existing bounded ``_sched_stuck`` counter
    (GIVEUP_STUCK -> 'planner_giveup') rather than run to max_cycles or loop
    forever waiting for a classification that will never arrive. The 2-round
    bound is intentional (see the comment at orchestrator.py's `_sched_stuck
    >= 2` check) — this test is the regression pin for it.
    """
    inputs = _make_run_v3_inputs(task=_discover_task())
    inputs["max_cycles"] = 5
    calls = {"n": 0}

    def _failing_sandbox(cmd: str):
        calls["n"] += 1
        return (False, "E   ModuleNotFoundError: No module named 'totally_unclassifiable_xyz'")

    inputs["sandbox_execute"] = _failing_sandbox

    bundle = _FixtureBundle(inputs, _discover_task(), monkeypatch)
    bundle.run()

    assert bundle.stop_reason == "planner_giveup"
    assert calls["n"] < inputs["max_cycles"], (
        "discover gate ran for the full max_cycles budget instead of giving up "
        "via the bounded _sched_stuck counter"
    )


def test_obligation_task_without_exec_readonly_gives_up(monkeypatch):
    """Obligation task with exec_readonly=None must NOT enter the typed-repair
    path (which would crash at certify_refresh) and must NOT silently downgrade
    to free-text mutation (build_agent.run — removed from run_v3 by Task 5a).
    It gives up honestly via GIVEUP_CONFIG -> 'planner_giveup'.

    Regression guard for I-1 (missing ``exec_readonly is not None`` guard),
    rewritten for Task 5a's explicit 3-way dispatch: there is no free-text
    fallback left to fall through to.
    """
    inputs = _make_run_v3_inputs(task=_obligation_task())
    # Override exec_readonly to None to reproduce the crash scenario
    inputs["exec_readonly"] = None

    bundle = _FixtureBundle(inputs, _obligation_task(), monkeypatch)
    bundle.run()

    assert bundle.stop_reason == "planner_giveup"
    assert bundle.repair_calls == 0, (
        "run_structured_repair was called with exec_readonly=None; "
        "this would crash at certify_refresh(graph, None, cycle)"
    )
    assert bundle.run_calls == 0, (
        "build_agent.run was called; the give-up path must not fall back to "
        "free-text mutation (run_v3 has no free-text path left)"
    )


def test_no_free_text_build_agent_run_in_run_v3_source():
    """Source-level pin (belt-and-suspenders on top of the behavioral tests
    above): run_v3's body must not contain a ``build_agent.run(`` call site.
    """
    import inspect
    src = inspect.getsource(orchestrator.run_v3)
    assert "build_agent.run(" not in src


def test_block_emit_absent_from_run_v3_source():
    """Task 5a/5b: block_emit is fully gone as an EXECUTABLE code path in
    run_v3 — the task branch's typed-repair emit is now the SAME hoisted
    replay closure (_binding_emit) that _dep_emit_phase uses. block_emit
    remains a standalone, directly-unit-tested module for run_v1 / a future
    ablation entry point (Phase 9); run_v3's docstring still names it
    (pointing callers at that ablation), so this checks for the actual
    import/call sites rather than any mention of the string.
    """
    import inspect
    src = inspect.getsource(orchestrator.run_v3)
    assert "import block_emit" not in src
    assert "block_emit(" not in src
