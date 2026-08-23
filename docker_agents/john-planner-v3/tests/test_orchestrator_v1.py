# tests/test_orchestrator_v1.py
"""Unit tests for run_v1 orchestrator loop (src/envstate/orchestrator.py).

All collaborators are faked — no LLM calls, no Docker containers.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
import pytest
from dataclasses import dataclass
from typing import Callable

# Put <repo>/src on sys.path so python_deps.* resolves when enable_graph_scheduler=True
# triggers the lazy import in graph_scheduler.py (mirrors test_graph_scheduler_wiring.py).
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from src.envstate.ledger import ActionLedger
from src.envstate.world_model import (
    CommandRecord,
    Fact,
    OpenProblem,
    PlannerDecision,
    Task,
    TaskReport,
    WorldModelMap,
    initial_map,
    merge_map,
)
from src.sandbox import InstallResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# run_v3 is fresh-replay-only (Phase 4): reset_to_base/run_install_script are
# mandatory. These v3 smoke tests use dep_graph=None (_dep_emit_phase
# short-circuits before ever touching them), so no-op fakes just satisfy the
# guard.
def _noop_reset_to_base() -> None:
    pass


def _noop_run_install_script(script: str) -> InstallResult:
    return InstallResult(rc=0, failing_command=None, lineno=None, stderr="")

def _base_map() -> WorldModelMap:
    return initial_map(
        base_image="python:3.11-slim",
        workdir="/app",
        language="python 3.11",
        build_system="pip",
        repo_layout=("src/", "tests/", "pyproject.toml"),
    )


def _task() -> Task:
    return Task(
        goal="install deps",
        done_when="pip install exits 0",
        layer="deps",
        facts=(),
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakePlanner:
    """Emits PlannerDecision objects from a pre-loaded queue."""

    def __init__(self, decisions: list[PlannerDecision]) -> None:
        self._queue = list(decisions)

    def decide(self, current_map: WorldModelMap) -> PlannerDecision:
        assert self._queue, "FakePlanner.decide called more times than expected"
        return self._queue.pop(0)


class FakeBuildAgent:
    """Returns TaskReport objects from a pre-loaded queue."""

    def __init__(self, reports: list[TaskReport]) -> None:
        self._queue = list(reports)

    def run(
        self,
        task: Task,
        sandbox_execute: Callable[[str], tuple[bool, str]],
        ledger: ActionLedger,
        step_offset: int = 0,
        check=None,
        budget: int | None = None,
    ) -> TaskReport:
        assert self._queue, "FakeBuildAgent.run called more times than expected"
        return self._queue.pop(0)


class FakeMaintainer:
    """Applies a series of map transformations from a pre-loaded queue."""

    def __init__(self, maps: list[WorldModelMap]) -> None:
        self._queue = list(maps)

    def update(self, current_map: WorldModelMap, report: TaskReport) -> WorldModelMap:
        assert self._queue, "FakeMaintainer.update called more times than expected"
        return self._queue.pop(0)


def _noop_sandbox(cmd: str) -> tuple[bool, str]:
    return True, "ok"


# ---------------------------------------------------------------------------
# Import target (will fail until orchestrator.py is rewritten)
# ---------------------------------------------------------------------------

from src.envstate.orchestrator import run_v1, run_v3, VERIFY_TEST_CMD  # noqa: E402
from src.envstate.deterministic_maintainer import DeterministicMaintainer  # noqa: E402


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRunV1DoneFlagTerminates:
    """done_flag=True in the map breaks the loop immediately."""

    def test_done_flag_after_one_cycle_returns_done_flag_reason(self):
        world_map = _base_map()
        done_map = merge_map(world_map, done_flag=True)

        planner = FakePlanner([
            PlannerDecision(action="task", task=_task()),
        ])
        build_agent = FakeBuildAgent([
            TaskReport(
                task_goal="install deps",
                status="done",
                commands=(CommandRecord(cmd="pip install .", rc=0, output="ok"),),
                learning="deps installed",
            ),
        ])
        maintainer = FakeMaintainer([done_map])

        final_map, stop_reason = run_v1(
            planner=planner,
            build_agent=build_agent,
            maintainer=maintainer,
            initial_world_map=world_map,
            ledger=ActionLedger(),
            sandbox_execute=_noop_sandbox,
            max_cycles=12,
        )

        assert stop_reason == "done_flag"
        assert final_map.done_flag is True

    def test_done_flag_does_not_call_planner_again(self):
        """After done_flag is set the loop must break before the next planner.decide."""
        world_map = _base_map()
        done_map = merge_map(world_map, done_flag=True)

        # Planner only has ONE decision; a second call would raise AssertionError.
        planner = FakePlanner([
            PlannerDecision(action="task", task=_task()),
        ])
        build_agent = FakeBuildAgent([
            TaskReport(task_goal="g", status="done", commands=(), learning=""),
        ])
        maintainer = FakeMaintainer([done_map])

        final_map, stop_reason = run_v1(
            planner=planner,
            build_agent=build_agent,
            maintainer=maintainer,
            initial_world_map=world_map,
            ledger=ActionLedger(),
            sandbox_execute=_noop_sandbox,
            max_cycles=12,
        )

        assert stop_reason == "done_flag"


class TestRunV1PlannerDone:
    """planner.decide returning action='done' should terminate cleanly."""

    def test_planner_done_returns_planner_done_reason(self):
        world_map = _base_map()

        planner = FakePlanner([
            PlannerDecision(action="done", reason="all layers verified"),
        ])
        # build_agent and maintainer should NOT be called
        build_agent = FakeBuildAgent([])
        maintainer = FakeMaintainer([])

        final_map, stop_reason = run_v1(
            planner=planner,
            build_agent=build_agent,
            maintainer=maintainer,
            initial_world_map=world_map,
            ledger=ActionLedger(),
            sandbox_execute=_noop_sandbox,
            max_cycles=12,
        )

        assert stop_reason == "planner_done"
        assert final_map is world_map  # map unchanged when planner says done

    def test_planner_giveup_returns_planner_giveup_reason(self):
        world_map = _base_map()

        planner = FakePlanner([
            PlannerDecision(action="giveup", reason="irrecoverable conflict"),
        ])
        build_agent = FakeBuildAgent([])
        maintainer = FakeMaintainer([])

        final_map, stop_reason = run_v1(
            planner=planner,
            build_agent=build_agent,
            maintainer=maintainer,
            initial_world_map=world_map,
            ledger=ActionLedger(),
            sandbox_execute=_noop_sandbox,
            max_cycles=12,
        )

        assert stop_reason == "planner_giveup"


class TestRunV1MaxCycles:
    """Exhausting max_cycles without done_flag returns 'max_cycles'."""

    def test_max_cycles_exhaustion_stop_reason(self):
        world_map = _base_map()
        # Each cycle: planner says "task", agent returns blocked, maintainer returns same map.
        n = 3
        planner = FakePlanner([
            PlannerDecision(action="task", task=_task()) for _ in range(n)
        ])
        build_agent = FakeBuildAgent([
            TaskReport(task_goal="install deps", status="blocked", commands=(), learning="still blocked")
            for _ in range(n)
        ])
        maintainer = FakeMaintainer([world_map for _ in range(n)])

        final_map, stop_reason = run_v1(
            planner=planner,
            build_agent=build_agent,
            maintainer=maintainer,
            initial_world_map=world_map,
            ledger=ActionLedger(),
            sandbox_execute=_noop_sandbox,
            max_cycles=n,
        )

        assert stop_reason == "max_cycles"
        assert final_map.done_flag is False

    def test_default_max_cycles_constant_is_12(self):
        """MAX_CYCLES module constant must equal 12 per spec §8."""
        from src.envstate import orchestrator
        assert orchestrator.MAX_CYCLES == 12

    def test_collect_only_cmd_constant_present(self):
        """COLLECT_ONLY_CMD module constant must be defined in orchestrator."""
        from src.envstate import orchestrator
        assert hasattr(orchestrator, "COLLECT_ONLY_CMD")
        assert "--collect-only" in orchestrator.COLLECT_ONLY_CMD


class TestRunV1TwoCycleRun:
    """Full 2-cycle run: cycle 1 task+blocked, cycle 2 task+done_flag."""

    def test_two_cycle_run_succeeds_on_second_cycle(self):
        world_map = _base_map()
        map_after_cycle1 = merge_map(world_map, notes=("deps partially installed",))
        map_after_cycle2 = merge_map(map_after_cycle1, done_flag=True)

        planner = FakePlanner([
            PlannerDecision(action="task", task=_task()),  # cycle 1
            PlannerDecision(action="task", task=_task()),  # cycle 2
        ])
        build_agent = FakeBuildAgent([
            TaskReport(task_goal="install deps", status="blocked", commands=(), learning="network error"),
            TaskReport(
                task_goal="install deps",
                status="done",
                commands=(CommandRecord(cmd="pip install .", rc=0, output="Successfully installed"),),
                learning="deps installed",
            ),
        ])
        maintainer = FakeMaintainer([map_after_cycle1, map_after_cycle2])

        on_cycle_calls: list[tuple[int, str]] = []

        def on_cycle(cycle_num, current_map, decision, report):
            on_cycle_calls.append((cycle_num, decision.action))

        final_map, stop_reason = run_v1(
            planner=planner,
            build_agent=build_agent,
            maintainer=maintainer,
            initial_world_map=world_map,
            ledger=ActionLedger(),
            sandbox_execute=_noop_sandbox,
            max_cycles=12,
            on_cycle=on_cycle,
        )

        assert stop_reason == "done_flag"
        assert final_map.done_flag is True
        assert len(on_cycle_calls) == 2
        assert on_cycle_calls[0] == (1, "task")
        assert on_cycle_calls[1] == (2, "task")


def test_done_branch_returns_planner_done_immediately():
    """action='done' returns ('…','planner_done') without running VERIFY_TEST_CMD.

    Regression pin: after removing the advisory-done path the done branch must
    return immediately without executing any sandbox commands.
    """
    calls: list[str] = []

    def tracking_exec(cmd: str) -> tuple[bool, str]:
        calls.append(cmd)
        return (True, "")

    planner = FakePlanner([PlannerDecision(action="done", reason="x")])
    final_map, stop = run_v1(
        planner, FakeBuildAgent([]), FakeMaintainer([]),
        _base_map(), ActionLedger(), tracking_exec, max_cycles=2,
    )
    assert stop == "planner_done"
    assert VERIFY_TEST_CMD not in calls   # advisory path is gone


# ---------------------------------------------------------------------------
# TerminationReason enum maps to the correct legacy stop-reason strings
# ---------------------------------------------------------------------------

def test_termination_reason_maps_to_legacy_strings():
    from src.envstate.orchestrator import TerminationReason, _to_stop_reason
    assert _to_stop_reason(TerminationReason.DONE) == "planner_done"
    assert _to_stop_reason(TerminationReason.DONE_FLAG) == "done_flag"
    assert _to_stop_reason(TerminationReason.GIVEUP_RESIDUAL) == "planner_giveup"
    assert _to_stop_reason(TerminationReason.GIVEUP_BUDGET) == "planner_giveup"
    assert _to_stop_reason(TerminationReason.GIVEUP_STUCK) == "planner_giveup"
    assert _to_stop_reason(TerminationReason.MAX_CYCLES) == "max_cycles"


class TestRunV1ReturnType:
    """run_v1 always returns a (WorldModelMap, str) tuple."""

    def test_return_is_tuple_of_map_and_str(self):
        world_map = _base_map()
        planner = FakePlanner([PlannerDecision(action="done", reason="ok")])
        result = run_v1(
            planner=planner,
            build_agent=FakeBuildAgent([]),
            maintainer=FakeMaintainer([]),
            initial_world_map=world_map,
            ledger=ActionLedger(),
            sandbox_execute=_noop_sandbox,
        )
        assert isinstance(result, tuple)
        assert len(result) == 2
        final_map, stop_reason = result
        assert isinstance(final_map, WorldModelMap)
        assert isinstance(stop_reason, str)


# ---------------------------------------------------------------------------
# Signature-guard: Task 3 — enable_contract_graph must be gone
# ---------------------------------------------------------------------------

def test_run_v1_has_no_contract_graph_param():
    params = inspect.signature(run_v1).parameters
    assert "enable_contract_graph" not in params


# ---------------------------------------------------------------------------
# v3 smoke: graph-scheduler reaches planner_done on a clean graph
# ---------------------------------------------------------------------------

def _world_map_with_clean_dep_graph() -> WorldModelMap:
    """WorldModelMap with dep_graph=None: scheduler frontier is empty.

    next_decision(None, run_tests=passing) → 'done' immediately.
    Mirrors the fixture pattern from test_graph_scheduler_wiring._missing_node_map()
    but uses the off-None path (no nodes at all = clean frontier).
    """
    return _base_map()


class _UnusedPlanner:
    """Explodes if planner.decide is ever called (graph-scheduler bypasses it)."""
    def decide(self, world_map):
        raise AssertionError("planner.decide must not be called in graph-scheduler mode")


class _NoopBuildAgent:
    """build_agent stub — never called when next_decision returns 'done' immediately."""
    def run(self, task, sandbox_execute, ledger, step_offset=0, check=None, budget=None):
        raise AssertionError("build_agent.run must not be called when scheduler yields done")

    def run_recipe(self, recipe, sandbox_execute, ledger, step_offset=0):
        raise AssertionError("build_agent.run_recipe must not be called here")


class _NoopMaintainer:
    """Passes world_map through unchanged — never called on the done path."""
    def update(self, world_map, report):
        return world_map


class _PassiveBuildAgent:
    """build_agent stub that can be called but returns a blocked report with no commands.

    Used in anti-hollow tests where next_decision dispatches a discover task
    (because _run_tests_verified rejects hollow output) but the agent produces
    no evidence of a real test pass, so the maintainer done-gate stays False.
    """
    def run(self, task, sandbox_execute, ledger, step_offset=0, check=None, budget=None):
        return TaskReport("discover", "blocked", (), "no commands ran")

    def run_recipe(self, recipe, sandbox_execute, ledger, step_offset=0):
        return TaskReport("recipe", "blocked", (), "no commands ran")


def test_v3_graph_scheduler_giveup_replay_when_dep_graph_none_never_replayed(monkeypatch):
    """Phase 7 (installability gate binding): with dep_graph=None, `_dep_emit_phase`
    short-circuits before ever calling `_binding_emit` (`current_map.dep_graph is
    None` guard, R3(c)) — so no fresh-from-base replay ever runs and
    `_last_replay_result` stays None. Even though the scheduler's frontier is
    trivially empty and tests pass (`next_decision` would say 'done'), run_v3 must
    NOT report 'planner_done' on an environment that was never actually replayed
    from base — there is no proof it builds. This closes the same hollow-success
    hole as `test_v3_collect_only_does_not_finalize_as_done` below: a passing
    scheduler decision alone is not sufficient, the binding replay proof is
    required too.

    Renamed from `test_v3_graph_scheduler_reaches_planner_done_on_clean_graph`
    (pre-Phase-7 characterization, which asserted the opposite — 'planner_done' —
    precisely because no replay was ever required for "done"; see
    `test_v3_graph_scheduler_reaches_planner_done_with_real_replay` below for the
    equivalent scenario WITH a real replay, which still reaches 'planner_done').
    """
    monkeypatch.setenv("DOCKERAGENT_ENABLE_SERVICE_PROVISION", "0")
    # clean dep-graph (dep_graph=None → empty frontier) + tests pass, but
    # _dep_emit_phase never runs (no dep_graph) → no replay ever happens.
    world = _world_map_with_clean_dep_graph()

    def passing_exec(cmd):
        return (True, "1 passed")

    final_map, stop = run_v3(
        _NoopBuildAgent(), _NoopMaintainer(),
        world, ActionLedger(), passing_exec, max_cycles=3,
        enable_dep_emit=True, enable_runtime_feedback=True,
        reset_to_base=_noop_reset_to_base, run_install_script=_noop_run_install_script,
    )
    assert stop == "planner_giveup"


def test_v3_graph_scheduler_reaches_planner_done_with_real_replay(monkeypatch):
    """Companion to the giveup case above: an EMPTY (but not None) dep_graph +
    a real exec_readonly DOES drive `_dep_emit_phase` through `_binding_emit`
    (reset_to_base + run_install_script), so `_last_replay_result` is a real
    rc=0 InstallResult by the time the scheduler decides 'done' — the binding
    replay guard (Phase 7) is satisfied and the run terminates 'planner_done'.
    """
    monkeypatch.setenv("DOCKERAGENT_ENABLE_SERVICE_PROVISION", "0")
    from python_deps.depgraph.schema import DepGraph
    world = merge_map(_base_map(), dep_graph=DepGraph())

    def passing_exec(cmd):
        return (True, "1 passed")

    def noop_exec_readonly(cmd):
        return (0, "")

    final_map, stop = run_v3(
        _NoopBuildAgent(), _NoopMaintainer(),
        world, ActionLedger(), passing_exec, max_cycles=3,
        exec_readonly=noop_exec_readonly,
        enable_dep_emit=True, enable_runtime_feedback=True,
        reset_to_base=_noop_reset_to_base, run_install_script=_noop_run_install_script,
    )
    assert stop == "planner_done"


# ---------------------------------------------------------------------------
# v3 anti-hollow: collect-only must not set done_flag
# ---------------------------------------------------------------------------

def test_v3_collect_only_does_not_finalize_as_done():
    """A pytest --collect-only rc=0 must NOT set done_flag / terminate run_v3 as done.

    The v3 done-gate requires a REAL verified test pass (_verified_test_run_passed).
    Characterization of the anti-hollow guarantee:

    Flow:
      collect_only_exec always returns (True, "collected 5 items") — rc=0, but the
      output contains no execution evidence ("N passed" or "[100%]"). Two guards
      block the hollow path:

      1. SCHEDULER gate (_run_tests_verified): runs sandbox_execute(VERIFY_TEST_CMD)
         → (True, "collected 5 items").  _gate_passed rejects this because
         _shows_execution("collected 5 items") is False (no "N passed" / "Ran N tests")
         and _shows_pytest_completion is False (no "[100%]").  run_tests() returns
         False, so next_decision returns a discover task instead of 'done'.

      2. MAINTAINER gate (DeterministicMaintainer v3_only=True): the discover-task
         build_agent returns a TaskReport with no commands.  _verified_test_run_passed
         finds no rc=0 test-command record → done_flag stays False.

    Proof it can fail: replace collect_only_exec with one that returns
    (True, "3 passed") for VERIFY_TEST_CMD — _run_tests_verified returns True,
    next_decision returns 'done', and the loop exits immediately as 'planner_done'.
    (The _PassiveBuildAgent never gets called on the done path, so no done_flag is
    set via the maintainer either, but the scheduler gate IS breached — that is the
    hollow-success anti-pattern this test closes off.)
    """
    world = _world_map_with_clean_dep_graph()

    def collect_only_exec(cmd):
        # Any command (including VERIFY_TEST_CMD) returns rc=0 but only hollow
        # collect-only output — no actual test execution evidence.
        return (True, "collected 5 items")

    final_map, stop = run_v3(
        _PassiveBuildAgent(), DeterministicMaintainer(v3_only=True),
        world, ActionLedger(), collect_only_exec, max_cycles=2,
        reset_to_base=_noop_reset_to_base, run_install_script=_noop_run_install_script,
    )
    assert final_map.done_flag is not True  # collect-only must not finalize
    assert stop not in ("planner_done", "done_flag")  # hollow scheduler gate is also caught
