"""Task 8b: pure assertion functions over a ``RunTrace`` / rendered artifact.

Proves the three e2e-proof claims (module docstring of ``run_trace.py``
carries the recorder side of this):

  1. ``verify_canonical_trace``        — the canonical loop was used (no
     legacy/ablation code path executed, and a real fresh replay backs any
     "done" result).
  2. ``verify_artifact_consistency``   — the graph/script/fresh-replay
     contract holds (the rendered ``setup.sh`` has no unscheduled blocks and
     contains every governed manual block).
  3. ``verify_local_import_guard``     — the legacy "repo-local import
     mistaken for a missing external package" path did no work.

Read-only: these functions only inspect a ``RunTrace`` / a rendered script
string and return a list of error strings (empty = clean). Nothing here
mutates a graph, a trace, or any other state.
"""
from __future__ import annotations

from src.envstate.run_trace import RunTrace


def verify_canonical_trace(t: RunTrace) -> list[str]:
    errs: list[str] = []
    if t.used_emit_drain:
        errs.append("legacy emit_drain executed in canonical run")
    if t.used_repair_failed_nodes:
        errs.append("legacy repair_failed_nodes executed")
    if t.used_build_agent_run:
        errs.append("free-text build_agent.run executed")
    if t.used_block_emit:
        errs.append("block_emit ablation executed inside the method")
    if t.loop_mode != "v3_graph_typed_repair":
        errs.append(f"non-canonical loop_mode {t.loop_mode!r}")
    if not t.replays:
        errs.append("no fresh replay ran (fresh replay is the sole executor)")
    if t.stop_reason in ("done", "planner_done", "done_flag"):
        last = t.last_replay
        if last is None or not last.ran:
            errs.append("done reached without a fresh replay")
        elif last.setup_rc != 0:
            errs.append(f"done reached but latest fresh replay failed: {last.failing_command}")
        if t.gates.get("installability", {}).get("provisional", True):
            errs.append("installability gate still provisional on a done run")
    for d in t.discover:
        if d.used_llm_mutation:
            errs.append(f"discover cycle {d.cycle} used LLM mutation, not the deterministic gate")
    return errs


def verify_artifact_consistency(script_text: str, manual_block_ids: tuple[str, ...]) -> list[str]:
    errs: list[str] = []
    if "(UNSCHEDULED BLOCKS)" in script_text:
        errs.append("rendered setup.sh contains an UNSCHEDULED BLOCKS section")
    for bid in manual_block_ids:
        if bid not in script_text:
            errs.append(f"governed manual block {bid} missing from final setup.sh")
    return errs


def verify_local_import_guard(t: RunTrace) -> list[str]:
    # No discover cycle produced a package node for a REPO_INTERNAL_REF diagnosis.
    errs: list[str] = []
    for d in t.discover:
        if "repo_internal_reference" in d.diagnosis_modes:
            if any(nid.startswith("pkg:") for nid in d.new_node_ids):
                errs.append(f"discover cycle {d.cycle} added a package node for a repo-local import")
    return errs
