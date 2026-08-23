"""Validate a GraphPatch against a graph + ownership scope (spec §8)."""
from __future__ import annotations

from .graph import ContractGraph
from .nodes import Edge
from .patch import GraphPatch
from .schema import (
    EDGE_RULES,
    MAINTAINER_FORBIDDEN_FIELDS,
)

MAX_PROMOTIONS_PER_CYCLE: int = 8


def _build_type_index(graph: ContractGraph, patch: GraphPatch) -> dict[str, str]:
    """Type of every node visible after the patch (existing + added)."""
    index: dict[str, str] = {n.id: n.type for n in graph.nodes}
    for n in list(patch.add_contracts) + list(patch.add_blockers) + list(patch.add_attempts):
        index[n.id] = n.type
    return index


def validate_patch(
    graph: ContractGraph,
    patch: GraphPatch,
    *,
    scope: str,
    known_command_ids: frozenset[str] = frozenset(),
) -> list[str]:
    """Return a list of human-readable errors; empty list == valid.

    scope="maintainer" enforces field-level ownership, grounded-blocker,
    backbone-attachment, and no-inventory-mirror rules (spec §8).
    scope="host" skips those checks.
    """
    errors: list[str] = []
    all_edges: list[Edge] = list(graph.edges) + list(patch.add_edges)
    type_index = _build_type_index(graph, patch)

    if scope == "maintainer":
        # --- 1. Promotion limit (no inventory mirror) ---
        if len(patch.add_contracts) > MAX_PROMOTIONS_PER_CYCLE:
            errors.append(
                f"too many contract promotions in one patch: "
                f"{len(patch.add_contracts)} > MAX_PROMOTIONS_PER_CYCLE={MAX_PROMOTIONS_PER_CYCLE}; "
                f"inventory mirror guard prevents bulk promotion"
            )

        # --- 2. add_contracts: type check + forbidden-field check ---
        for n in patch.add_contracts:
            if n.type != "Contract":
                errors.append(
                    f"maintainer may not add {n.type!r} nodes via add_contracts ({n.id}); "
                    f"Attempt and other non-Contract types are host-only"
                )
            else:
                # Maintainer may never write status/outcome/active on a Contract
                for field in MAINTAINER_FORBIDDEN_FIELDS:
                    if n.data.get(field) is not None:
                        errors.append(
                            f"maintainer may not set forbidden field {field!r} on node {n.id}"
                        )

        # --- 3. Grounded blockers: evidence_refs must be non-empty and all known ---
        for n in patch.add_blockers:
            refs: list[str] = list(n.data.get("evidence_refs") or [])
            bad = [r for r in refs if r not in known_command_ids]
            if not refs or bad:
                errors.append(
                    f"blocker {n.id!r} has ungrounded evidence_refs {bad!r}; "
                    f"every ref must be a known command id"
                )

        # --- 4. Backbone attachment for atomic contracts ---
        in_edge_targets = {
            e.target for e in all_edges if e.type == "depends_on" and not e.invalidated
        }
        for n in patch.add_contracts:
            if n.type == "Contract" and n.data.get("level") == "atomic":
                if n.id not in in_edge_targets:
                    errors.append(
                        f"atomic contract {n.id!r} is an orphan: "
                        f"no depends_on backbone attachment under any goal"
                    )

        # --- 5. Backbone attachment for blockers (must have a violates out-edge) ---
        violates_sources = {
            e.source for e in all_edges if e.type == "violates" and not e.invalidated
        }
        for n in patch.add_blockers:
            if n.id not in violates_sources:
                errors.append(
                    f"blocker {n.id!r} has no violates edge; "
                    f"every blocker must attach to the contract backbone"
                )

    # --- Edge validity (all scopes) ---
    for e in patch.add_edges:
        if e.type not in EDGE_RULES:
            errors.append(f"unknown edge type {e.type!r}")
            continue
        src_type = type_index.get(e.source)
        tgt_type = type_index.get(e.target)
        if src_type is None or tgt_type is None:
            errors.append(
                f"edge endpoint missing: {e.source} -{e.type}-> {e.target}"
            )
            continue
        allowed_src, allowed_tgt = EDGE_RULES[e.type]
        if src_type not in allowed_src or tgt_type not in allowed_tgt:
            errors.append(
                f"edge type {e.type!r} not allowed between "
                f"{src_type} and {tgt_type}"
            )

    return errors
