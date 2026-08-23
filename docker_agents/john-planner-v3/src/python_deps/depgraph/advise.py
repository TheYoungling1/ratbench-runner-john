"""Phase-0 advisory render: a ``DepGraph`` -> the string the planner reads.

This is the *single integration surface* for the dep-graph "shadow" stage
(``docs/superpowers/plans/2026-06-23-phase0-depgraph-advisory.md``): the graph is
built once in a scratch container, rendered here, and spliced into the planner
prompt.  Advisory only — nothing here mutates the graph, the map, or the loop.

The render is relevance-gated (``docs/DESIGN-tiered-layer-execution-strategy.md``
§3): full detail on the unsatisfied **frontier** (the nodes the planner can act
on) and a one-line **satisfied summary** (counts per layer, never one line per
node), so a 50-package closure stays a few lines, not a wall.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from python_deps.depgraph.build import build_dep_graph
from python_deps.depgraph.executor import DockerExecutor, Executor, LocalSubprocessExecutor
from python_deps.depgraph.emit import partition
from python_deps.depgraph.schema import DepGraph, DiscoveredBy, EdgeType, Layer, Node, NodeType, State

logger = logging.getLogger(__name__)

_HEADER = "[DEPENDENCY GRAPH - advisory * host-certified in scratch container]"

# Bottom-up layer order, so the frontier reads base -> ... -> tests.
_LAYER_RANK: dict[Layer, int] = {
    Layer.INTERPRETER: 0,
    Layer.SYSTEM: 1,
    Layer.TOOLCHAIN: 2,
    Layer.PIP: 3,
    Layer.NAMING: 4,
    Layer.CONFIG: 5,
    Layer.RUNTIME: 5,
    Layer.TESTS: 6,
}

# Lines that actually name a cause sit at the END of a traceback, not the header.
_ERROR_HINT = re.compile(
    r"(error|not found|not installed|no information|cannot open|no module|"
    r"no such file|unable to locate|failed|missing)",
    re.I,
)
_TRACEBACK_HEADER = "traceback (most recent call last):"
_MAX_EVIDENCE = 160


def _best_evidence_line(evidence: str | None) -> str | None:
    """The most informative line of ``evidence``.

    A probe/certify failure stores the command's full stderr; the useful line is
    the *cause* at the end of a traceback (``ImportError: libGL.so.1: ...``), not
    the generic ``Traceback (most recent call last):`` header the naive ``[0]``
    slice would pick.  Prefer the last error-looking line, else the last line.
    """
    if not evidence:
        return None
    lines = [ln.strip() for ln in evidence.splitlines() if ln.strip()]
    candidates = [ln for ln in lines if ln.lower() != _TRACEBACK_HEADER] or lines
    if not candidates:
        return None
    for line in reversed(candidates):
        if _ERROR_HINT.search(line):
            return line[:_MAX_EVIDENCE]
    return candidates[-1][:_MAX_EVIDENCE]


def _needed_by(graph: DepGraph, node: Node) -> list[str]:
    """Names of nodes that ``requires`` ``node`` — excluding the same-name
    Import->Package naming link (rendering it reads as "psycopg2 needed by
    psycopg2", which is noise, not a real dependency)."""
    names = {
        pred.name for pred in graph.required_by(node.id) if pred.name != node.name
    }
    return sorted(names)


def _project_deps(graph: DepGraph, proj_id: str) -> list[str]:
    return sorted(
        {
            n.name
            for n in graph.requires_of(proj_id)
            if n.type in (NodeType.PACKAGE, NodeType.IMPORT)
        }
    )


def _recent_attempts(node: Node, limit: int = 2) -> str:
    return "; ".join(f"{a.command} -> {a.outcome}" for a in node.attempts[-limit:])


def render_dep_graph_advisory(graph: DepGraph) -> str:
    """Render ``graph`` as the planner-facing advisory section.

    Returns ``""`` for an empty/uninformative graph (so the caller adds nothing
    to the prompt).
    """
    if not graph.nodes:
        return ""

    lines = [_HEADER]

    goal = next((n for n in graph.nodes if n.type is NodeType.TEST), None)
    proj = next((n for n in graph.nodes if n.type is NodeType.PROJECT), None)
    if goal is not None:
        lines.append(f"GOAL     {goal.name:24} {goal.state.value}")
    if proj is not None:
        deps = _project_deps(graph, proj.id)
        lines.append(
            f"PROJECT  {proj.name:24} -> "
            f"{', '.join(deps) if deps else '(none declared)'}"
        )

    unsatisfied_nodes = sorted(
        (
            n
            for n in graph.nodes
            if n.state is State.MISSING and n.type is not NodeType.TEST
        ),
        key=lambda n: (_LAYER_RANK.get(n.layer, 9), n.name),
    )
    if unsatisfied_nodes:
        lines.append("")
        lines.append("UNSATISFIED (act here):")
        for n in unsatisfied_nodes:
            lines.append(
                f"  {n.layer.value.upper():9} {n.name}   [{n.type.value}]  MISSING"
            )
            ev = _best_evidence_line(n.evidence)
            if ev:
                lines.append(f"            evidence: {ev}")
            nb = _needed_by(graph, n)
            if nb:
                lines.append(f"            needed by: {', '.join(nb)}")
            if n.fix_candidates:
                marker = "  (value needed)" if any(
                    f.endswith("=?") for f in n.fix_candidates
                ) else ""
                lines.append(
                    f"            fix-candidate: {', '.join(n.fix_candidates)}{marker}"
                )
            attempts = _recent_attempts(n)
            if attempts:
                lines.append(f"            attempts: {attempts}")

    satisfied = [n for n in graph.nodes if n.state is State.SATISFIED]
    if satisfied:
        counts: dict[str, int] = {}
        for n in satisfied:
            counts[n.layer.value] = counts.get(n.layer.value, 0) + 1
        summary = " * ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        lines.append("")
        lines.append(f"SATISFIED (summary): {summary}")

    services = sorted(
        (n for n in graph.nodes if n.type is NodeType.SERVICE), key=lambda n: n.name
    )
    if services:
        lines.append("")
        lines.append("SERVICES (declared — reachability NOT certified here):")
        for n in services:
            fix = n.fix_candidates[0] if n.fix_candidates else "?"
            setup = n.data.get("setup")
            # A setup-shape Service is a declared, provisioned obligation the
            # scheduler/certify path treats as MANDATORY (blocks 'done') -> [setup].
            # A non-setup Service is an advisory hint (runtime/llm classifier),
            # never certified -> [inferred] + the "may be mocked" caveat.
            label = "setup" if setup is not None else "inferred"
            line = f"  {n.name:10} [{label}]   fix: {fix}"
            bound = n.data.get("bound_config")
            if bound:
                line += f"   addresses: {bound}"
            if setup is None:
                line += "   (may be mocked — agent's call)"
            lines.append(line)
            if setup is not None:
                # Clean CR6 setup shape: the declared service provisioning recipe.
                kind = n.data.get("service_kind", n.name)
                lines.append(f"            declared setup-service (kind: {kind})")
                if setup.get("start"):
                    lines.append(f"            start: {setup['start']}")
                if setup.get("createdb"):
                    lines.append(f"            then: {setup['createdb']}")

    if len(lines) == 1:  # header only — nothing worth telling the planner
        return ""
    return "\n".join(lines)


_PLANNER_HEADER = (
    "[DEPENDENCY GRAPH - unified * host-certified in live container]"
    "  (authoritative for deps/system/toolchain; build/tests/config -> open_problems)"
)


def _chain_to_goal(graph: DepGraph, node: Node, limit: int = 6) -> str:
    """Render the transitive required_by chain up to a Project/Test root.

    'lxml <- app <- repo_tests_pass'. Picks one predecessor per hop (the first by
    name) — enough to show the planner why the node matters. Cycle-guarded.
    """
    chain = [node.name]
    seen = {node.id}
    cur = node
    for _ in range(limit):
        preds = [p for p in graph.required_by(cur.id) if p.id not in seen]
        if not preds:
            break
        cur = sorted(preds, key=lambda p: p.name)[0]
        chain.append(cur.name)
        seen.add(cur.id)
        if cur.type is NodeType.TEST:
            break
    return " <- ".join(chain)


def _conflict_note(graph: DepGraph, node: Node) -> str | None:
    for e in graph.edges:
        if e.relation is EdgeType.CONFLICTS_WITH and node.id in (e.src, e.dst):
            other = e.dst if e.src == node.id else e.src
            other_node = graph.get(other)
            other_name = other_node.name if other_node else other
            summary = e.data.get("summary") if e.data else None
            detail = f" ({summary})" if summary else ""
            return f"conflict: {node.name} vs {other_name}{detail}"
    return None


def _platform_note(node: Node) -> str | None:
    if node.resolved_python or node.resolved_platform:
        return f"resolved for: {node.resolved_python or '?'} / {node.resolved_platform or '?'}"
    return None


def render_depgraph_planner(
    graph: DepGraph, changed_ids: frozenset[str] = frozenset()
) -> str:
    """Unified planner-facing render: certified counts + a rich frontier packet.

    The FRONTIER is derived from ``partition(graph).frontier`` — only actionable
    installable nodes appear; un-resolvable Import/Runtime nodes are excluded
    (R1: frontier scoping).
    """
    if not graph.nodes:
        return ""

    lines = [_PLANNER_HEADER]

    goal = next((n for n in graph.nodes if n.type is NodeType.TEST), None)
    if goal is not None:
        lines.append(f"GOAL     {goal.name:24} {goal.state.value}")

    satisfied = [n for n in graph.nodes if n.state is State.SATISFIED]
    if satisfied:
        counts: dict[str, int] = {}
        for n in satisfied:
            counts[n.layer.value] = counts.get(n.layer.value, 0) + 1
        summary = " * ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        lines.append(f"CERTIFIED  {summary}   (host-checked in live container)")

    frontier = sorted(
        partition(graph).frontier,
        key=lambda n: (_LAYER_RANK.get(n.layer, 9), n.name),
    )
    if frontier:
        lines.append("")
        lines.append("FRONTIER (graph could not auto-resolve - your call):")
        for n in frontier:
            mark = "  (re-checked this cycle)" if n.id in changed_ids else ""
            lines.append(f"  {n.layer.value.upper():9} {n.name}   [{n.type.value}]  MISSING{mark}")
            ev = _best_evidence_line(n.evidence)
            if ev:
                lines.append(f"            evidence: {ev}")
            lines.append(f"            chain: {_chain_to_goal(graph, n)}")
            conflict = _conflict_note(graph, n)
            if conflict:
                lines.append(f"            {conflict}")
            plat = _platform_note(n)
            if plat:
                lines.append(f"            {plat}")
            if n.attempts:
                hist = "; ".join(f"{a.command} -> {a.outcome}" for a in n.attempts[-4:])
                lines.append(f"            attempts: {hist}")

    frontier_ids = {n.id for n in frontier}
    runtime_nodes = sorted(
        (n for n in graph.nodes
         if n.discovered_by is DiscoveredBy.RUNTIME and n.id not in frontier_ids),
        key=lambda n: (_LAYER_RANK.get(n.layer, 9), n.name),
    )
    if runtime_nodes:
        lines.append("")
        lines.append("RUNTIME-DISCOVERED (observed this run — not in the static frontier):")
        for n in runtime_nodes:
            lines.append(
                f"  {n.layer.value.upper():9} {n.name}   [{n.type.value}]  {n.state.value}"
            )
            ev = _best_evidence_line(n.evidence)
            if ev:
                lines.append(f"            evidence: {ev}")

    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def build_advisory_for_repo(
    repo_path: str,
    base_image: str,
    *,
    host_executor: Executor | None = None,
    target_python: str | None = None,
    classify: Callable | None = None,
) -> tuple[str, DepGraph | None]:
    """Build the dep graph in a fresh scratch container and render the advisory.

    Returns ``(advisory_str, graph)``.  On **any** failure (Docker down, resolve
    error, install timeout) logs and returns ``("", None)`` — the caller must
    proceed exactly as if the feature were off (graceful degradation; the live
    agent container is never touched because probing uses its own throwaway
    ``DockerExecutor``).
    """
    try:
        host = host_executor or LocalSubprocessExecutor()
        with DockerExecutor(base_image) as scratch:
            graph = build_dep_graph(
                repo_path, scratch, host_executor=host, target_python=target_python,
            )
        if classify is not None:
            from ecosystems.registry import PROVIDERS, select_provider   # defensive: mirrors build.py's provider-import style
            provider = select_provider(repo_path, PROVIDERS, default=PROVIDERS[0])
            graph = provider.service_obligations(graph, repo_path, classify)   # Phase 3
        return render_dep_graph_advisory(graph), graph
    except Exception as exc:  # noqa: BLE001 — advisory must never break a run
        logger.warning("dep-graph advisory unavailable: %s", exc)
        return "", None
