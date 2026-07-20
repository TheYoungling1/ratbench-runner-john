"""Apply a (pre-validated) GraphPatch, returning a new immutable ContractGraph."""
from __future__ import annotations

import dataclasses
from typing import Any

from .graph import ContractGraph
from .nodes import Edge, Node
from .patch import GraphPatch

_NOTE_CAP = 10


def _merge_data(node: Node, updates: dict[str, Any]) -> Node:
    """Return a new Node with the given keys merged into its data dict."""
    new_data = {**node.data, **updates}
    return dataclasses.replace(node, data=new_data)


def apply_patch(graph: ContractGraph, patch: GraphPatch) -> ContractGraph:
    """Apply *patch* to *graph* and return a new ContractGraph (immutable)."""
    nodes_by_id: dict[str, Node] = {n.id: n for n in graph.nodes}

    # --- node additions (setdefault: dup ids are no-ops; validation layer catches true dups) ---
    for n in patch.add_contracts:
        nodes_by_id.setdefault(n.id, n)
    for n in patch.add_blockers:
        nodes_by_id.setdefault(n.id, n)
    for n in patch.add_attempts:
        nodes_by_id.setdefault(n.id, n)

    # --- full-node replacements (host-internal) ---
    for n in patch.update_blockers:
        nodes_by_id[n.id] = n
    for n in patch.update_contracts:
        nodes_by_id[n.id] = n
    for n in patch.update_attempts:
        nodes_by_id[n.id] = n

    # --- semantic field-level merges (Maintainer-facing) ---
    _BLOCKER_CLASSIFICATION_FIELDS = frozenset({"root_or_downstream", "kind", "summary"})
    for upd in patch.update_blocker_classification:
        bid = upd.get("blocker_id")
        if bid and bid in nodes_by_id:
            field_updates = {k: v for k, v in upd.items()
                             if k in _BLOCKER_CLASSIFICATION_FIELDS}
            if field_updates:
                nodes_by_id[bid] = _merge_data(nodes_by_id[bid], field_updates)

    for upd in patch.update_contract_description:
        cid = upd.get("contract_id")
        if cid and cid in nodes_by_id and "description" in upd:
            nodes_by_id[cid] = _merge_data(nodes_by_id[cid], {"description": upd["description"]})

    # --- invalidations ---
    for nid in patch.invalidate_nodes:
        if nid in nodes_by_id:
            nodes_by_id[nid] = dataclasses.replace(nodes_by_id[nid], invalidated=True)

    # --- edges ---
    edges: tuple[Edge, ...] = graph.edges + tuple(patch.add_edges)

    # --- diagnostic notes: append new, keep only the last _NOTE_CAP ---
    combined_notes = graph.diagnostic_notes + patch.diagnostic_notes
    capped_notes: tuple[str, ...] = combined_notes[-_NOTE_CAP:] if len(combined_notes) > _NOTE_CAP else combined_notes

    return ContractGraph(
        nodes=tuple(nodes_by_id.values()),
        edges=edges,
        diagnostic_notes=capped_notes,
    )
