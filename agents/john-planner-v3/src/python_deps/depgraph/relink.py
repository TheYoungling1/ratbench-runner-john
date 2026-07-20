"""Stage 4a — certified Import->Package relink from packages_distributions().

After ``install_closure`` has installed the resolved closure, the container can
report the ground-truth import-name -> distribution map via
``importlib.metadata.packages_distributions()`` (Python 3.10+). This stage uses it
to add CERTIFIED ``Import->Package`` edges — e.g. ``import dateutil`` provided by
dist ``python-dateutil``. It is now the SOLE Import->Package source in
construction: the provisional pre-install heuristic
(``resolve.link_imports_to_packages``) is retired from the build path. Discovery
only: it adds edges + honest ``unresolved`` data flags, never node state.

Pure parser + pure edge builder + thin executor orchestrator (repo immutability:
every "mutation" returns a NEW ``DepGraph``).
"""

from __future__ import annotations

import json
from dataclasses import replace

from python_deps.depgraph.executor import Executor
from python_deps.depgraph.schema import DepGraph, Edge, EdgeType, NodeType
from python_deps.import_mapping import (
    normalize_package_name,
    top_level_import_name,
)

PACKAGES_DIST_CMD = (
    'python -c "import importlib.metadata, json; '
    'print(json.dumps(importlib.metadata.packages_distributions()))"'
)


