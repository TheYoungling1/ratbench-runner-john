"""Change 2 (design: testgate-certify.md §3/§5.3): _dep_emit_phase's post-emit
``certify_refresh`` is redundant whenever the fresh replay was clean AND the
graph was already fully certified before it — the reinstall then reproduces
the identical container the start-of-cycle certify already certified against.
Skip iff ``_failed_node is None and _pre_fully_certified``; otherwise run it
every time (never leaves a stale MISSING node uncertified).

Harness mirrors tests/test_v3_replay_executor.py's package-install scenario.
Counts physical ``certify_refresh`` calls by monkeypatching
``src.envstate.depgraph_live.certify_refresh`` with a counting spy — it is
imported *inside* ``_dep_emit_phase`` on every call, so patching the module
attribute is intercepted by each fresh ``from ... import certify_refresh``.

Note: ``src.envstate.install_localizer`` also does
``from src.envstate.depgraph_live import certify_refresh`` — but at MODULE
import time (once), inside ``certify_reciped_only`` (called every
``_binding_emit``, i.e. once per cycle regardless of Change 2). That is a
SEPARATE, always-unconditional certify pass this design does not gate — it is
not part of the ":762"/":787" pair under test here. We force-import
``install_localizer`` before patching so its ``certify_refresh`` binding is
fixed to the real function ahead of time; otherwise, if this test file were
the very first thing in the process to trigger ``_binding_emit``, the
patch would be picked up by ``install_localizer``'s own import statement too,
inflating the count by one extra (unrelated) call per cycle.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

import src.envstate.install_localizer  # noqa: F401 — force real import before patching (see module docstring)
import src.envstate.depgraph_live as dl
from src.envstate.orchestrator import VERIFY_TEST_CMD, run_v3
from src.envstate.ledger import ActionLedger
from src.envstate.world_model import TaskReport, initial_map, merge_map
from src.sandbox import InstallResult
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Layer, Node, NodeType, State,
)


class _RecordingBuildAgent:
    """No-`.client` build agent — discover tasks route through the
    deterministic gate, and a failed install cannot spend a repair turn
    (the `getattr(build_agent, "client", None) is not None` guard fails)."""

    def __init__(self):
        self.tasks = []

    def run(self, task, sandbox_execute, ledger, step_offset=0, check=None, budget=None):
        self.tasks.append(task)
        return TaskReport(task_goal=task.goal, status="blocked", commands=(), learning="discover")

    def run_recipe(self, recipe, sandbox_execute, ledger, step_offset=0):
        return TaskReport(task_goal="r", status="done", commands=(), learning="ok")


class _NoopMaintainer:
    def update(self, world_map, report):
        return world_map


def _pkg_map():
    """WorldModelMap with one MISSING, reciped PACKAGE node (pip-installable)."""
    node = Node(
        id="pkg:requests", type=NodeType.PACKAGE, name="requests", version="2.31.0",
        layer=Layer.PIP, discovered_by=DiscoveredBy.STATIC_SCAN, state=State.MISSING,
        check_command="python3 -c 'import requests'",
    )
    base = initial_map(base_image="python:3.11-slim", workdir="/repo", language="python",
                       build_system="pip", repo_layout=())
    return merge_map(base, dep_graph=DepGraph().with_node(node))


def _build_harness():
    """Stateful fakes: exec_readonly reports the node MISSING until
    run_install_script flips `installed`. VERIFY_TEST_CMD always fails, so the
    scheduler stays on the discover-gate path every cycle once the node is
    SATISFIED (frontier empty -> run_tests() -> fail -> discover task) —
    max_cycles=2 gives 2 full `_dep_emit_phase` invocations."""
    state = {"installed": False}
    calls = {"reset": 0, "install": 0}

    def sandbox_execute(cmd):
        if cmd == VERIFY_TEST_CMD:
            return (False, "no tests ran")
        return (True, "ok")

    def exec_readonly(cmd):
        if "import requests" in cmd:
            return (0, "") if state["installed"] else (1, "ModuleNotFoundError")
        return (1, "")

    def reset_to_base():
        calls["reset"] += 1

    def run_install_script(script):
        calls["install"] += 1
        state["installed"] = True
        return InstallResult(rc=0, failing_command=None, lineno=None, stderr="")

    inputs = dict(
        build_agent=_RecordingBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_pkg_map(),
        ledger=ActionLedger(),
        sandbox_execute=sandbox_execute,
        max_cycles=2,
        exec_readonly=exec_readonly,
        enable_dep_emit=True,
        reset_to_base=reset_to_base,
        run_install_script=run_install_script,
    )
    return inputs, calls


def _spy_certify(monkeypatch):
    n = {"count": 0}
    real = dl.certify_refresh

    def spy(*a, **k):
        n["count"] += 1
        return real(*a, **k)

    monkeypatch.setattr(dl, "certify_refresh", spy)
    return n


def test_787_skipped_in_steady_state(monkeypatch):
    # Same package harness: cycle 1 installs (MISSING@start -> post-emit certify
    # runs -> SATISFIED); cycle 2 is steady state (SATISFIED@start, clean replay
    # -> post-emit certify SKIPPED).
    n = _spy_certify(monkeypatch)
    inputs, _calls = _build_harness()
    final_map, stop = run_v3(**inputs)
    assert stop == "max_cycles"
    # cycle 1: start-of-cycle + post-emit = 2 ; cycle 2: start-of-cycle only
    # (post-emit skipped) = 1  -> total 3 (was 4 before Change 2).
    assert n["count"] == 3, n
    assert final_map.dep_graph.get("pkg:requests").state is State.SATISFIED


def test_787_runs_when_install_fails(monkeypatch):
    # A failing fresh replay (_failed_node is not None) must force the
    # post-emit certify every cycle it runs — never skip on a failure.
    n = _spy_certify(monkeypatch)
    inputs, _calls = _build_harness()

    def failing_install(script):
        return InstallResult(rc=1, failing_command="pip install requests",
                             lineno=1, stderr="boom")

    inputs["run_install_script"] = failing_install
    inputs["build_agent"] = _RecordingBuildAgent()   # no .client -> no repair, honest giveup
    final_map, stop = run_v3(**inputs)
    assert stop == "max_cycles"
    # Node never installs -> _pre_fully_certified is False AND _failed_node is
    # not None every cycle -> the post-emit certify runs every cycle: 2
    # certify calls (start + post-emit) per cycle * 2 cycles run = 4.
    assert n["count"] == 2 * 2, n
    assert final_map.dep_graph.get("pkg:requests").state is State.MISSING
