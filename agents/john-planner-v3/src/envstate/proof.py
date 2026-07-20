"""Task 8d: pure proof-harness helpers — no Docker, no network.

``scripts/run_v3_e2e.py`` builds a ``RunTracer``, threads it through
``run_v3``, and on exit calls ``finalize_trace`` to turn the recorder + the
``gate_observer``'s last observation + the rendered ``setup.sh`` into a
``RunTrace`` snapshot and a verify report. ``scripts/run_v3_proof.py`` runs
that e2e driver once per repo and reduces the resulting
``(RunTrace, script_text)`` pairs into the report table via
``repo_row``/``aggregate``, and the single composite ``canonical_success``
predicate — the strongest per-repo proof claim (done + green replay + no
legacy path + artifact-complete + every host certifier satisfied).

Everything in this module is a pure function over already-collected data
(``RunTrace`` objects and plain strings) — nothing here talks to Docker, a
sandbox, or an LLM, so it is fully unit-testable by hand-constructing traces.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from src.envstate.run_trace import (
    DiscoverRecord,
    FreshReplayRecord,
    PatchGateRecord,
    RunTrace,
)
from src.envstate.trace_verify import (
    verify_artifact_consistency,
    verify_canonical_trace,
    verify_local_import_guard,
)

if TYPE_CHECKING:
    from src.envstate.run_trace import RunTracer

# stop_reason values that _to_stop_reason (orchestrator.py) maps success
# terminations to — kept as a local tuple (not imported) so this module has
# no orchestrator dependency; verify_canonical_trace uses the same literal set.
_DONE_REASONS = ("done", "planner_done", "done_flag")


def finalize_trace(
    tracer: "RunTracer",
    stop: str,
    gates_seen: Sequence[Sequence[Any]],
    script_text: str,
) -> tuple[RunTrace, dict[str, list[str]]]:
    """Snapshot ``tracer`` and run all three verifiers against it in one place.

    ``gates_seen`` is the driver's ``gate_observer=gates_seen.append``
    accumulator — a list of ``(installability_gate, testability_gate)``
    tuples, one per ``run_v3`` exit (``enable_gate_observability=True`` fires
    the observer exactly once, on the way out, but this accepts a sequence
    defensively). Only the LAST observation is binding. Each gate object is
    duck-typed (``.name``/``.passed``/``.provisional``/``.evidence``) so this
    module never needs to import ``GateResult``.
    """
    gates = {
        g.name: {"passed": g.passed, "provisional": g.provisional, "evidence": g.evidence}
        for g in (gates_seen[-1] if gates_seen else ())
    }
    trace = tracer.snapshot(stop_reason=stop, gates=gates)
    report = {
        "canonical": verify_canonical_trace(trace),
        "artifact": verify_artifact_consistency(script_text, trace.manual_block_ids),
        "local_import": verify_local_import_guard(trace),
    }
    return trace, report


def canonical_success(trace: RunTrace, script_text: str) -> bool:
    """The strongest per-repo proof claim: done, on a green fresh replay, via
    the canonical loop only, with an artifact-complete script, and no host
    certifier left unsatisfied.

    Intentionally stricter than the loop's own success signal: ``run_v3``'s
    ``_finalize_if_replayed`` (orchestrator.py) gates a `done`/`planner_done`/
    `done_flag` ``stop_reason`` on install rc0 alone, so a run can reach a
    success ``stop_reason`` while still carrying an unsatisfied reciped node
    (or a failing test) — ``canonical_success``, not ``stop_reason``, is the
    paper/report success metric.
    """
    last = trace.last_replay
    return bool(
        trace.stop_reason in _DONE_REASONS
        and last is not None
        and last.setup_rc == 0
        and last.test_rc == 0
        and not verify_canonical_trace(trace)
        and not verify_artifact_consistency(script_text, trace.manual_block_ids)
        and not last.unsatisfied_node_ids
    )


def repo_row(trace: RunTrace) -> dict[str, Any]:
    """One row of the per-repo proof table.

    Trace-only (no ``script_text``): every column here is derivable from the
    recorder's own facts. The one column that needs the rendered artifact
    (``canonical_success``, which folds in ``verify_artifact_consistency``) is
    intentionally NOT part of this row — callers that have the script text
    add it alongside (see ``scripts/run_v3_proof.py``).
    """
    last = trace.last_replay
    legacy_used = bool(
        trace.used_emit_drain
        or trace.used_repair_failed_nodes
        or trace.used_build_agent_run
        or trace.used_block_emit
    )
    added_nodes: set[str] = set()
    for pg in trace.patchgate:
        added_nodes.update(pg.accepted_node_ids)
    for d in trace.discover:
        added_nodes.update(d.new_node_ids)

    fresh_replay = bool(last is not None and last.setup_rc == 0)
    tests_pass = bool(last is not None and last.test_rc == 0)
    passed = trace.stop_reason in _DONE_REASONS and fresh_replay and tests_pass

    return {
        "repo": trace.repo,
        "result": "PASS" if passed else "FAIL",
        "legacy_used": legacy_used,
        "graph_nodes_added": len(added_nodes),
        "patchgate_accepts": sum(1 for pg in trace.patchgate if pg.accepted),
        "manual_blocks": len(trace.manual_block_ids),
        "fresh_replay": fresh_replay,
        "tests_pass": tests_pass,
        "residual_reason": "" if passed else trace.stop_reason,
    }


def aggregate(traces: Sequence[tuple[RunTrace, str]]) -> dict[str, Any]:
    """Reduce ``(RunTrace, script_text)`` pairs (one per repo run) to the
    report's aggregate counters.

    ``script_text`` — each repo's rendered ``setup.sh`` — is needed only for
    ``manual_block_artifact_mismatches`` (``verify_artifact_consistency``);
    every other counter is derivable from the trace alone.
    """
    total = len(traces)
    canonical_loop_runs = sum(
        1 for t, _ in traces if t.loop_mode == "v3_graph_typed_repair"
    )
    legacy_path_violations = sum(1 for t, _ in traces if verify_canonical_trace(t))
    replayed_green = sum(
        1 for t, _ in traces if t.last_replay is not None and t.last_replay.setup_rc == 0
    )
    manual_block_artifact_mismatches = sum(
        1 for t, script in traces if verify_artifact_consistency(script, t.manual_block_ids)
    )
    local_import_false_package_attempts = sum(
        len(verify_local_import_guard(t)) for t, _ in traces
    )
    return {
        "canonical_loop_runs": canonical_loop_runs,
        "legacy_path_violations": legacy_path_violations,
        "fresh_replay_pass_rate": (replayed_green / total) if total else 0.0,
        "manual_block_artifact_mismatches": manual_block_artifact_mismatches,
        "local_import_false_package_attempts": local_import_false_package_attempts,
    }


def _tuplify(d: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Coerce the named keys back to tuples (JSON round-trips tuples as lists)."""
    out = dict(d)
    for k in keys:
        if k in out and isinstance(out[k], list):
            out[k] = tuple(out[k])
    return out