def parse_packages_distributions(stdout: str) -> dict[str, list[str]]:
    """Parse the JSON ``{import_name: [dist, ...]}`` map; ``{}`` if malformed."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, val in data.items():
        if isinstance(key, str) and isinstance(val, list):
            out[key] = [v for v in val if isinstance(v, str)]
    return out


def import_to_package_edges(
    graph: DepGraph, dist_map: dict[str, list[str]]
) -> list[Edge]:
    """Certified Import->Package edges from a packages_distributions() map.

    Module keys are real names (``PIL``, ``MySQLdb``) so match case-insensitively;
    distribution names match a Package node by canonical (PEP 503) name. A
    namespace import (multiple dists) links to every dist that is present as a
    Package node. Edges already in the graph are skipped (no duplicates).
    """
    pkg_by_canon = {
        normalize_package_name(n.name): n.id
        for n in graph.nodes
        if n.type is NodeType.PACKAGE
    }
    dist_by_module = {module.lower(): dists for module, dists in dist_map.items()}
    existing = {
        (e.src, e.dst) for e in graph.edges if e.relation is EdgeType.REQUIRES
    }

    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for node in graph.nodes:
        if node.type is not NodeType.IMPORT:
            continue
        module = top_level_import_name(node.name).lower()
        for dist in dist_by_module.get(module, ()):
            pkg_id = pkg_by_canon.get(normalize_package_name(dist))
            if pkg_id is None:
                continue
            key = (node.id, pkg_id)
            if key in existing or key in seen:
                continue
            seen.add(key)
            edges.append(
                Edge(
                    src=node.id,
                    dst=pkg_id,
                    relation=EdgeType.REQUIRES,
                    origin="certified",
                )
            )
    return edges


def flag_unresolved_imports(graph: DepGraph) -> DepGraph:
    """Flag an IMPORT node ``unresolved`` — an honest under-declaration signal,
    NOT a fabricated root — only when it is BOTH unprovided (no outgoing REQUIRES
    edge to a Package after Tier-1 relink) AND non-optional. A guarded import
    (``data["optional"] is True``, tagged by the scan — a try/except-ImportError
    body or an ``if`` branch, e.g. a ``sys.platform``/``sys.version_info`` fork)
    is deliberate, not under-declared, so it is exempt and left unflagged. Test
    goal is separated by the scan; optional imports are exempt. Uses Node.data
    (state is the host-certification axis and does not apply to imports).

    Idempotent: an IMPORT that is now provided OR now known optional has any
    STALE ``unresolved`` flag (and the evidence this function set) cleared, so
    re-running yields the same result regardless of the flag state carried into
    the call. Only the ``unresolved``/evidence keys are touched — every other
    ``data`` key is preserved. Returns a NEW graph.
    """
    provided = _provided_imports(graph)
    new = graph
    for node in graph.nodes:
        if node.type is not NodeType.IMPORT:
            continue
        unprovided = node.id not in provided
        non_optional = node.data.get("optional") is not True
        if unprovided and non_optional:
            new = new.with_node(
                replace(
                    node,
                    data={**dict(node.data), "unresolved": True},
                    evidence=f"unresolved: no distribution provides import {node.name}",
                )
            )
            continue
        # Provided OR optional -> must NOT be flagged. Clear any STALE flag (and
        # the evidence this function set), else leave the node byte-for-byte
        # untouched so the common/first-run path never rewrites needlessly.
        if node.data.get("unresolved") is not True:
            continue
        data = dict(node.data)
        data.pop("unresolved", None)
        new = new.with_node(replace(node, data=data, evidence=None))
    return new


def flag_runtime_import_failure(
    graph: DepGraph, import_node_id: str, *, reason: str
) -> DepGraph:
    """Flag a METADATA-PRESENT Import whose run-time ``import X`` failed for a
    NON-native reason (P2.3, Correction 4) — the third failure class.

    Distinct from ``flag_unresolved_imports``' ``data["unresolved"]`` (metadata
    ABSENT: nothing provides the import name — under-declaration, Phase A): here a
    distribution DOES provide the import (there is an outgoing REQUIRES->Package
    edge, i.e. relink certified a provider), yet ``import X`` still raises for a
    reason that is neither a missing shared library (that path fabricates a
    ``SystemLib``) nor under-declaration (a broken / under-provisioned dist, a
    Python-level ``ImportError``/``RuntimeError`` at import time). Sets
    ``data["unresolved_runtime"] = True`` + a short ``data["import_error"]``
    (``reason``) on the Import node.

    Scoped to METADATA-PRESENT Imports: the flag is applied ONLY when the target is
    an Import node that is PROVIDED (has an outgoing REQUIRES->Package edge). A
    metadata-ABSENT Import is already honestly flagged ``unresolved`` (P0.3) and
    must NOT be double-flagged — an unprovided Import (including one carrying the
    ``unresolved`` flag) is left byte-for-byte untouched. Every other ``data`` key
    is preserved. Returns a NEW graph (a no-op when the target is absent, not an
    Import, or not provided).
    """
    node = graph.get(import_node_id)
    if node is None or node.type is not NodeType.IMPORT:
        return graph
    if node.id not in _provided_imports(graph):
        return graph
    data = {**dict(node.data), "unresolved_runtime": True, "import_error": reason}
    return graph.with_node(replace(node, data=data))


def _provided_imports(graph: DepGraph) -> set[str]:
    """Import-node ids with an outgoing certified REQUIRES->Package edge (a dist
    provides the import name). Mirrors the ``provided`` set in
    ``flag_unresolved_imports`` so metadata-presence is defined identically."""
    return {
        e.src
        for e in graph.edges
        if e.relation is EdgeType.REQUIRES
        and (dst := graph.get(e.dst)) is not None
        and dst.type is NodeType.PACKAGE
    }


def certified_import_links(graph: DepGraph, executor: Executor) -> DepGraph:
    """Stage 4a: add certified Import->Package edges from the container.

    Runs ``packages_distributions()`` in the (post-install) container, links every
    Import to its certified provider Package, then flags every still-unprovided
    non-optional Import ``unresolved`` (P0.3). This is the SOLE Import->Package
    source in construction: with declared-only roots (P0.1) an import never
    becomes a MISSING placeholder Package, so there are no identity-fallback
    ghosts to sweep. On command failure the graph is returned unchanged.
    """
    result = executor.run(PACKAGES_DIST_CMD)
    if not result.ok:
        return graph
    dist_map = parse_packages_distributions(result.stdout)
    edges = import_to_package_edges(graph, dist_map)
    new = graph
    for edge in edges:
        new = new.with_edge(edge)
    return flag_unresolved_imports(new)
