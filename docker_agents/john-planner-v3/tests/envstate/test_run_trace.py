"""Task 8a: RunTracer (append-only recorder) -> RunTrace (frozen snapshot).

Pure instrumentation, no orchestrator wiring here. Covers:
  1. RunTracer records one of each kind (patchgate/discover/replay/marks/
     manual_blocks) and snapshot() returns a RunTrace whose fields match what
     was recorded.
  2. RunTrace is frozen (dataclasses.FrozenInstanceError on attribute set).
  3. last_replay returns the LAST of multiple recorded replays.
  4. to_dict() round-trips to a plain, JSON-serializable dict (no dataclass
     instances left behind in nested tuples).
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest

from src.envstate.run_trace import (
    DiscoverRecord,
    FreshReplayRecord,
    PatchGateRecord,
    RunTrace,
    RunTracer,
)


def _patchgate_record(cycle: int = 1) -> PatchGateRecord:
    return PatchGateRecord(
        cycle=cycle,
        failed_block_id="system.libplacebo",
        evidence_ref="ev.1.0",
        accepted=True,
        accepted_node_ids=("apt:libplacebo-dev",),
        accepted_block_ids=("block.1",),
        errors=(),
    )


def _discover_record(cycle: int = 1) -> DiscoverRecord:
    return DiscoverRecord(
        cycle=cycle,
        command="python -m pytest -q",
        used_llm_mutation=False,
        new_node_ids=("pkg:requests",),
        diagnosis_modes=("missing_external_pkg",),
    )


def _replay_record(*, setup_rc: int = 0, test_rc: int | None = 0) -> FreshReplayRecord:
    return FreshReplayRecord(
        ran=True,
        setup_rc=setup_rc,
        failing_command=None if setup_rc == 0 else "pip install -r requirements.txt",
        certified_node_ids=("pkg:requests",),
        unsatisfied_node_ids=(),
        test_rc=test_rc,
        test_summary="1 passed in 0.01s",
    )


# ---------------------------------------------------------------------------
# 1. RunTracer records + snapshot() reflects exactly what was recorded
# ---------------------------------------------------------------------------

def test_snapshot_reflects_all_recorded_kinds():
    tracer = RunTracer(repo="acme/widget")
    pg = _patchgate_record()
    disc = _discover_record()
    replay = _replay_record()

    tracer.record_patchgate(pg)
    tracer.record_discover(disc)
    tracer.record_replay(replay)
    tracer.set_manual_blocks(("block.1", "block.2"))

    trace = tracer.snapshot(stop_reason="planner_done", gates={"installability": {"provisional": False}})

    assert trace.repo == "acme/widget"
    assert trace.patchgate == (pg,)
    assert trace.discover == (disc,)
    assert trace.replays == (replay,)
    assert trace.manual_block_ids == ("block.1", "block.2")
    assert trace.stop_reason == "planner_done"
    assert trace.gates == {"installability": {"provisional": False}}
    # Marks default False when never called.
    assert trace.used_emit_drain is False
    assert trace.used_repair_failed_nodes is False
    assert trace.used_build_agent_run is False
    assert trace.used_block_emit is False
    assert trace.loop_mode == "v3_graph_typed_repair"


def test_snapshot_defensive_copies_gates_dict():
    """Part-1 review Minor: snapshot() must copy the caller's `gates` dict, not
    alias it — otherwise a post-snapshot mutation of the SOURCE dict would
    retroactively change an already-returned, supposedly-frozen RunTrace."""
    tracer = RunTracer()
    source = {"installability": {"provisional": False}}

    trace = tracer.snapshot(stop_reason="planner_done", gates=source)
    assert trace.gates == {"installability": {"provisional": False}}

    source["installability"] = {"provisional": True}
    source["testability"] = {"passed": False}

    assert trace.gates == {"installability": {"provisional": False}}, (
        "trace.gates changed after the snapshot call when the SOURCE dict "
        "passed to snapshot() was mutated — gates=gates aliased the caller's "
        "dict instead of copying it"
    )
    assert "testability" not in trace.gates


def test_snapshot_reflects_marks():
    tracer = RunTracer()
    tracer.mark_emit_drain()
    tracer.mark_repair_failed_nodes()
    tracer.mark_build_agent_run()
    tracer.mark_block_emit()

    trace = tracer.snapshot(stop_reason="planner_giveup", gates={})

    assert trace.used_emit_drain is True
    assert trace.used_repair_failed_nodes is True
    assert trace.used_build_agent_run is True
    assert trace.used_block_emit is True


def test_empty_tracer_snapshot_has_empty_defaults():
    tracer = RunTracer()
    trace = tracer.snapshot(stop_reason="", gates={})

    assert trace.patchgate == ()
    assert trace.discover == ()
    assert trace.replays == ()
    assert trace.manual_block_ids == ()
    assert trace.last_replay is None


# ---------------------------------------------------------------------------
# 2. RunTrace (and its nested records) are frozen
# ---------------------------------------------------------------------------

def test_run_trace_is_frozen():
    trace = RunTracer().snapshot(stop_reason="x", gates={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        trace.stop_reason = "y"  # type: ignore[misc]


def test_nested_records_are_frozen():
    pg = _patchgate_record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        pg.accepted = False  # type: ignore[misc]

    disc = _discover_record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        disc.used_llm_mutation = True  # type: ignore[misc]

    replay = _replay_record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        replay.setup_rc = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 3. last_replay returns the LAST of multiple recorded replays
# ---------------------------------------------------------------------------

def test_last_replay_returns_most_recent_of_several():
    tracer = RunTracer()
    first = _replay_record(setup_rc=1, test_rc=None)
    second = _replay_record(setup_rc=1, test_rc=None)
    third = _replay_record(setup_rc=0, test_rc=0)

    tracer.record_replay(first)
    tracer.record_replay(second)
    tracer.record_replay(third)

    trace = tracer.snapshot(stop_reason="planner_done", gates={})

    assert trace.replays == (first, second, third)
    assert trace.last_replay is third
    assert trace.last_replay.setup_rc == 0


# ---------------------------------------------------------------------------
# 3b. set_last_replay_tests() back-fills ONLY the LAST replay's test fields
# ---------------------------------------------------------------------------

def test_set_last_replay_tests_backfills_last():
    """Task 8 gap-fix: the test gate (`_run_tests_verified`) is a SEPARATE
    call from the fresh-replay executor, so `record_replay` always records
    test_rc=None/test_summary="". `set_last_replay_tests` back-fills the
    LAST recorded replay in place (via dataclasses.replace, keeping the
    record frozen) once the test gate result is known — earlier replays in
    the same run must be untouched.
    """
    tracer = RunTracer()
    first = _replay_record(setup_rc=1, test_rc=None)
    second = _replay_record(setup_rc=0, test_rc=None)

    tracer.record_replay(first)
    tracer.record_replay(second)
    tracer.set_last_replay_tests(0, "5 passed in 0.10s")

    trace = tracer.snapshot(stop_reason="planner_done", gates={})

    assert len(trace.replays) == 2
    # Earlier replay is byte-identical to what was recorded — untouched.
    assert trace.replays[0] == first
    assert trace.replays[0].test_rc is None
    assert trace.replays[0].test_summary == first.test_summary
    # Only the LAST replay got the back-filled test result.
    assert trace.replays[1].test_rc == 0
    assert trace.replays[1].test_summary == "5 passed in 0.10s"
    # Every other field on the last replay is preserved (only test_rc/
    # test_summary changed via dataclasses.replace).
    assert trace.replays[1].setup_rc == second.setup_rc
    assert trace.replays[1].ran == second.ran
    assert trace.replays[1].certified_node_ids == second.certified_node_ids
    assert trace.replays[1].unsatisfied_node_ids == second.unsatisfied_node_ids

    assert trace.last_replay.test_rc == 0


def test_set_last_replay_tests_noop_when_empty():
    """No replays recorded yet -> set_last_replay_tests must not raise and
    must leave `replays` empty (defensive no-op, not an IndexError)."""
    tracer = RunTracer()
    tracer.set_last_replay_tests(0, "5 passed")

    trace = tracer.snapshot(stop_reason="planner_done", gates={})

    assert trace.replays == ()
    assert trace.last_replay is None


# ---------------------------------------------------------------------------
# 4. to_dict() round-trips to a plain, JSON-serializable dict
# ---------------------------------------------------------------------------

def test_to_dict_round_trips_and_is_json_serializable():
    tracer = RunTracer(repo="acme/widget")
    tracer.record_patchgate(_patchgate_record())
    tracer.record_discover(_discover_record())
    tracer.record_replay(_replay_record(setup_rc=1, test_rc=None))
    tracer.record_replay(_replay_record(setup_rc=0, test_rc=0))
    tracer.set_manual_blocks(("block.1",))
    tracer.mark_emit_drain()

    trace = tracer.snapshot(
        stop_reason="planner_done",
        gates={"installability": {"passed": True, "provisional": False}},
    )

    d = trace.to_dict()

    assert isinstance(d, dict)
    assert d["repo"] == "acme/widget"
    assert d["used_emit_drain"] is True
    assert d["stop_reason"] == "planner_done"
    assert d["manual_block_ids"] == ("block.1",) or d["manual_block_ids"] == ["block.1"]

    # No dataclass instances survive serialization anywhere in the tree.
    assert not dataclasses.is_dataclass(d.get("patchgate"))
    for entry in d["patchgate"]:
        assert isinstance(entry, dict)
        assert not dataclasses.is_dataclass(entry)
    for entry in d["discover"]:
        assert isinstance(entry, dict)
    assert len(d["replays"]) == 2
    for entry in d["replays"]:
        assert isinstance(entry, dict)
        assert not dataclasses.is_dataclass(entry)

    # "last_replay" (a derived @property, not a dataclass field) must not
    # silently leak an unserializable object into to_dict() either way; if
    # present it must itself already be a plain dict.
    if "last_replay" in d:
        assert isinstance(d["last_replay"], dict)

    # The whole thing must be JSON-serializable in one shot.
    serialized = json.dumps(d)
    reloaded = json.loads(serialized)
    assert reloaded["repo"] == "acme/widget"
    assert len(reloaded["replays"]) == 2
    assert reloaded["replays"][1]["setup_rc"] == 0
