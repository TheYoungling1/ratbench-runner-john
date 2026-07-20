"""Deterministic Maintainer (Change B): blocker extraction + done-gate, no LLM.

Replaces the LLM Maintainer's graph-patch step with verbatim-signature blockers
carrying the correct layer, so the existing host machinery (_auto_resolve_blockers
→ derive_open_problems) fires after a fix.
Attempts/outcomes are NOT handled here — the orchestrator already does that.
"""
from __future__ import annotations

import logging

from .contracts import ids
from .contracts.apply import apply_patch
from .contracts.extract import CONTRACT_LAYERS, extract_blocker_match
from .contracts.graph import ContractGraph
from .contracts.nodes import Edge, Node
from .contracts.patch import GraphPatch
from .contracts.validation import validate_patch
from .done_gate import _progress_synced_with_done, _verified_test_run_passed
from .world_model import TaskReport, WorldModelMap, merge_map

logger = logging.getLogger(__name__)


def build_blocker_patch(graph: ContractGraph, report: TaskReport) -> GraphPatch:
    """A scope='host' patch of Contract + Blocker + violates for each failure
    signature in the report's command output. Idempotent vs the graph."""
    contracts: list[Node] = []
    blockers: list[Node] = []
    edges: list[Edge] = []
    seen: set[str] = set()

    for rec in report.commands:
        if getattr(rec, "rc", 0) == 0:
            continue
        for raw in (rec.output or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            match = extract_blocker_match(line)
            if match is None:
                continue
            subject, bkind, ckind, sig = match
            layer = CONTRACT_LAYERS[ckind]
            cid = ids.contract_id(ckind, subject)
            bid = ids.blocker_id(sig)
            if cid not in seen and not graph.has_node(cid):
                contracts.append(Node(cid, "Contract", {
                    "level": "atomic", "kind": ckind, "subject": subject, "layer": layer,
                    "check": "", "source_refs": [f"signature:{sig[:60]}"],
                    "evidence_refs": [], "description": f"{ckind} obligation: {subject}.",
                    "metadata": {},
                }))
            if bid not in seen and not graph.has_node(bid):
                blockers.append(Node(bid, "Blocker", {
                    "signature": sig, "kind": bkind, "layer": layer, "subject": subject,
                    "summary": f"{bkind}: {subject}", "root_or_downstream": "root",
                    "active": True, "evidence_refs": [], "metadata": {},
                }))
                edges.append(Edge(bid, "violates", cid))
            seen.add(cid)
            seen.add(bid)

    notes = (report.learning,) if (report.learning or "").strip() else ()
    return GraphPatch(
        add_contracts=tuple(contracts),
        add_blockers=tuple(blockers),
        add_edges=tuple(edges),
        diagnostic_notes=notes,
    )


def maintain(current_map: WorldModelMap, report: TaskReport) -> WorldModelMap:
    """Deterministic drop-in for Maintainer.update: extract blockers + done-gate.

    Attempts/outcomes are handled by the orchestrator, not here.
    """
    done = current_map.done_flag or _verified_test_run_passed(report)
    graph = current_map.contract_graph
    patch = build_blocker_patch(graph, report)
    if not patch.is_empty():
        # host scope: blockers carry empty evidence_refs, which scope="maintainer" would reject
        errors = validate_patch(graph, patch, scope="host")
        if errors:
            logger.warning("deterministic maintain: dropping invalid host patch: %s", errors)
        else:
            graph = apply_patch(graph, patch)
    return merge_map(
        current_map,
        done_flag=done,
        progress=_progress_synced_with_done(current_map, done),
        contract_graph=graph,
    )


def _v3_done_gate(current_map: WorldModelMap, report: TaskReport) -> WorldModelMap:
    """v3-only done-gate: set done_flag + sync progress. No contract_graph write.

    The dep-graph is the sole world-model in v3; the blocker patch is vestigial
    there. Anti-hollow: still requires _verified_test_run_passed (host evidence),
    never finalizes on LLM/action say-so.
    """
    done = current_map.done_flag or _verified_test_run_passed(report)
    progress_update = _progress_synced_with_done(current_map, done)
    if done == current_map.done_flag and progress_update is None:
        return current_map  # no-op fast path
    return merge_map(current_map, done_flag=done, progress=progress_update)


class DeterministicMaintainer:
    """Duck-typed stand-in for Maintainer (exposes .update).

    v3_only=True activates the slim done-gate path (_v3_done_gate): only
    done_flag + progress are written; contract_graph is left untouched because
    dep_graph is the sole world-model in the v3/graph-scheduler arm.
    The default (v3_only=False) delegates to maintain() unchanged — v1 behavior
    is byte-for-byte preserved.
    """

    def __init__(self, *, v3_only: bool = False) -> None:
        self._v3_only = v3_only

    def update(self, current_map: WorldModelMap, report: TaskReport) -> WorldModelMap:
        if self._v3_only:
            return _v3_done_gate(current_map, report)
        return maintain(current_map, report)
