from __future__ import annotations

import sys
from pathlib import Path

# Put <repo>/src on the path so python_deps.depgraph.* resolves
# (mirrors the pattern in tests/test_depgraph_live_certify.py and tests/depgraph/conftest.py).
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from src.envstate.depgraph_live import emit_drain  # noqa: E402
from src.envstate.ledger import ActionLedger  # noqa: E402
from src.envstate.world_model import TaskReport, CommandRecord  # noqa: E402
from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph, Edge, EdgeType, Layer, Node, NodeType, State, DiscoveredBy,
)


class _FakeBuildAgent:
    def __init__(self):
        self.recipes = []

    def run_recipe(self, recipe, sandbox_execute, ledger, step_offset=0):
        self.recipes.append(recipe)
        cmds = []
        for s in recipe.steps:
            sandbox_execute(s.command)          # propagate install side effects (R2)
            cmds.append(CommandRecord(s.command, 0, "ok"))
        return TaskReport("emit", "done", tuple(cmds), "ok", completed_steps=len(recipe.steps))


def _pkg(name, *, state=State.MISSING):
    return Node(id=f"pkg:{name}", type=NodeType.PACKAGE, name=name, layer=Layer.PIP,
                discovered_by=DiscoveredBy.RESOLVER, state=state, version="1.0",
                check_command=f'python -c "import {name}"')


def test_emit_drain_installs_then_certifies():
    g = DepGraph(nodes=(_pkg("flask"), _pkg("click")))
    ba = _FakeBuildAgent()
    installed = set()

    def sandbox_execute(cmd):
        for name in ("flask", "click"):
            if name in cmd:
                installed.add(name)
        return True, "Successfully installed"

    def exec_readonly(cmd):
        return (0, "") if any(n in cmd and n in installed for n in ("flask", "click")) else (1, "no")

    new, reports, steps = emit_drain(
        g, ba, sandbox_execute, ActionLedger(), exec_readonly,
        step_offset=0, cycle=1,
    )
    assert new.get("pkg:flask").state is State.SATISFIED
    assert new.get("pkg:click").state is State.SATISFIED
    assert len(ba.recipes) == 1            # one pip step, drained in one pass
    assert new.get("pkg:flask").attempts   # emit attempt recorded


def test_emit_drain_unlocks_build_from_source_across_passes():
    lxml = Node(id="pkg:lxml", type=NodeType.PACKAGE, name="lxml", layer=Layer.PIP,
                discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING, version="5.0",
                build_from_source=True, check_command='python -c "import lxml"')
    libxml = Node(id="syslib:libxml2", type=NodeType.SYSTEM_LIB, name="libxml2.so.2",
                  layer=Layer.SYSTEM, discovered_by=DiscoveredBy.PROBE, state=State.MISSING,
                  check_command="ldconfig -p | grep libxml2",
                  fix_candidates=("apt:libxml2-dev",), chosen_fix="apt:libxml2-dev")
    g = DepGraph(nodes=(lxml, libxml),
                 edges=(Edge(src="pkg:lxml", dst="syslib:libxml2", relation=EdgeType.REQUIRES),))
    ba = _FakeBuildAgent()
    done = set()

    def sandbox_execute(cmd):
        if "libxml2-dev" in cmd:
            done.add("libxml2")
        if "lxml==" in cmd or "lxml" in cmd and "pip install" in cmd:
            done.add("lxml")
        return True, "ok"

    def exec_readonly(cmd):
        if "libxml2" in cmd:
            return (0, "") if "libxml2" in done else (1, "")
        if "import lxml" in cmd:
            return (0, "") if "lxml" in done else (1, "")
        return (1, "")

    new, reports, steps = emit_drain(
        g, ba, sandbox_execute, ActionLedger(), exec_readonly, step_offset=0, cycle=1,
    )
    assert new.get("syslib:libxml2").state is State.SATISFIED
    assert new.get("pkg:lxml").state is State.SATISFIED
    assert len(ba.recipes) == 2            # pass 1: apt; pass 2: pip (after toolchain certified)


class _FailingBuildAgent:
    def __init__(self):
        self.recipes = []

    def run_recipe(self, recipe, sandbox_execute, ledger, step_offset=0):
        self.recipes.append(recipe)
        cmds = [CommandRecord(s.command, 1, "boom") for s in recipe.steps]
        return TaskReport("emit", "blocked", tuple(cmds), "failed", completed_steps=0)


def test_emit_drain_stops_re_emitting_after_backoff():
    # Fix #3: a perpetually-failing emit must stop after MAX_EMIT_ATTEMPTS passes
    # instead of re-emitting up to max_drain (=4) — and the node ends on the frontier.
    from python_deps.depgraph.emit import MAX_EMIT_ATTEMPTS, partition

    g = DepGraph(nodes=(_pkg("doomed"),))
    ba = _FailingBuildAgent()
    new, reports, steps = emit_drain(
        g, ba, lambda c: (False, "fail"), ActionLedger(), lambda c: (1, "no"),
        step_offset=0, cycle=1,
    )
    assert len(ba.recipes) == MAX_EMIT_ATTEMPTS          # backoff, not a max_drain loop
    assert "pkg:doomed" in {n.id for n in partition(new).frontier}
