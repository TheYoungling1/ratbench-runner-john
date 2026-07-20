"""Pure emit core: classify the graph and turn the certified closure into a recipe.

This module is the deterministic counterpart to the LLM recipe loop: it decides
which MISSING nodes the host can install without judgement (EMITTABLE), which
require the LLM (FRONTIER), and emits an ordered, layer-correct recipe for the
emittable set. Pure with respect to its inputs — no Docker, no network, no
subprocess (mirrors probe.py / advise.py). Returns neutral EmitStep objects so
this package keeps its zero dependency on src.envstate (world_model.py:22).
"""
from __future__ import annotations

from dataclasses import dataclass

from python_deps.depgraph.schema import (
    DepGraph,
    EdgeType,
    Layer,
    Node,
    NodeType,
    State,
)

# Node types the host can directly install. Import/Test/Project/Runtime are
# structural — satisfied via their Package (naming relink) or out of scope here.
_INSTALLABLE: tuple[NodeType, ...] = (
    NodeType.PACKAGE,
    NodeType.SYSTEM_LIB,
    NodeType.TOOL,
)

# Emit attempts are tagged with this sentinel in Attempt.check (set by
# depgraph_live.emit_drain). A node that fails to emit this many times stops being
# emittable and is escalated to the FRONTIER for the LLM — without this, the drain
# re-emits the same doomed closure every cycle (observed: 12 identical attempts).
EMIT_ATTEMPT_TAG = "emit"
MAX_EMIT_ATTEMPTS = 2


def _failed_emit_attempts(node: Node) -> int:
    return sum(
        1 for a in node.attempts
        if a.check == EMIT_ATTEMPT_TAG and a.outcome == "failed"
    )


@dataclass(frozen=True)
class Partition:
    certified: tuple[Node, ...]
    emittable: tuple[Node, ...]
    frontier: tuple[Node, ...]


def _conflicted_ids(graph: DepGraph) -> set[str]:
    """Node ids touched by a conflicts_with edge (uv unsat core) — never emit."""
    ids: set[str] = set()
    for e in graph.edges:
        if e.relation is EdgeType.CONFLICTS_WITH:
            ids.add(e.src)
            ids.add(e.dst)
    return ids


def _toolchain_ready(graph: DepGraph, pkg: Node) -> bool:
    """Native readiness gate. Runtime SystemLib deps must be SATISFIED before ANY
    package emits (a wheel dlopens them too). Build-time Tool deps gate source
    builds and unknown build mode; only a known wheel (build_from_source is False)
    skips them. Soft requires edges never block (invariant #10; mirrors
    schedule._dependencies_satisfied)."""
    for edge in graph.edges:
        if not (edge.src == pkg.id and edge.relation is EdgeType.REQUIRES
                and edge.data.get("hard", True)):
            continue
        dep = graph.get(edge.dst)
        if dep is None or dep.state is State.SATISFIED:
            continue
        if dep.type is NodeType.SYSTEM_LIB:
            return False                               # runtime lib: wheel & sdist
        if dep.type is NodeType.TOOL and pkg.build_from_source is not False:
            return False       # build tool blocks source builds AND unknown build mode;
                               # only a KNOWN wheel (build_from_source is False) skips it
    return True


def _is_emittable(graph: DepGraph, node: Node, conflicted: set[str]) -> bool:
    if node.state is not State.MISSING:
        return False
    if node.id in conflicted:
        return False
    if _failed_emit_attempts(node) >= MAX_EMIT_ATTEMPTS:
        return False               # repeatedly failed to emit -> the LLM's call
    if node.type is NodeType.PACKAGE:
        if not node.version:           # unresolved -> the LLM's call
            return False
        if not _toolchain_ready(graph, node):
            return False               # native/runtime-link deps must certify first
                                        # (wheel OR sdist — a wheel still dlopens libs)
        return True
    if node.type in (NodeType.SYSTEM_LIB, NodeType.TOOL):
        return bool(node.chosen_fix and node.chosen_fix.startswith("apt:"))
    return False


def partition(graph: DepGraph) -> Partition:
    """Classify installable nodes into certified / emittable / frontier."""
    conflicted = _conflicted_ids(graph)
    certified: list[Node] = []
    emittable: list[Node] = []
    frontier: list[Node] = []
    for n in graph.nodes:
        if n.type not in _INSTALLABLE:
            continue
        if n.state is State.SATISFIED:
            certified.append(n)
        elif _is_emittable(graph, n, conflicted):
            emittable.append(n)
        elif n.state is State.MISSING:
            frontier.append(n)
        # UNKNOWN with no decision: neither emitted nor escalated.
    return Partition(tuple(certified), tuple(emittable), tuple(frontier))


def _is_reciped(node: Node) -> bool:
    """A node the deterministic recipe layer can install (mirrors _is_emittable's
    type/fix test, minus the attempt cap — a backed-off node is still 'reciped')."""
    if node.type is NodeType.PACKAGE:
        return bool(node.version)
    if node.type in (NodeType.SYSTEM_LIB, NodeType.TOOL):
        return bool(node.chosen_fix) and node.chosen_fix.startswith("apt:")
    return False


