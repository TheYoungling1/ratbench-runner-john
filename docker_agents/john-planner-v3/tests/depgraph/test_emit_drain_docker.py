"""Task 10 — Docker integration test: emit certifies; wrong emit self-escalates.

Proves end-to-end that:
  (a) a real emit flips a resolved node to SATISFIED inside a live container, and
  (b) a deliberately-wrong apt package name leaves the node MISSING so it falls
      back to FRONTIER (the safety valve — spec §9).

Run (requires Docker, python:3.11-slim pre-pulled):

    pytest tests/depgraph/test_emit_drain_docker.py -v

Skips cleanly when the ``docker`` binary is absent.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

# sys.path: repo root (for src.envstate.*) + src/ (for python_deps.*)
_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"
for _p in (_REPO, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.envstate.depgraph_live import emit_drain  # noqa: E402
from src.envstate.ledger import ActionLedger  # noqa: E402
from src.envstate.world_model import TaskReport, CommandRecord  # noqa: E402
from python_deps.depgraph.executor import DockerExecutor  # noqa: E402
from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph, Node, NodeType, Layer, State, DiscoveredBy,
)

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker required")


class _DirectBuildAgent:
    """Stand-in build agent that runs each emitted command verbatim (no LLM)."""

    def __init__(self, ex):
        self.ex = ex

    def run_recipe(self, recipe, sandbox_execute, ledger, step_offset=0):
        cmds = []
        ok_all = True
        done = 0
        for s in recipe.steps:
            r = self.ex.run(s.command, timeout=600)
            cmds.append(CommandRecord(s.command, r.returncode, (r.stdout + r.stderr)[-500:]))
            if r.ok:
                done += 1
            else:
                ok_all = False
                break
        return TaskReport(
            "emit",
            "done" if ok_all else "blocked",
            tuple(cmds),
            "ok" if ok_all else "fail",
            completed_steps=done,
        )


def _pkg(name, version):
    return Node(
        id=f"pkg:{name}",
        type=NodeType.PACKAGE,
        name=name,
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
        state=State.MISSING,
        version=version,
        check_command=f'python -c "import {name}"',
    )


def test_real_emit_certifies_resolved_package():
    with DockerExecutor("python:3.11-slim") as ex:
        g = DepGraph(nodes=(_pkg("click", "8.1.7"),))
        ba = _DirectBuildAgent(ex)
        new, reports, steps = emit_drain(
            g, ba,
            lambda c: (ex.run(c).ok, ""),                          # sandbox_execute
            ActionLedger(),                                        # ledger
            lambda c: (ex.run(c).returncode, ex.run(c).stdout),   # exec_readonly
            step_offset=0, cycle=1,
        )
        assert new.get("pkg:click").state is State.SATISFIED


def test_wrong_apt_name_self_escalates_to_frontier():
    with DockerExecutor("python:3.11-slim") as ex:
        bad = Node(
            id="tool:nope",
            type=NodeType.TOOL,
            name="nope",
            layer=Layer.TOOLCHAIN,
            discovered_by=DiscoveredBy.PROBE,
            state=State.MISSING,
            check_command="command -v nope",
            fix_candidates=("apt:this-apt-pkg-does-not-exist",),
            chosen_fix="apt:this-apt-pkg-does-not-exist",
        )
        g = DepGraph(nodes=(bad,))
        ba = _DirectBuildAgent(ex)
        new, reports, steps = emit_drain(
            g, ba,
            lambda c: (ex.run(c).ok, ""),                          # sandbox_execute
            ActionLedger(),                                        # ledger
            lambda c: (ex.run(c).returncode, ex.run(c).stdout),   # exec_readonly
            step_offset=0, cycle=1,
        )
        # emit failed in-container; the node stays MISSING -> escalates to the LLM
        assert new.get("tool:nope").state is State.MISSING
