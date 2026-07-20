"""Task 8d: pure proof-harness helpers (no Docker, no orchestrator wiring).

Covers ``src/envstate/proof.py``:
  1. ``finalize_trace``     — snapshot + 3-verifier report from a tracer +
     gate_observer accumulator + rendered script text.
  2. ``canonical_success``  — the composite per-repo proof predicate.
  3. ``repo_row``           — the per-repo proof-table row shape.
  4. ``aggregate``          — the report's aggregate counters.
  5. ``trace_from_dict``    — JSON round-trip inverse of ``RunTrace.to_dict()``.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.envstate.proof import (
    aggregate,
    canonical_success,
    finalize_trace,
    repo_row,
    trace_from_dict,
)
from src.envstate.run_trace import (
    DiscoverRecord,
    FreshReplayRecord,
    PatchGateRecord,
    RunTrace,
    RunTracer,
)


@dataclass(frozen=True)
class _FakeGate:
    """Duck-typed stand-in for ``src.envstate.gates.GateResult`` — proof.py
    never imports the real type, only reads ``.name``/``.passed``/
    ``.provisional``/``.evidence`` off whatever the driver's gate_observer saw.
    """
    name: str
    passed: bool
    provisional: bool
    evidence: str = ""


def _green_replay(**overrides) -> FreshReplayRecord:
    base = dict(
        ran=True, setup_rc=0, failing_command=None,
        certified_node_ids=("pkg:requests",), unsatisfied_node_ids=(),
        test_rc=0, test_summary="1 passed in 0.01s",
    )
    base.update(overrides)
    return FreshReplayRecord(**base)


def _clean_trace(**overrides) -> RunTrace:
    base = dict(
        repo="acme/widget", loop_mode="v3_graph_typed_repair",
        used_emit_drain=False, used_repair_failed_nodes=False,
        used_build_agent_run=False, used_block_emit=False,
        patchgate=(), discover=(), replays=(_green_replay(),),
        manual_block_ids=(), stop_reason="planner_done",
        gates={"installability": {"provisional": False, "passed": True}},
    )
    base.update(overrides)
    return RunTrace(**base)


_CLEAN_SCRIPT = "#!/bin/bash\npip install requests\n"


# ---------------------------------------------------------------------------
# finalize_trace
# ---------------------------------------------------------------------------

def test_finalize_trace_clean_report_is_all_empty():
    tracer = RunTracer(repo="acme/widget")
    tracer.record_replay(_green_replay())
    gates_seen = [(
        _FakeGate(name="installability", passed=True, provisional=False),
        _FakeGate(name="testability", passed=True, provisional=False),
    )]
    trace, report = finalize_trace(tracer, "planner_done", gates_seen, _CLEAN_SCRIPT)

    assert isinstance(trace, RunTrace)
    assert trace.stop_reason == "planner_done"
    assert report == {"canonical": [], "artifact": [], "local_import": []}


def test_finalize_trace_captures_last_gate_observation_only():
    tracer = RunTracer()
    tracer.record_replay(_green_replay())
    stale = (_FakeGate(name="installability", passed=False, provisional=True),)
    fresh = (
        _FakeGate(name="installability", passed=True, provisional=False, evidence="rc=0"),
        _FakeGate(name="testability", passed=True, provisional=False, evidence="pass_rate>=0.8"),
    )
    trace, _report = finalize_trace(tracer, "planner_done", [stale, fresh], _CLEAN_SCRIPT)

    assert trace.gates == {
        "installability": {"passed": True, "provisional": False, "evidence": "rc=0"},
        "testability": {"passed": True, "provisional": False, "evidence": "pass_rate>=0.8"},
    }


def test_finalize_trace_empty_gates_seen_yields_empty_gates_dict():
    tracer = RunTracer()
    tracer.record_replay(_green_replay())
    trace, _report = finalize_trace(tracer, "max_cycles", [], _CLEAN_SCRIPT)
    assert trace.gates == {}


def test_finalize_trace_legacy_mark_appears_in_canonical_report():
    # Non-done stop_reason so the isolated assertion below is just the legacy
    # mark, not compounded with the "installability gate still provisional"
    # check that verify_canonical_trace also runs on a done stop_reason.
    tracer = RunTracer()
    tracer.mark_emit_drain()
    tracer.record_replay(_green_replay())
    trace, report = finalize_trace(tracer, "planner_giveup", [], _CLEAN_SCRIPT)
    assert report["canonical"] == ["legacy emit_drain executed in canonical run"]
    assert trace.used_emit_drain is True


def test_finalize_trace_manual_block_missing_from_script_appears_in_artifact_report():
    tracer = RunTracer()
    tracer.record_replay(_green_replay())
    tracer.set_manual_blocks(("block.1",))
    trace, report = finalize_trace(tracer, "planner_giveup", [], _CLEAN_SCRIPT)
    assert report["artifact"] == ["governed manual block block.1 missing from final setup.sh"]
    assert trace.manual_block_ids == ("block.1",)


# ---------------------------------------------------------------------------
# canonical_success
# ---------------------------------------------------------------------------

def test_canonical_success_true_for_clean_done_green_trace():
    assert canonical_success(_clean_trace(), _CLEAN_SCRIPT) is True


def test_canonical_success_false_when_stop_reason_not_done():
    trace = _clean_trace(stop_reason="planner_giveup")
    assert canonical_success(trace, _CLEAN_SCRIPT) is False


def test_canonical_success_false_when_setup_rc_nonzero():
    trace = _clean_trace(replays=(_green_replay(setup_rc=1, failing_command="pip install -r requirements.txt"),))
    assert canonical_success(trace, _CLEAN_SCRIPT) is False


def test_canonical_success_false_when_test_rc_nonzero():
    trace = _clean_trace(replays=(_green_replay(test_rc=1, test_summary="1 failed"),))
    assert canonical_success(trace, _CLEAN_SCRIPT) is False


def test_canonical_success_false_when_legacy_path_used():
    trace = _clean_trace(used_build_agent_run=True)
    assert canonical_success(trace, _CLEAN_SCRIPT) is False


def test_canonical_success_false_when_manual_block_missing_from_artifact():
    trace = _clean_trace(manual_block_ids=("block.1",))
    assert canonical_success(trace, _CLEAN_SCRIPT) is False
    assert canonical_success(trace, _CLEAN_SCRIPT + "# block.1\n") is True


def test_canonical_success_false_when_unsatisfied_nodes_remain():
    trace = _clean_trace(replays=(_green_replay(unsatisfied_node_ids=("apt:libfoo",)),))
    assert canonical_success(trace, _CLEAN_SCRIPT) is False


def test_canonical_success_false_when_no_replays_ran():
    trace = _clean_trace(replays=())
    assert canonical_success(trace, _CLEAN_SCRIPT) is False


# ---------------------------------------------------------------------------
# repo_row
# ---------------------------------------------------------------------------

_EXPECTED_ROW_KEYS = {
    "repo", "result", "legacy_used", "graph_nodes_added", "patchgate_accepts",
    "manual_blocks", "fresh_replay", "tests_pass", "residual_reason",
}


def test_repo_row_shape_has_the_nine_documented_columns():
    row = repo_row(_clean_trace())
    assert set(row.keys()) == _EXPECTED_ROW_KEYS


def test_repo_row_values_for_a_passing_trace():
    row = repo_row(_clean_trace(repo="acme/widget"))
    assert row["repo"] == "acme/widget"
    assert row["result"] == "PASS"
    assert row["legacy_used"] is False
    assert row["fresh_replay"] is True
    assert row["tests_pass"] is True
    assert row["residual_reason"] == ""


def test_repo_row_failing_trace_reports_residual_reason():
    row = repo_row(_clean_trace(stop_reason="planner_giveup", replays=(_green_replay(setup_rc=1, failing_command="x"),)))
    assert row["result"] == "FAIL"
    assert row["fresh_replay"] is False
    assert row["residual_reason"] == "planner_giveup"


def test_repo_row_legacy_used_true_when_any_legacy_mark_set():
    assert repo_row(_clean_trace(used_repair_failed_nodes=True))["legacy_used"] is True
    assert repo_row(_clean_trace(used_block_emit=True))["legacy_used"] is True
    assert repo_row(_clean_trace())["legacy_used"] is False


def test_repo_row_graph_nodes_added_unions_patchgate_and_discover_new_ids():
    pg = PatchGateRecord(
        cycle=1, failed_block_id=None, evidence_ref="ev.1", accepted=True,
        accepted_node_ids=("apt:libgl1-mesa-glx",), accepted_block_ids=("block.1",), errors=(),
    )
    d = DiscoverRecord(
        cycle=1, command="python -m pytest -q", used_llm_mutation=False,
        new_node_ids=("pkg:requests", "apt:libgl1-mesa-glx"), diagnosis_modes=("missing_external_pkg",),
    )
    trace = _clean_trace(patchgate=(pg,), discover=(d,))
    row = repo_row(trace)
    assert row["graph_nodes_added"] == 2  # union, not sum (dedup on apt:libgl1-mesa-glx)
    assert row["patchgate_accepts"] == 1


def test_repo_row_manual_blocks_is_a_count():
    trace = _clean_trace(manual_block_ids=("block.1", "block.2"))
    assert repo_row(trace)["manual_blocks"] == 2


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------

def test_aggregate_clean_set_has_zero_violations():
    pairs = [(_clean_trace(repo=f"repo{i}"), _CLEAN_SCRIPT) for i in range(3)]
    agg = aggregate(pairs)
    assert agg == {
        "canonical_loop_runs": 3,
        "legacy_path_violations": 0,
        "fresh_replay_pass_rate": 1.0,
        "manual_block_artifact_mismatches": 0,
        "local_import_false_package_attempts": 0,
    }


def test_aggregate_counts_legacy_path_violation_from_one_tainted_trace():
    pairs = [
        (_clean_trace(repo="clean1"), _CLEAN_SCRIPT),
        (_clean_trace(repo="tainted", used_emit_drain=True), _CLEAN_SCRIPT),
    ]
    agg = aggregate(pairs)
    assert agg["legacy_path_violations"] == 1


def test_aggregate_counts_local_import_false_package_attempts():
    d = DiscoverRecord(
        cycle=1, command="python -m pytest -q", used_llm_mutation=False,
        new_node_ids=("pkg:docs-src",), diagnosis_modes=("repo_internal_reference",),
    )
    pairs = [(_clean_trace(discover=(d,)), _CLEAN_SCRIPT)]
    agg = aggregate(pairs)
    assert agg["local_import_false_package_attempts"] == 1


def test_aggregate_counts_manual_block_artifact_mismatch():
    pairs = [(_clean_trace(manual_block_ids=("block.1",)), _CLEAN_SCRIPT)]
    agg = aggregate(pairs)
    assert agg["manual_block_artifact_mismatches"] == 1


def test_aggregate_fresh_replay_pass_rate_is_a_fraction():
    pairs = [
        (_clean_trace(repo="a"), _CLEAN_SCRIPT),
        (_clean_trace(repo="b", replays=(_green_replay(setup_rc=1, failing_command="x"),)), _CLEAN_SCRIPT),
    ]
    agg = aggregate(pairs)
    assert agg["fresh_replay_pass_rate"] == 0.5


def test_aggregate_empty_traces_does_not_crash():
    agg = aggregate([])
    assert agg["canonical_loop_runs"] == 0
    assert agg["fresh_replay_pass_rate"] == 0.0


# ---------------------------------------------------------------------------
# trace_from_dict (JSON round trip — inverse of RunTrace.to_dict())
# ---------------------------------------------------------------------------

def test_trace_from_dict_round_trips_a_fully_populated_trace_through_json():
    pg = PatchGateRecord(
        cycle=1, failed_block_id="system.libplacebo", evidence_ref="ev.1", accepted=True,
        accepted_node_ids=("apt:libplacebo-dev",), accepted_block_ids=("block.1",), errors=(),
    )
    d = DiscoverRecord(
        cycle=1, command="python -m pytest -q", used_llm_mutation=False,
        new_node_ids=("pkg:requests",), diagnosis_modes=("missing_external_pkg",),
    )
    original = _clean_trace(patchgate=(pg,), discover=(d,), manual_block_ids=("block.1",))

    round_tripped = trace_from_dict(json.loads(json.dumps(original.to_dict())))

    assert round_tripped == original
    # Tuple-ness survived the list-coercion JSON imposes, not just equality
    # (list == tuple would fail this assert but pass a naive `==` on dicts).
    assert isinstance(round_tripped.patchgate[0].accepted_node_ids, tuple)
    assert isinstance(round_tripped.discover[0].new_node_ids, tuple)
    assert isinstance(round_tripped.replays[0].certified_node_ids, tuple)
    assert isinstance(round_tripped.manual_block_ids, tuple)


def test_trace_from_dict_defaults_missing_keys():
    trace = trace_from_dict({})
    assert trace == RunTrace()
