"""Tests for the residual-handler give-up gate in _runtime_ingest_phase.

Source-text assertions (below) confirm static wiring; the behavioral tests
further down confirm the dynamic semantics — specifically that a successful
pip-install event never triggers planner_giveup, and that _out_of_scope alone
(without a host-grounded diverged node) cannot finalize the run.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_SRC_DIR = _ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

_SRC = _ROOT / "src" / "envstate" / "orchestrator.py"


def test_residual_giveup_is_gated_and_never_sets_done_flag():
    src = _SRC.read_text()
    assert "_residual_giveup" in src
    # After the split, the residual logic lives in run_v3's _runtime_ingest_phase.
    run_v3_start = src.index("def run_v3(")
    run_v3_body = src[run_v3_start:]
    body = run_v3_body[run_v3_body.index("def _runtime_ingest_phase"):]
    assert "diverged_node_ids" in body          # divergence detector consumed
    assert "note_out_of_scope" in body          # non-env diagnoses captured
    assert "emittable" in body                  # frontier-clean guard present
    # _residual_giveup is set inside the ingest body
    assert "_residual_giveup" in body
    # The main loop checks it after ingest (not inside the closure)
    assert "if _residual_giveup is not None:" in run_v3_body
    # the residual give-up block must NOT write done_flag
    blk = body[body.index("_residual_giveup"):]
    assert "done_flag" not in blk[:600]


def test_llm_classifier_injected_only_under_graph_scheduler():
    src = _SRC.read_text()
    # After the split, make_llm_classifier lives exclusively in run_v3's
    # _runtime_ingest_phase (the gate is now at the function/dispatch level).
    run_v1_start = src.index("def run_v1(")
    run_v3_start = src.index("def run_v3(")
    run_v1_body = src[run_v1_start:run_v3_start]
    run_v3_body = src[run_v3_start:]
    v3_ingest_body = run_v3_body[run_v3_body.index("def _runtime_ingest_phase"):]
    # make_llm_classifier is in run_v3, not run_v1
    assert "make_llm_classifier" in v3_ingest_body
    assert "make_llm_classifier" not in run_v1_body, (
        "make_llm_classifier must NOT appear in run_v1"
    )
    # the deterministic classifier is always present
    assert "classify_observation" in v3_ingest_body
    # temperature 0 on the wrapped completion
    assert "temperature=0" in v3_ingest_body


# ── Behavioral tests (dynamic semantics) ─────────────────────────────────────

from src.envstate.ledger import ActionLedger, make_action_event
from src.envstate.orchestrator import run_v1, run_v3
from src.envstate.world_model import (
    PlannerDecision,
    Task,
    TaskReport,
    initial_map,
    merge_map,
)
from src.sandbox import InstallResult
from python_deps.depgraph.ids import TEST_NODE_ID
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Layer, Node, NodeType, State,
)


# run_v3 is fresh-replay-only (Phase 4): reset_to_base/run_install_script are
# mandatory. _no_missing_node_map() below has only a TEST node (not reciped),
# so the fresh-replay executor never has anything to actually install — these
# fakes just satisfy the guard and report a clean rc=0.
def _noop_reset_to_base() -> None:
    pass


def _noop_run_install_script(script: str) -> InstallResult:
    return InstallResult(rc=0, failing_command=None, lineno=None, stderr="")


class _ExplodingPlanner:
    """Never called in graph-scheduler mode — explodes if it is."""
    def decide(self, world_map):
        raise AssertionError("planner.decide must not be called in scheduler mode")


class _FakeClient:
    """Non-None sentinel so build_agent.client is truthy."""


class _LLMBuildAgent:
    """Build agent stub with non-None client and model (wires the LLM tier)."""
    client = _FakeClient()
    model = "test-model"

    def run(self, task, sandbox_execute, ledger, step_offset=0, check=None, budget=None):
        return TaskReport(task_goal="task", status="blocked", commands=(), learning="blocked")

    def run_recipe(self, recipe, sandbox_execute, ledger, step_offset=0):
        return TaskReport(task_goal="recipe", status="done", commands=(), learning="ok")


class _NoopMaintainer:
    def update(self, world_map, report):
        return world_map


_VERIFY_CMD = "python -m pytest -q"
_PASSING_OUTPUT = "1 passed in 0.01s\n"


def _sandbox_tests_pass(cmd):
    """Tests pass for VERIFY_TEST_CMD; everything else fails (installs etc.)."""
    if cmd == _VERIFY_CMD:
        return True, _PASSING_OUTPUT
    # shim / other commands: succeed silently
    return True, ""


def _exec_readonly_missing(cmd):
    """Certify reports everything as missing."""
    return 1, "not found"


def _no_missing_node_map():
    """A WorldModelMap whose dep-graph has only a TEST node (no MISSING installable).

    This ensures:
      - scheduler_frontier() == () (no actionable obligation)
      - partition().emittable == () (nothing to emit)
    """
    test_node = Node(
        id=TEST_NODE_ID,
        type=NodeType.TEST,
        name="repo_tests_pass",
        layer=Layer.TESTS,
        discovered_by=DiscoveredBy.GOAL,
    )
    base = initial_map(
        base_image="python:3.11",
        workdir="/repo",
        language="python",
        build_system="pip",
        repo_layout=(),
    )
    return merge_map(base, dep_graph=DepGraph().with_node(test_node))


def _ledger_with_success_event() -> ActionLedger:
    """Ledger pre-seeded with one SUCCESSFUL pip-install event (rc=0)."""
    ledger = ActionLedger()
    evt = make_action_event(
        step=1,
        cmd="python3 -m pip install --break-system-packages click==8.1.8",
        success=True,
        stdout=(
            "Collecting click==8.1.8\n"
            "  Downloading click-8.1.8-py3-none-any.whl\n"
            "Successfully installed click-8.1.8"
        ),
        env_revision_before=0,
        env_revision_after=1,
        mutation_class=None,
        container_id="test",
    )
    ledger.append(evt)
    return ledger


def _ledger_with_failed_event() -> ActionLedger:
    """Ledger pre-seeded with one FAILED event (rc=1) — non-env error."""
    ledger = ActionLedger()
    evt = make_action_event(
        step=1,
        cmd="python3 run_tests.py",
        success=False,
        stdout="AssertionError: expected 42 got 43",
        env_revision_before=0,
        env_revision_after=0,
        mutation_class=None,
        container_id="test",
    )
    ledger.append(evt)
    return ledger


# ── Primary test: bug reproduction ───────────────────────────────────────────

def test_out_of_scope_from_success_event_does_not_give_up_on_cycle1(monkeypatch):
    """A successful pip-install event must NOT trigger planner_giveup.

    Bug: the LLM classifier was called on rc=0 events, producing _out_of_scope,
    which combined with partition().emittable==() to fire the give-up gate
    before next_decision ever ran.
    Fix (Edit 2): filter obs to rc!=0 before passing to classifiers, so the LLM
    is never asked to "explain a failure" that did not happen.

    RED (pre-fix): stop == "planner_giveup"  (residual handler fires first)
    GREEN (post-fix): stop == "planner_done"  (tests pass; next_decision returns done)

    Note: sandbox_execute returns passing output for VERIFY_TEST_CMD so that
    next_decision (which runs only post-fix, once the residual handler is silent)
    sees green tests and returns "done" → "planner_done". The pre-fix path is
    terminated by the residual handler before next_decision is ever reached.
    """
    # Monkeypatch the LLM classifier import site in the orchestrator to inject a
    # fake that immediately calls note_out_of_scope (reproduces llm_classifier.py:75-78
    # on a non-env verdict). The orchestrator imports make_llm_classifier lazily
    # inside _runtime_ingest_phase via `from src.envstate.llm_classifier import ...`,
    # so patching the module attribute is the correct interception point.
    import src.envstate.llm_classifier as _llm_mod

    def _fake_make(complete_fn, *, note_out_of_scope=None):
        def _classify(cmd, out):
            # Unconditionally declare out-of-scope (simulates LLM verdict on a
            # successful install that looks like REPO_BUG to the model).
            if note_out_of_scope is not None:
                note_out_of_scope(cmd, "non-env: REPO_BUG (simulated)")
            return None
        return _classify

    monkeypatch.setattr(_llm_mod, "make_llm_classifier", _fake_make)

    final_map, stop = run_v3(
        build_agent=_LLMBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_no_missing_node_map(),
        ledger=_ledger_with_success_event(),
        sandbox_execute=_sandbox_tests_pass,
        max_cycles=2,
        exec_readonly=_exec_readonly_missing,
        enable_dep_emit=True,
        enable_runtime_feedback=True,
        reset_to_base=_noop_reset_to_base,
        run_install_script=_noop_run_install_script,
    )
    # Post-fix: successful events are filtered out (rc != 0), so obs is empty,
    # _runtime_ingest_phase returns early, _residual_giveup stays None.
    # next_decision then runs and sees passing tests → returns "done" → planner_done.
    # Pre-fix: _out_of_scope is non-empty + no emittable → residual handler fires
    # → planner_giveup before next_decision is reached.
    assert stop != "planner_giveup", (
        f"got {stop!r} — successful pip-install events must not trigger planner_giveup"
    )
    assert stop == "planner_done", (
        f"expected 'planner_done' (tests pass post-fix) but got {stop!r}"
    )


# ── Companion test: locks Edit 1 ─────────────────────────────────────────────

def _ledger_with_module_not_found_event() -> ActionLedger:
    """Ledger pre-seeded with a FAILED event the deterministic classifier maps
    to a RUNTIME Package node (ModuleNotFoundError -> package 'PyYAML')."""
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


def test_runtime_ingest_merges_discovered_node_on_graph_arm():
    """Regression: a runtime failure must be ingested AND merged into the live
    graph on the graph-scheduler arm.

    Guards a real shipped bug: `_runtime_ingest_phase` referenced `os.environ`
    while `orchestrator.py` did not `import os`, so every cycle raised NameError
    *before* the merge_map that writes discovered nodes back. The bare
    `except Exception` suppressed it, so the runtime-feedback loop was silently
    dead on v1gs/v1gsp/v3 and no existing test caught it (they assert the
    ABSENCE of give-up, which the NameError also produced).

    RED (missing `import os`): NameError -> merge skipped -> node absent.
    GREEN (with `import os`): node merged into final_map.dep_graph.
    """
    final_map, _stop = run_v3(
        build_agent=_LLMBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_no_missing_node_map(),
        ledger=_ledger_with_module_not_found_event(),
        sandbox_execute=_sandbox_tests_pass,
        max_cycles=1,
        exec_readonly=_exec_readonly_missing,
        enable_dep_emit=True,
        enable_runtime_feedback=True,
        reset_to_base=_noop_reset_to_base,
        run_install_script=_noop_run_install_script,
    )
    merged = [
        n for n in final_map.dep_graph.nodes
        if n.type is NodeType.PACKAGE and n.name == "PyYAML"
        and n.discovered_by is DiscoveredBy.RUNTIME
    ]
    assert merged, (
        "runtime-discovered Package node 'PyYAML' was not merged into the live "
        "graph — _runtime_ingest_phase raised before merge_map (missing import?)"
    )


def test_out_of_scope_without_divergence_does_not_finalize_giveup(monkeypatch):
    """_out_of_scope alone (no host-grounded diverged node) must not give up.

    This locks Edit 1: the give-up condition requires diverged AND no_actionable
    AND not emittable. Without a diverged node the gate is False regardless of
    _out_of_scope.

    RED (pre-fix): stop == "planner_giveup"  (buggy: _out_of_scope alone suffices)
    GREEN (post-fix): stop == "planner_done"  (gate is False; tests pass)
    """
    import src.envstate.llm_classifier as _llm_mod

    def _fake_make(complete_fn, *, note_out_of_scope=None):
        def _classify(cmd, out):
            if note_out_of_scope is not None:
                note_out_of_scope(cmd, "non-env: REPO_BUG (simulated)")
            return None
        return _classify

    monkeypatch.setattr(_llm_mod, "make_llm_classifier", _fake_make)

    # Failed event (rc=1) — passes the rc!=0 filter in Edit 2 — but produces no
    # diverged node (the classifier returns None and does not add a graph node).
    # sandbox_execute returns passing output for VERIFY_TEST_CMD so that
    # next_decision (reached only post-fix) returns done → planner_done.
    final_map, stop = run_v3(
        build_agent=_LLMBuildAgent(),
        maintainer=_NoopMaintainer(),
        initial_world_map=_no_missing_node_map(),
        ledger=_ledger_with_failed_event(),
        sandbox_execute=_sandbox_tests_pass,
        max_cycles=2,
        exec_readonly=_exec_readonly_missing,
        enable_dep_emit=True,
        enable_runtime_feedback=True,
        reset_to_base=_noop_reset_to_base,
        run_install_script=_noop_run_install_script,
    )
    # Post-fix: diverged is empty (no node was added → no divergence possible),
    # so the gate `diverged and no_actionable and not emittable` is False.
    # next_decision sees passing tests → planner_done.
    assert stop != "planner_giveup", (
        f"got {stop!r} — _out_of_scope alone (no diverged node) must not trigger planner_giveup"
    )
    assert stop == "planner_done", (
        f"expected 'planner_done' (tests pass post-fix) but got {stop!r}"
    )
