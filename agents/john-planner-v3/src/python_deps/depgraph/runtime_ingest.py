"""Runtime-feedback graph ingestion (design 2026-06-26 §5, §9).

Pure module — no src.envstate imports. Unit-testable with plain data.

``ingest_runtime_failures(graph, observations, classifiers) -> (DepGraph, list[Discovery])``

Maps each non-None Discovery to an idempotent graph mutation:
  * id absent  -> append new node
  * id present -> annotate: merge runtime evidence + set runtime_confidence
Hangs a Test --requires--> node edge with origin="runtime" (deduped by DepGraph).
Returns a NEW DepGraph every time (immutability) and the list of Discoveries found.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace

from python_deps.depgraph.ids import (
    TEST_NODE_ID, config_id, package_id, service_id, syslib_id, tool_id,
)
from python_deps.depgraph.runtime_classify import Discovery, classify_observation
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Edge, EdgeType, Layer, Node, NodeType, State,
)
from python_deps.import_mapping import normalize_package_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Discovery -> Node constructor
# ---------------------------------------------------------------------------

def _node_for_discovery(d: Discovery) -> Node:
    """Build a fresh Node from a Discovery.  State is UNKNOWN — certify owns it."""
    node_id = _id_for_discovery(d)
    return Node(
        id=node_id,
        type=d.node_type,
        name=d.name,
        layer=d.layer,
        discovered_by=DiscoveredBy.RUNTIME,
        state=State.UNKNOWN,
        check_command=d.check_command,
        evidence=d.evidence,
        provenance="runtime ingest",
        data={"runtime_confidence": d.confidence, **d.data},
    )


def _id_for_discovery(d: Discovery) -> str:
    if d.node_type is NodeType.PACKAGE:
        return package_id(d.name, None)
    if d.node_type is NodeType.SYSTEM_LIB:
        return syslib_id(d.name)
    if d.node_type is NodeType.TOOL:
        return tool_id(d.name)
    if d.node_type is NodeType.CONFIG:
        return config_id(d.name)
    if d.node_type is NodeType.SERVICE:
        return service_id(d.name)
    # Should never be reached for the five discovery types.
    raise ValueError(f"Unsupported discovery node_type: {d.node_type}")


# ---------------------------------------------------------------------------
# Annotate-or-append (spec §9)
# ---------------------------------------------------------------------------

def _find_existing_node(graph: DepGraph, d: Discovery) -> Node | None:
    """Existing node this discovery should annotate, or None to append.

    A runtime PACKAGE discovery has no version (id ``pkg:<name>``) but the static
    resolver records ``pkg:<name>==<version>`` — so match PACKAGE by normalized
    name, not just by exact id, or every real package would be appended twice.

    An unresolved import mapping yields ``d.name is None``; there is no id or
    normalized name to look up, so treat it as never matching an existing node.
    """
    if d.name is None:
        return None
    direct = graph.get(_id_for_discovery(d))
    if direct is not None:
        return direct
    if d.node_type is NodeType.PACKAGE:
        want = normalize_package_name(d.name)
        for n in graph.nodes:
            if n.type is NodeType.PACKAGE and normalize_package_name(n.name) == want:
                return n
    return None


def diverged_node_ids(graph: DepGraph, discoveries) -> tuple[str, ...]:
    """Ids of discoveries that map to an already-SATISFIED node (spec §8).

    Such a residual means NECESSARY (the graph says present) and SUFFICIENT
    (tests still red referencing it) have diverged — adding more nodes will not
    close it. The orchestrator routes these to an honest give-up, not another
    loop iteration. Pure; reads state only.
    """
    out: list[str] = []
    for d in discoveries:
        existing = _find_existing_node(graph, d)
        if existing is not None and existing.state is State.SATISFIED:
            out.append(existing.id)
    return tuple(out)


def _annotate_or_append(graph: DepGraph, d: Discovery, owner_node_id: str | None = None) -> DepGraph:
    """Apply one Discovery to the graph idempotently.  Returns a NEW graph."""
    if d.name is None:
        # Unresolved import: no distribution to graph. Never fabricate a `pkg:None`
        # node (plan invariant: an unmapped import produces NO root). The discovery
        # is still recorded in `found` by the caller for advisory/logging.
        return graph
    existing = _find_existing_node(graph, d)
    target_id = existing.id if existing is not None else _id_for_discovery(d)

    if existing is None:
        # Append: brand-new requirement discovered at runtime.
        new_node = _node_for_discovery(d)
    else:
        # Annotate: merge stronger runtime evidence onto the existing node.
        # runtime evidence is strictly stronger than static (spec §8);
        # update discovered_by + evidence + runtime_confidence.
        new_data = {**dict(existing.data), "runtime_confidence": d.confidence}
        if d.data:
            new_data.update(d.data)
        new_node = replace(
            existing,
            discovered_by=DiscoveredBy.RUNTIME,
            evidence=d.evidence,
            data=new_data,
        )

    new_graph = graph.with_node(new_node)

    # Hang the REQUIRES edge from the CULPRIT owner when one is known, present, AND a
    # legal requires-src type; else fall back to the global Test node (spec §7).
    # Owner precedence: explicit owner_node_id > d.requires_of > TEST_NODE_ID.
    # CRITICAL: EDGE_RULES["requires"] only allows src in {Test, Project, Import,
    # Package} (schema.py). If the LLM sets requires_of to e.g. a syslib id, an
    # unguarded with_edge would RAISE, and ingest's per-observation try/except
    # (runtime_ingest.py:156-157) would SILENTLY DROP the whole discovery. Validate
    # the src type first and fall back to Test if it is not a legal requires-src.
    _VALID_REQUIRES_SRC = {"Test", "Project", "Import", "Package"}
    owner = owner_node_id or d.requires_of
    owner_node = new_graph.get(owner) if owner is not None else None
    if owner_node is not None and owner_node.type.value in _VALID_REQUIRES_SRC:
        src_id = owner
    else:
        src_id = TEST_NODE_ID
    if new_graph.get(src_id) is not None:
        edge = Edge(src=src_id, dst=target_id, relation=EdgeType.REQUIRES, origin="runtime")
        new_graph = new_graph.with_edge(edge)

    return new_graph


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def ingest_runtime_failures(
    graph: DepGraph,
    observations: list[tuple[str, str]],
    classifiers: Sequence[Callable] = (classify_observation,),
    owner_node_id: str | None = None,
) -> tuple[DepGraph, list[Discovery]]:
    """Map observations to Discoveries, apply them idempotently to ``graph``.

    ``observations`` is a list of ``(command, output)`` tuples (one per ledger
    event since the last ingest).  ``classifiers`` is tried in order; the first
    non-None result wins per observation.

    Returns ``(new_graph, found)`` where ``found`` is the list of all non-None
    Discoveries (for logging / advisory re-render).  Never raises — any
    per-observation exception logs a warning and skips that observation.
    """
    new = graph
    found: list[Discovery] = []

    for cmd, out in observations:
        try:
            d: Discovery | None = None
            for classifier in classifiers:
                d = classifier(cmd, out)
                if d is not None:
                    break
            if d is None:
                continue
            new = _annotate_or_append(new, d, owner_node_id)
            found.append(d)
        except Exception as exc:  # noqa: BLE001 — must never break the run (spec §11)
            logger.warning("runtime_ingest: skipped observation (%r): %s", cmd[:60], exc)

    return new, found
