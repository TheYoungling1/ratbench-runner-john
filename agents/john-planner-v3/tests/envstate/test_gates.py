"""Phase 7: installability gate binding by construction (from the per-cycle replay).

Under Model B, run_v3's sole executor is a fresh full-script replay from base
every cycle (no memoization) — there is no separate terminal-replay step, so
the latest cycle's InstallResult IS the installability proof. These tests
cover:

  1. ``evaluate_installability_gate(graph, replay=...)`` is BINDING (not
     provisional) whenever a real replay result is supplied — both the
     rc=0/passed and rc!=0/failed cases.
  2. A full ``run_v3`` run to "done" (via a fake sandbox that always returns
     rc=0) reports ``provisional is False`` for the installability gate —
     i.e. the canonical path never falls back to the graph-frontier heuristic.

The existing provisional-path tests (``replay=None``, used only by the
block_emit ablation) live in test_gates_installability.py and are untouched.
"""
import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.envstate.gates import evaluate_installability_gate
from src.envstate.ledger import ActionLedger
from src.envstate.orchestrator import VERIFY_TEST_CMD, run_v3
from src.envstate.world_model import initial_map, merge_map
from src.sandbox import InstallResult
from python_deps.depgraph.schema import DepGraph, DiscoveredBy, Layer, Node, NodeType, State


# ---------------------------------------------------------------------------
# 1. evaluate_installability_gate(graph, replay=...) — pure unit tests
# ---------------------------------------------------------------------------

def test_installability_gate_binding_on_real_replay():
    class _R:
        rc = 0
        failing_command = None

    g = evaluate_installability_gate(None, replay=_R())
    assert g.passed is True
    assert g.provisional is False
    assert "fresh replay rc=0" in g.evidence
    assert g.command == "fresh-from-base setup.sh replay"


def test_installability_gate_binding_fail():
    class _R:
        rc = 1
        failing_command = "apt-get install -y libpq-dev"

    g = evaluate_installability_gate(None, replay=_R())
    assert g.passed is False
    assert g.provisional is False
    assert "libpq-dev" in g.evidence


# ---------------------------------------------------------------------------
# 2. run_v3 to "done" via a fake sandbox — the gate reported must be binding
# ---------------------------------------------------------------------------

class _NoClientBuildAgent:
    """No `.client` -> discover tasks would route through the deterministic
    gate, and typed repair is never reachable. Unused here (the harness never
    fails), kept only so run_v3's call sites resolve `getattr(..., "client")`
    safely."""


class _NoopMaintainer:
    def update(self, world_map, report):
        return world_map


def _pkg_map():
    node = Node(
        id="pkg:requests", type=NodeType.PACKAGE, name="requests", version="2.31.0",
        layer=Layer.PIP, discovered_by=DiscoveredBy.STATIC_SCAN, state=State.MISSING,
        check_command="python3 -c 'import requests'",
    )
    base = initial_map(base_image="python:3.11-slim", workdir="/repo", language="python",
                       build_system="pip", repo_layout=())
    return merge_map(base, dep_graph=DepGraph().with_node(node))


def _run_v3_to_done_with_fake_sandbox(tracer=None):
    """Fake sandbox that installs the one MISSING package node on the first
    replay and reports success from then on — the scheduler reaches "done" on
    cycle 1 (frontier empties + tests pass) with a real rc=0 InstallResult
    already recorded as ``_last_replay_result``.

    ``tracer`` (optional, Task 8 gap-fix regression test) — threaded straight
    through to ``run_v3`` so a caller can snapshot it afterwards and inspect
    the recorded ``FreshReplayRecord``s.
    """
    state = {"installed": False}

    def sandbox_execute(cmd):
        if cmd == VERIFY_TEST_CMD:
            # The anti-hollow done-gate (_verified_test_run_passed) requires a
            # genuine execution summary ("N passed"), not just rc=0/ok=True.
            return (
                (state["installed"], "1 passed in 0.01s")
                if state["installed"]
                else (False, "no tests ran")
            )
        return (True, "ok")

    def exec_readonly(cmd):
        if "import requests" in cmd:
            return (0, "") if state["installed"] else (1, "ModuleNotFoundError")
        return (1, "")

    def reset_to_base():
        pass

    def run_install_script(script):
        state["installed"] = True
        return InstallResult(rc=0, failing_command=None, lineno=None, stderr="")

    captured: dict = {}

    def gate_observer(gates):
        installability, testability = gates
        captured["gates"] = {
            "installability": dataclasses.asdict(installability),
            "testability": dataclasses.asdict(testability),
        }

    final_map, stop = run_v3(
        build_agent=_NoClientBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_pkg_map(),
        ledger=ActionLedger(),
        sandbox_execute=sandbox_execute,
        max_cycles=3,
        exec_readonly=exec_readonly,
        enable_dep_emit=True,
        reset_to_base=reset_to_base,
        run_install_script=run_install_script,
        enable_gate_observability=True,
        gate_observer=gate_observer,
        tracer=tracer,
    )
    return SimpleNamespace(final_map=final_map, stop=stop, gates=captured["gates"])


