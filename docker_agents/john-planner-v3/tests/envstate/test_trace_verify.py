"""Task 8b: pure assertion functions over a RunTrace (no orchestrator wiring).

Covers the three proof claims from the e2e-proof design:
  1. verify_canonical_trace(t)          — the canonical loop was used.
  2. verify_artifact_consistency(...)   — the graph/script/fresh-replay
     contract holds (rendered setup.sh matches what was governed).
  3. verify_local_import_guard(t)       — the legacy "repo-local import
     mistaken for a missing package" path did no work.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.envstate.run_trace import DiscoverRecord, FreshReplayRecord, RunTrace
from src.envstate.trace_verify import (
    verify_artifact_consistency,
    verify_canonical_trace,
    verify_local_import_guard,
)


def _green_replay(**overrides) -> FreshReplayRecord:
    base = dict(
        ran=True,
        setup_rc=0,
        failing_command=None,
        certified_node_ids=("pkg:requests",),
        unsatisfied_node_ids=(),
        test_rc=0,
        test_summary="1 passed in 0.01s",
    )
    base.update(overrides)
    return FreshReplayRecord(**base)


def _clean_trace(**overrides) -> RunTrace:
    base = dict(
        repo="acme/widget",
        loop_mode="v3_graph_typed_repair",
        used_emit_drain=False,
        used_repair_failed_nodes=False,
        used_build_agent_run=False,
        used_block_emit=False,
        patchgate=(),
        discover=(),
        replays=(_green_replay(),),
        manual_block_ids=(),
        stop_reason="planner_done",
        gates={"installability": {"provisional": False, "passed": True}},
    )
    base.update(overrides)
    return RunTrace(**base)


# ---------------------------------------------------------------------------
# verify_canonical_trace
# ---------------------------------------------------------------------------

def test_clean_canonical_trace_has_no_errors():
    assert verify_canonical_trace(_clean_trace()) == []


def test_used_emit_drain_is_one_error():
    errs = verify_canonical_trace(_clean_trace(used_emit_drain=True))
    assert errs == ["legacy emit_drain executed in canonical run"]


def test_used_repair_failed_nodes_is_one_error():
    errs = verify_canonical_trace(_clean_trace(used_repair_failed_nodes=True))
    assert errs == ["legacy repair_failed_nodes executed"]


def test_used_build_agent_run_is_one_error():
    errs = verify_canonical_trace(_clean_trace(used_build_agent_run=True))
    assert errs == ["free-text build_agent.run executed"]


def test_used_block_emit_is_one_error():
    errs = verify_canonical_trace(_clean_trace(used_block_emit=True))
    assert errs == ["block_emit ablation executed inside the method"]


def test_non_canonical_loop_mode_is_an_error():
    errs = verify_canonical_trace(_clean_trace(loop_mode="ablation_block_emit"))
    assert errs == ["non-canonical loop_mode 'ablation_block_emit'"]


def test_empty_replays_is_no_fresh_replay_error():
    errs = verify_canonical_trace(_clean_trace(replays=(), stop_reason=""))
    assert errs == ["no fresh replay ran (fresh replay is the sole executor)"]


def test_done_with_failed_last_replay_is_an_error():
    trace = _clean_trace(replays=(_green_replay(setup_rc=1, failing_command="pip install -r requirements.txt", test_rc=None),))
    errs = verify_canonical_trace(trace)
    assert errs == ["done reached but latest fresh replay failed: pip install -r requirements.txt"]


def test_done_with_last_replay_not_ran_is_an_error():
    # replays is non-empty (so the "no fresh replay ran" branch is not hit)
    # but the LAST one never actually ran -> "done reached without a fresh replay".
    trace = _clean_trace(replays=(_green_replay(), _green_replay(ran=False, setup_rc=None, test_rc=None)))
    errs = verify_canonical_trace(trace)
    assert errs == ["done reached without a fresh replay"]


def test_done_with_provisional_installability_gate_is_an_error():
    trace = _clean_trace(gates={"installability": {"provisional": True}})
    errs = verify_canonical_trace(trace)
    assert errs == ["installability gate still provisional on a done run"]


def test_done_with_missing_installability_gate_defaults_provisional():
    trace = _clean_trace(gates={})
    errs = verify_canonical_trace(trace)
    assert errs == ["installability gate still provisional on a done run"]


def test_not_done_stop_reason_skips_replay_and_gate_checks():
    # stop_reason outside the done set -> no "failed replay" / "provisional
    # gate" checks fire, even with a failing replay and a provisional gate,
    # as long as at least one replay ran (still required unconditionally).
    trace = _clean_trace(
        stop_reason="planner_giveup",
        replays=(_green_replay(setup_rc=1, failing_command="apt-get install -y libfoo", test_rc=None),),
        gates={"installability": {"provisional": True}},
    )
    assert verify_canonical_trace(trace) == []


def test_discover_used_llm_mutation_is_an_error():
    d = DiscoverRecord(
        cycle=2,
        command="python -m pytest -q",
        used_llm_mutation=True,
        new_node_ids=(),
        diagnosis_modes=(),
    )
    trace = _clean_trace(discover=(d,))
    errs = verify_canonical_trace(trace)
    assert errs == ["discover cycle 2 used LLM mutation, not the deterministic gate"]


def test_multiple_violations_accumulate():
    errs = verify_canonical_trace(_clean_trace(used_emit_drain=True, used_block_emit=True))
    assert set(errs) == {
        "legacy emit_drain executed in canonical run",
        "block_emit ablation executed inside the method",
    }
    assert len(errs) == 2


# ---------------------------------------------------------------------------
# verify_artifact_consistency
# ---------------------------------------------------------------------------

def test_artifact_consistency_clean_script_has_no_errors():
    script = "#!/bin/bash\napt-get install -y libplacebo-dev\n# block.1\npip install requests\n"
    assert verify_artifact_consistency(script, ("block.1",)) == []


def test_artifact_consistency_unscheduled_blocks_is_an_error():
    script = "#!/bin/bash\n(UNSCHEDULED BLOCKS)\npip install requests\n"
    errs = verify_artifact_consistency(script, ())
    assert errs == ["rendered setup.sh contains an UNSCHEDULED BLOCKS section"]


def test_artifact_consistency_missing_manual_block_is_an_error():
    script = "#!/bin/bash\npip install requests\n"
    errs = verify_artifact_consistency(script, ("block.1",))
    assert errs == ["governed manual block block.1 missing from final setup.sh"]


def test_artifact_consistency_both_violations_accumulate():
    script = "#!/bin/bash\n(UNSCHEDULED BLOCKS)\n"
    errs = verify_artifact_consistency(script, ("block.1", "block.2"))
    assert errs == [
        "rendered setup.sh contains an UNSCHEDULED BLOCKS section",
        "governed manual block block.1 missing from final setup.sh",
        "governed manual block block.2 missing from final setup.sh",
    ]


# ---------------------------------------------------------------------------
# verify_local_import_guard
# ---------------------------------------------------------------------------

def test_local_import_guard_clean_trace_has_no_errors():
    d = DiscoverRecord(
        cycle=1,
        command="python -m pytest -q",
        used_llm_mutation=False,
        new_node_ids=("pkg:requests",),
        diagnosis_modes=("missing_external_pkg",),
    )
    trace = _clean_trace(discover=(d,))
    assert verify_local_import_guard(trace) == []


def test_local_import_guard_repo_internal_ref_with_pkg_node_is_an_error():
    d = DiscoverRecord(
        cycle=3,
        command="python -m pytest -q",
        used_llm_mutation=False,
        new_node_ids=("pkg:docs-src",),
        diagnosis_modes=("repo_internal_reference",),
    )
    trace = _clean_trace(discover=(d,))
    errs = verify_local_import_guard(trace)
    assert errs == ["discover cycle 3 added a package node for a repo-local import"]


def test_local_import_guard_repo_internal_ref_without_pkg_node_is_clean():
    d = DiscoverRecord(
        cycle=3,
        command="python -m pytest -q",
        used_llm_mutation=False,
        new_node_ids=("apt:some-lib",),
        diagnosis_modes=("repo_internal_reference",),
    )
    trace = _clean_trace(discover=(d,))
    assert verify_local_import_guard(trace) == []
