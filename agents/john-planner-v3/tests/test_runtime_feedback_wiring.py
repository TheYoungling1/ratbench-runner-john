"""Integration-style test: verify runtime_feedback wiring in orchestrator.run_v1.

Uses synthetic planner/build_agent/maintainer stubs (no Docker/LLM).
Confirms:
  - flag OFF  -> no runtime nodes appended (flag-off byte-identical)
  - flag ON   -> runtime PACKAGE node appears after a ledger event with ModuleNotFoundError
  - flag ON   -> exception in ingest does NOT crash the loop
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]          # repo root (this file is at tests/)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))                   # for `from src.envstate...`
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))                    # for `from python_deps...`

from src.envstate.ledger import ActionLedger, make_action_event
from src.envstate.orchestrator import run_v1
from src.envstate.world_model import (
    PlannerDecision,
    Task,
    TaskReport,
    WorldModelMap,
    initial_map,
    merge_map,
)
from python_deps.depgraph.ids import TEST_NODE_ID, package_id
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Layer, Node, NodeType, State,
)


# ── Minimal stubs ────────────────────────────────────────────────────────────

class _QueuePlanner:
    """Emits PlannerDecision objects from a pre-loaded queue (mirrors the
    FakePlanner convention in tests/test_orchestrator_v1.py)."""
    def __init__(self, decisions):
        self._queue = list(decisions)
    def decide(self, world_map):
        assert self._queue, "_QueuePlanner.decide called more times than expected"
        return self._queue.pop(0)


def _task() -> Task:
    return Task(goal="install deps", done_when="pip exits 0", layer="deps", facts=())


class _StubBuildAgent:
    def run(self, task, sandbox_execute, ledger, step_offset=0, check=None, budget=None):
        return TaskReport(task_goal="task", status="done", commands=(), learning="ok")

    def run_recipe(self, recipe, sandbox_execute, ledger, step_offset=0):
        return TaskReport(task_goal="recipe", status="done", commands=(), learning="ok")


class _NoopMaintainer:
    """Does not set done_flag, so the loop runs the queued 'task' cycle, then the
    planner's queued 'done' terminates it. (The pre-seeded ledger event is ingested
    at the top of cycle 1, before the planner is consulted.)"""
    def update(self, world_map, report):
        return world_map


def _sandbox_execute(cmd):
    return True, "ok"


def _make_initial_map_with_graph() -> WorldModelMap:
    """A valid WorldModelMap (via initial_map) carrying a minimal DepGraph.

    MUST use initial_map(...) — direct WorldModelMap(...) construction omits
    required frozen-dataclass fields and raises TypeError (C2 fix).
    """
    test_node = Node(
        id=TEST_NODE_ID,
        type=NodeType.TEST,
        name="repo_tests_pass",
        layer=Layer.TESTS,
        discovered_by=DiscoveredBy.GOAL,
    )
    graph = DepGraph().with_node(test_node)
    base = initial_map(
        base_image="python:3.11",
        workdir="/repo",
        language="python",
        build_system="pip",
        repo_layout=(),
    )
    return merge_map(base, dep_graph=graph)


def _ledger_with_module_error() -> ActionLedger:
    """Ledger pre-populated with one ModuleNotFoundError event."""
    ledger = ActionLedger()
    evt = make_action_event(
        step=1,
        cmd="python app.py",
        success=False,
        stdout="ModuleNotFoundError: No module named 'yaml'",
        env_revision_before=0,
        env_revision_after=0,
        mutation_class=None,
        container_id="test",
    )
    ledger.append(evt)
    return ledger


# ── flag OFF: byte-identical ──────────────────────────────────────────────────

def test_flag_off_graph_unchanged():
    ledger = _ledger_with_module_error()
    initial = _make_initial_map_with_graph()

    # Planner: one task cycle, then done.
    planner = _QueuePlanner([
        PlannerDecision(action="task", task=_task()),
        PlannerDecision(action="done"),
    ])
    final_map, _ = run_v1(
        planner=planner,
        build_agent=_StubBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=initial,
        ledger=ledger,
        sandbox_execute=_sandbox_execute,
        max_cycles=3,
        enable_runtime_feedback=False,
    )

    # No runtime nodes added — flag OFF is byte-identical to today.
    assert final_map.dep_graph is not None
    pkg_node = final_map.dep_graph.get(package_id("PyYAML", None))
    assert pkg_node is None, "flag OFF should not append runtime nodes"


# ── flag ON: runtime node appears ────────────────────────────────────────────

def test_flag_on_runtime_node_appended():
    ledger = _ledger_with_module_error()
    initial = _make_initial_map_with_graph()

    # Planner: one task cycle, then done. Ingest runs at the TOP of every cycle,
    # so the pre-seeded ModuleNotFoundError event is ingested on cycle 1 before
    # any branch returns.
    planner = _QueuePlanner([
        PlannerDecision(action="task", task=_task()),
        PlannerDecision(action="done"),
    ])
    final_map, _ = run_v1(
        planner=planner,
        build_agent=_StubBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=initial,
        ledger=ledger,
        sandbox_execute=_sandbox_execute,
        max_cycles=3,
        enable_runtime_feedback=True,
    )

    assert final_map.dep_graph is not None
    pkg_node = final_map.dep_graph.get(package_id("PyYAML", None))
    assert pkg_node is not None, "flag ON must append the runtime PACKAGE node"
    assert pkg_node.discovered_by is DiscoveredBy.RUNTIME


# ── flag ON: ingest exception does not crash the loop ────────────────────────

def test_flag_on_ingest_exception_does_not_crash():
    """If ingest raises internally, the loop must still complete normally."""
    # Monkey-patch the symbol the orchestrator imports (it imports the function
    # from python_deps.depgraph.runtime_ingest INSIDE _runtime_ingest_phase, so
    # patching the module attribute is the interception point).
    import python_deps.depgraph.runtime_ingest as _m
    original = _m.ingest_runtime_failures

    def _boom(graph, observations, classifiers=(_m.classify_observation,)):
        raise RuntimeError("simulated ingest crash")

    _m.ingest_runtime_failures = _boom
    try:
        ledger = _ledger_with_module_error()
        initial = _make_initial_map_with_graph()
        planner = _QueuePlanner([
            PlannerDecision(action="task", task=_task()),
            PlannerDecision(action="done"),
        ])
        final_map, stop_reason = run_v1(
            planner=planner,
            build_agent=_StubBuildAgent(),
            maintainer=_NoopMaintainer(),
            initial_world_map=initial,
            ledger=ledger,
            sandbox_execute=_sandbox_execute,
            max_cycles=3,
            enable_runtime_feedback=True,
        )
        # Loop must complete normally despite the exception.
        assert stop_reason in ("planner_done", "done_flag", "max_cycles")
    finally:
        _m.ingest_runtime_failures = original