def test_test_rc_backfilled_makes_canonical_success_reachable():
    """Task 8 test_rc back-fill gap-fix: before wiring ``_run_tests_verified``
    to ``tracer.set_last_replay_tests``, ``trace.last_replay.test_rc`` was
    ALWAYS None on every real run (the test gate is a separate call from the
    fresh-replay executor that produces ``FreshReplayRecord``s), so
    ``proof.canonical_success`` could never be True on a real trace. A clean
    run to "done" (rc=0 replay, passing anti-hollow test gate) must now
    back-fill the LAST replay's ``test_rc`` to 0, and ``canonical_success``
    must be reachable end-to-end.
    """
    from python_deps.depgraph.build_script import render_build_script
    from src.envstate.proof import canonical_success
    from src.envstate.run_trace import RunTracer

    tracer = RunTracer(repo="acme/widget")
    result = _run_v3_to_done_with_fake_sandbox(tracer=tracer)
    assert result.stop == "planner_done"

    trace = tracer.snapshot(stop_reason=result.stop, gates=result.gates)

    assert trace.last_replay is not None
    assert trace.last_replay.setup_rc == 0
    assert trace.last_replay.test_rc == 0

    script_text = render_build_script(
        result.final_map.dep_graph, getattr(result.final_map, "manual_blocks", ())
    )
    assert canonical_success(trace, script_text) is True


def test_done_reports_binding_gate_not_provisional():
    trace = _run_v3_to_done_with_fake_sandbox()
    assert trace.stop == "planner_done"
    assert trace.gates["installability"]["provisional"] is False
    assert trace.gates["installability"]["passed"] is True
    assert "fresh replay rc=0" in trace.gates["installability"]["evidence"]


# ---------------------------------------------------------------------------
# 3. Review fix wave: BOTH success doors (scheduler "done" AND maintainer
#    done_flag) must be bound to a green replay via `_finalize_if_replayed`.
# ---------------------------------------------------------------------------

def test_done_with_failed_replay_gives_up():
    """Scheduler reaches action='done' (trivially-satisfiable empty graph +
    passing tests) on cycle 1, but the SAME cycle's fresh replay that ran
    just before the decision returned rc=1. The old inline done-guard covered
    this branch in theory but had zero test coverage — this pins it: 'done'
    must downgrade to GIVEUP_REPLAY (stop == 'planner_giveup'), never
    'planner_done'.
    """
    node = Node(
        id="pkg:requests", type=NodeType.PACKAGE, name="requests", version="2.31.0",
        layer=Layer.PIP, discovered_by=DiscoveredBy.STATIC_SCAN, state=State.SATISFIED,
        check_command="python3 -c 'import requests'",
    )
    base = initial_map(base_image="python:3.11-slim", workdir="/repo", language="python",
                       build_system="pip", repo_layout=())
    # Already-SATISFIED node -> scheduler_frontier is empty from cycle 1 -> the
    # scheduler decides "done" purely from frontier+tests, independent of the
    # replay outcome recorded this same cycle.
    world = merge_map(base, dep_graph=DepGraph().with_node(node))

    def sandbox_execute(cmd):
        if cmd == VERIFY_TEST_CMD:
            return (True, "1 passed in 0.01s")
        return (True, "ok")

    def exec_readonly(cmd):
        return (0, "") if "import requests" in cmd else (1, "")

    def reset_to_base():
        pass

    def run_install_script(script):
        # The fresh replay itself fails even though every node was already
        # certified SATISFIED going in (e.g. a system-level regression) —
        # the "done" decision must not be trusted over this.
        return InstallResult(rc=1, failing_command="pip install -r requirements.txt",
                             lineno=None, stderr="boom")

    final_map, stop = run_v3(
        build_agent=_NoClientBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=world,
        ledger=ActionLedger(),
        sandbox_execute=sandbox_execute,
        max_cycles=3,
        exec_readonly=exec_readonly,
        enable_dep_emit=True,
        reset_to_base=reset_to_base,
        run_install_script=run_install_script,
    )
    assert stop == "planner_giveup"
    assert stop != "planner_done"


def test_done_flag_without_green_replay_gives_up():
    """Mirrors the run_v1 done_flag tests (pre-set done_flag=True on the
    initial map, NoopMaintainer preserves it). With ``dep_graph=None``,
    ``_dep_emit_phase`` never calls ``_binding_emit`` (existing R3(c) guard),
    so ``_last_replay_result`` stays None for the whole run even though
    done_flag is set going into cycle 1's task branch. Before the fix, the
    ``current_map.done_flag`` hard-stop returned TerminationReason.DONE_FLAG
    unconditionally — a second, ungrounded success door alongside the
    scheduler's 'done' decision. Now it must route through the same
    ``_finalize_if_replayed`` guard and give up instead.
    """
    base = initial_map(base_image="python:3.11-slim", workdir="/repo", language="python",
                       build_system="pip", repo_layout=())
    # dep_graph stays None (default) so _dep_emit_phase/_binding_emit never runs
    # and reset_to_base/run_install_script must never be called either.
    world = merge_map(base, done_flag=True)

    def sandbox_execute(cmd):
        # Tests never pass, so the scheduler's own "done" decision is
        # unreachable (frontier is empty for a None graph too, but
        # run_tests() must be False here to force the "task" -> discover
        # branch, where the pre-set done_flag is what triggers the exit).
        return (False, "not ready")

    def reset_to_base():
        raise AssertionError("reset_to_base must never be called: dep_graph is None")

    def run_install_script(script):
        raise AssertionError("run_install_script must never be called: dep_graph is None")

    final_map, stop = run_v3(
        build_agent=_NoClientBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=world,
        ledger=ActionLedger(),
        sandbox_execute=sandbox_execute,
        max_cycles=3,
        reset_to_base=reset_to_base,
        run_install_script=run_install_script,
    )
    assert stop not in ("planner_done", "done_flag")
    assert stop == "planner_giveup"
