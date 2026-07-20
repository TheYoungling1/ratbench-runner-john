"""Test that a fresh-replay install failure triggers run_structured_repair in the v3 path.

Phase 4 (fresh-replay-only run_v3): the emit under test is the ONLY executor
(orchestrator._binding_emit — render whole graph -> reset_to_base ->
run_install_script -> certify_reciped_only), not block_emit (removed from
run_v3's _dep_emit_phase). Harness fake style originally mirrored
tests/test_v3_block_emit_wiring.py (removed in Phase 9 — it existed solely
to pin the now-deleted enable_script_materialization/enable_binding_install
deprecation-raise), but exercises reset_to_base/run_install_script instead of
monkeypatching block_emit.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

import src.envstate.orchestrator as orch
from src.envstate import orchestrator
from src.envstate.ledger import ActionLedger
from src.envstate.repair_loop import RepairOutcome
from src.envstate.world_model import TaskReport, initial_map, merge_map
from src.sandbox import InstallResult
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Layer, Node, NodeType, State,
)


# ---------------------------------------------------------------------------
# Fakes (originally mirrored from the now-removed test_v3_block_emit_wiring.py,
# with .client added)
# ---------------------------------------------------------------------------

class _FakeClient:
    """Non-None sentinel to pass the `getattr(build_agent, "client", None)` guard."""


class _RecordingBuildAgent:
    """Minimal build agent with a non-None .client so the repair guard fires."""

    def __init__(self):
        self.tasks = []
        self.client = _FakeClient()   # repair guard: getattr(build_agent, "client", None)
        self.model = "fake-model"

    def run(self, task, sandbox_execute, ledger, step_offset=0, check=None, budget=None):
        self.tasks.append(task)
        return TaskReport(task_goal="t", status="blocked", commands=(), learning="b")

    def run_recipe(self, recipe, sandbox_execute, ledger, step_offset=0):
        return TaskReport(task_goal="r", status="done", commands=(), learning="ok")

    def propose(self, scope, **kwargs):
        return None


class _NoopMaintainer:
    def update(self, world_map, report):
        return world_map


def _syslib_map():
    """A WorldModelMap with one MISSING SystemLib (same fixture shape as the
    now-removed test_v3_block_emit_wiring.py used)."""
    node = Node(
        id="syslib:libpq.so",
        type=NodeType.SYSTEM_LIB,
        name="libpq.so",
        layer=Layer.SYSTEM,
        discovered_by=DiscoveredBy.RESOLVER,
        state=State.MISSING,
        check_command="ldconfig -p | grep -q libpq",
        chosen_fix="apt:libpq-dev",
    )
    base = initial_map(
        base_image="python:3.11-slim",
        workdir="/repo",
        language="python",
        build_system="pip",
        repo_layout=(),
    )
    return merge_map(base, dep_graph=DepGraph().with_node(node))


def _build_inputs():
    """Construct all-callable fakes for run_v3.

    exec_readonly always reports the check_command as failing (rc=1), so the
    node stays MISSING through both certify_refresh AND certify_reciped_only
    (inside _binding_emit) regardless of the install outcome — this is what
    makes _binding_emit return a non-None failed_node and trigger the repair
    guard. run_install_script reports a clean (rc=0) install: the node is
    "unsatisfied after an ostensibly successful install", the scenario
    run_structured_repair exists to resolve.
    """
    led = ActionLedger()

    def sandbox(cmd):
        return (True, "ok")

    def ro(cmd):
        # Node stays MISSING through certify_refresh AND certify_reciped_only.
        return (1, "")

    def reset_to_base():
        pass

    def run_install_script(script):
        return InstallResult(rc=0, failing_command=None, lineno=None, stderr="")

    return dict(
        build_agent=_RecordingBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_syslib_map(),
        ledger=led,
        sandbox_execute=sandbox,
        max_cycles=1,
        exec_readonly=ro,
        enable_dep_emit=True,
        reset_to_base=reset_to_base,
        run_install_script=run_install_script,
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_failed_install_invokes_structured_repair(monkeypatch):
    """When the fresh-replay emit certifies a node as still unsatisfied after
    install and build_agent.client is not None, run_structured_repair must be
    called at least once.
    """
    calls = {"repair": 0}

    # --- mock run_structured_repair: record the call and return success -----
    def _fake_repair(graph, failed_id, bundle, cycle, **kwargs):
        calls["repair"] += 1
        return RepairOutcome(
            graph=graph,
            still_failing_id=None,   # resolved — no budget exhaustion
            manual_blocks=(),
            known_invalid=frozenset(),
            turns_spent=1,
            budget_exhausted=False,
        )

    monkeypatch.setattr(orch, "run_structured_repair", _fake_repair)

    # --- run -------------------------------------------------------------------
    inputs = _build_inputs()
    orchestrator.run_v3(**inputs)

    # --- assert ----------------------------------------------------------------
    assert calls["repair"] >= 1, (
        "run_structured_repair was never called; a node still unsatisfied "
        "after the fresh-replay install should trigger typed repair"
    )
