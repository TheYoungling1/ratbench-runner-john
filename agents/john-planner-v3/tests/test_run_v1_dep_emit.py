from __future__ import annotations

import sys
from pathlib import Path

# Put <repo>/src on the path so python_deps.depgraph.* resolves
# (mirrors the pattern in test_depgraph_live_certify.py and tests/depgraph/conftest.py).
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from src.envstate.orchestrator import run_v1  # noqa: E402
from src.envstate.ledger import ActionLedger  # noqa: E402
from src.envstate.world_model import (  # noqa: E402
    Fact,
    initial_map,
    PlannerDecision,
    TaskReport,
    CommandRecord,
)
from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph, Node, NodeType, Layer, State, DiscoveredBy,
)


class _GiveupPlanner:
    def decide(self, world_map):
        return PlannerDecision(action="giveup", reason="stop")


class _FakeBuildAgent:
    def __init__(self):
        self.recipes = []

    def run_recipe(self, recipe, sandbox_execute, ledger, step_offset=0):
        self.recipes.append(recipe)
        cmds = []
        for s in recipe.steps:
            sandbox_execute(s.command)          # propagate install side effects (R2/R3a)
            cmds.append(CommandRecord(s.command, 0, "ok"))
        return TaskReport("emit", "done", tuple(cmds), "ok", completed_steps=len(recipe.steps))


class _NoopMaintainer:
    def update(self, world_map, report):
        return world_map


def _pkg(name):
    return Node(id=f"pkg:{name}", type=NodeType.PACKAGE, name=name, layer=Layer.PIP,
                discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING, version="1.0",
                check_command=f'python -c "import {name}"')


def test_run_v1_emits_and_certifies_before_planner():
    g = DepGraph(nodes=(_pkg("flask"),))
    m = initial_map("img", "/app", "python 3.12", "pip", (), dep_graph=g)
    installed = set()

    def sandbox_execute(cmd):
        if "flask" in cmd:
            installed.add("flask")
        return True, "ok"

    def exec_readonly(cmd):
        return (0, "") if ("flask" in installed and "import flask" in cmd) else (1, "no")

    final, reason = run_v1(
        _GiveupPlanner(), _FakeBuildAgent(), _NoopMaintainer(), m, ActionLedger(),
        sandbox_execute, max_cycles=1, exec_readonly=exec_readonly, enable_dep_emit=True,
    )
    # emit ran before the planner gave up, so flask is certified in the carried graph
    assert final.dep_graph.get("pkg:flask").state is State.SATISFIED
    assert "CERTIFIED" in final.dep_advisory


def test_run_v1_off_state_does_not_touch_graph():
    g = DepGraph(nodes=(_pkg("flask"),))
    m = initial_map("img", "/app", "python 3.12", "pip", (), dep_graph=g)
    final, reason = run_v1(
        _GiveupPlanner(), _FakeBuildAgent(), _NoopMaintainer(), m, ActionLedger(),
        lambda c: (True, "ok"), max_cycles=1, enable_dep_emit=False,
    )
    assert final.dep_graph.get("pkg:flask").state is State.MISSING  # untouched


def test_emit_certified_packages_land_in_installed():
    """R3(d): after emit certifies flask, Fact('flask', ...) must be in final.installed."""
    g = DepGraph(nodes=(_pkg("flask"),))
    m = initial_map("img", "/app", "python 3.12", "pip", (), dep_graph=g)
    installed_set = set()

    def sandbox_execute(cmd):
        if "flask" in cmd:
            installed_set.add("flask")
        return True, "ok"

    def exec_readonly(cmd):
        return (0, "") if ("flask" in installed_set and "import flask" in cmd) else (1, "no")

    final, reason = run_v1(
        _GiveupPlanner(), _FakeBuildAgent(), _NoopMaintainer(), m, ActionLedger(),
        sandbox_execute, max_cycles=1, exec_readonly=exec_readonly, enable_dep_emit=True,
    )
    # Synthesis payoff: the Dockerfile synthesizer reads final.installed, so
    # emit-certified packages must land there even before the maintainer runs.
    assert any(f.name == "flask" for f in final.installed), (
        f"flask not found in final.installed: {final.installed}"
    )