def _is_installable_project(node: Node) -> bool:
    """The repo under test, when it declares a build system (pyproject/setup.py,
    recorded as ``data['installable']`` at construction). Rendered as the FINAL
    editable install — the capstone after every dependency — so it is deliberately
    NOT part of ``_is_reciped`` (the third-party recipe set): keeping it separate
    is what lets the renderer emit it once, last, without a per-layer double-emit."""
    return node.type is NodeType.PROJECT and bool(node.data.get("installable"))


def failed_reciped_nodes(graph: DepGraph) -> tuple[Node, ...]:
    """Reciped, host-checkable nodes still MISSING after a drain whose deps are
    SATISFIED — the spec's `isolate`. Excludes CONFIG/SERVICE (advisory) and
    nodes with no check_command (no host stop condition)."""
    from python_deps.depgraph.schedule import _dependencies_satisfied
    out = []
    for n in graph.nodes:
        if n.state is not State.MISSING:
            continue
        if not n.check_command:
            continue
        if not _is_reciped(n):
            continue
        if not _dependencies_satisfied(graph, n):
            continue
        out.append(n)
    return tuple(out)


# Bottom-up execution rank (matches certify._LAYER_ORDER / advise._LAYER_RANK).
_LAYER_RANK: dict[Layer, int] = {
    Layer.INTERPRETER: 0,
    Layer.SYSTEM: 1,
    Layer.TOOLCHAIN: 2,
    Layer.PIP: 3,
    Layer.NAMING: 4,
    Layer.RUNTIME: 5,
    Layer.TESTS: 6,
}


def topo_order(graph: DepGraph, nodes: tuple[Node, ...]) -> tuple[Node, ...]:
    """Order ``nodes`` dependency-first (a required node before its dependent).

    Kahn's algorithm over ``requires`` edges restricted to the node set; ties
    broken by (layer rank, name) for reproducibility. On a cycle (should not
    happen for a resolved closure) the remaining nodes are emitted in
    layer-rank+name order rather than raising — emit must never crash a run.
    """
    ids = {n.id for n in nodes}
    by_id = {n.id: n for n in nodes}
    deps: dict[str, set[str]] = {nid: set() for nid in ids}
    for e in graph.edges:
        if e.relation is EdgeType.REQUIRES and e.src in ids and e.dst in ids:
            deps[e.src].add(e.dst)

    ordered: list[Node] = []
    placed: set[str] = set()
    remaining = set(ids)
    while remaining:
        ready = [nid for nid in remaining if deps[nid] <= placed]
        if not ready:                      # cycle — emit the rest deterministically
            ready = list(remaining)
        ready.sort(key=lambda nid: (_LAYER_RANK.get(by_id[nid].layer, 9), by_id[nid].name))
        nxt = ready[0]
        ordered.append(by_id[nxt])
        placed.add(nxt)
        remaining.discard(nxt)
    return tuple(ordered)


_APT_PREFIX = "apt:"


@dataclass(frozen=True)
class EmitStep:
    kind: str                      # AttemptKind value: system_install | python_install
    command: str
    target_node_ids: tuple[str, ...]


def _apt_name(node: Node) -> str | None:
    if node.chosen_fix and node.chosen_fix.startswith(_APT_PREFIX):
        return node.chosen_fix[len(_APT_PREFIX):]
    return None


def _pip_spec(node: Node) -> str:
    return f"{node.name}=={node.version}" if node.version else node.name


def build_recipe(graph: DepGraph, ordered: tuple[Node, ...]) -> tuple[EmitStep, ...]:
    """Turn the topo-ordered emittable set into at most two steps (D2):

    1. one apt step for all SystemLib/Tool nodes (dedup apt names), and
    2. one pinned-closure pip step for all Package nodes (resolver-consistent).

    Cross-layer / build-from-source ordering is handled by the drain loop across
    iterations, so a single pass needs only apt-before-pip.
    """
    syslibs = [n for n in ordered if n.type in (NodeType.SYSTEM_LIB, NodeType.TOOL)]
    packages = [n for n in ordered if n.type is NodeType.PACKAGE]
    steps: list[EmitStep] = []

    if syslibs:
        names: list[str] = []
        for n in syslibs:
            apt = _apt_name(n)
            if apt and apt not in names:
                names.append(apt)
        if names:
            steps.append(EmitStep(
                kind="system_install",
                command="apt-get update && apt-get install -y " + " ".join(names),
                target_node_ids=tuple(n.id for n in syslibs),
            ))

    if packages:
        specs = " ".join(_pip_spec(n) for n in packages)
        # python3 (not `python`): real agent bases often ship only python3 (a bare
        # `python` exits 127). --break-system-packages: those bases are usually
        # PEP-668 externally-managed, which otherwise blocks a system pip install.
        # Both are no-ops on a clean python image, so this stays portable.
        steps.append(EmitStep(
            kind="python_install",
            command="python3 -m pip install --break-system-packages " + specs,
            target_node_ids=tuple(n.id for n in packages),
        ))
    return tuple(steps)


def next_deterministic_wave(graph: DepGraph) -> tuple[EmitStep, ...]:
    """The current topological wave: the emittable frontier collapsed to ≤2 batch
    EmitSteps (apt + pip), deps-before-dependents. Empty when nothing is emittable.

    A batch IS a wave: partition() only surfaces nodes whose REQUIRES-deps
    are already SATISFIED, and already excludes backoff-capped / conflicting nodes.
    """
    part = partition(graph)
    if not part.emittable:
        return ()
    ordered = topo_order(graph, part.emittable)
    return build_recipe(graph, ordered)
