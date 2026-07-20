"""Change 1 (design: testgate-certify.md §1/§5.2): run_v3's discover cycle used
to run VERIFY_TEST_CMD TWICE per cycle — once as the scheduler's test probe
(``_run_tests_verified``) and once as the discover gate (``_run_discover_gate``)
— against a container that nothing mutated in between. ``VerifyTestCache``
collapses that into ONE physical pytest execution per cycle, keyed on a
container-generation token that is bumped by every ``reset_to_base``/
``run_install_script`` call so a stale pass/fail can never be served across a
mutation (e.g. the next cycle's fresh replay).

Harness mirrors tests/test_v3_replay_executor.py's discover-task-only scenario
(VERIFY_TEST_CMD always "fails" so the scheduler never hands out a targeted
obligation — the discover-gate path runs every cycle).
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.envstate.orchestrator import VERIFY_TEST_CMD, run_v3
from src.envstate.ledger import ActionLedger
from src.envstate.world_model import TaskReport, initial_map, merge_map
from src.sandbox import InstallResult
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Layer, Node, NodeType, State,
)


class _RecordingBuildAgent:
    """No-`.client` build agent — discover tasks route through the
    deterministic gate (`_run_discover_gate`), not `.run`/`.propose`."""

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
    scheduler stays on the discover-gate path every cycle — the discover gate
    is deterministic and does not spend `_repair_turns`, so the run terminates
    via `max_cycles` (not the LLM-turn budget), giving the test 2 full cycles
    to observe."""
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


def test_discover_cycle_runs_pytest_once_and_reruns_next_cycle():
    verify_calls = {"n": 0}
    inputs, _calls = _build_harness()
    raw_exec = inputs["sandbox_execute"]

    def counting_exec(cmd):
        if cmd == VERIFY_TEST_CMD:
            verify_calls["n"] += 1
        return raw_exec(cmd)
    inputs["sandbox_execute"] = counting_exec

    final_map, stop = run_v3(**inputs)
    assert stop == "max_cycles"

    # Memo: ONE physical pytest per discover cycle (was TWO: scheduler probe +
    # discover gate). Invalidation: the per-cycle reset bumps the generation, so
    # cycle 2 does NOT reuse cycle 1's result -> exactly one run PER cycle == 2.
    #   under-dedup (no memo) would give 4; over-dedup/stale (broken bump) -> 1.
    assert verify_calls["n"] == 2, verify_calls

    # The discover gate still writes a ledger event with the memoized raw
    # result (behavior preserved) — one per cycle, all failing.
    evts = [e for e in inputs["ledger"].events() if e.cmd == VERIFY_TEST_CMD]
    assert evts and all(e.rc != 0 for e in evts)