def trace_from_dict(d: dict[str, Any]) -> RunTrace:
    """Inverse of ``RunTrace.to_dict()`` — reconstruct a frozen ``RunTrace``
    from its JSON-round-tripped dict form (as written to ``--trace-out``).

    ``dataclasses.asdict`` (used by ``to_dict``) turns every tuple field into
    a plain Python tuple already, but a JSON round-trip (``json.dumps`` has no
    tuple type) turns them into lists — this restores tuples on every field
    typed ``tuple[...]`` on the nested records, so a reconstructed ``RunTrace``
    is structurally identical (``==``) to the one that produced the dict.
    """
    patchgate = tuple(
        PatchGateRecord(**_tuplify(p, ("accepted_node_ids", "accepted_block_ids", "errors")))
        for p in d.get("patchgate", ())
    )
    discover = tuple(
        DiscoverRecord(**_tuplify(p, ("new_node_ids", "diagnosis_modes")))
        for p in d.get("discover", ())
    )
    replays = tuple(
        FreshReplayRecord(**_tuplify(p, ("certified_node_ids", "unsatisfied_node_ids")))
        for p in d.get("replays", ())
    )
    return RunTrace(
        repo=d.get("repo", ""),
        loop_mode=d.get("loop_mode", "v3_graph_typed_repair"),
        used_emit_drain=d.get("used_emit_drain", False),
        used_repair_failed_nodes=d.get("used_repair_failed_nodes", False),
        used_build_agent_run=d.get("used_build_agent_run", False),
        used_block_emit=d.get("used_block_emit", False),
        patchgate=patchgate,
        discover=discover,
        replays=replays,
        manual_block_ids=tuple(d.get("manual_block_ids", ())),
        stop_reason=d.get("stop_reason", ""),
        gates=d.get("gates", {}),
    )
