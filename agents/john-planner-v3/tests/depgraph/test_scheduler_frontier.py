# tests/depgraph/test_scheduler_frontier.py
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph, Node, Edge, NodeType, Layer, State, EdgeType, DiscoveredBy,
)
from python_deps.depgraph.schedule import scheduler_frontier  # noqa: E402


def _node(nid, ntype, name, state, *, check="true", layer=Layer.PIP):
    return Node(
        id=nid, type=ntype, name=name, layer=layer,
        discovered_by=DiscoveredBy.STATIC_SCAN, state=state, check_command=check,
    )


def test_missing_node_with_check_is_actionable():
    g = DepGraph().with_node(_node("pkg:requests", NodeType.PACKAGE, "requests", State.MISSING))
    assert [n.id for n in scheduler_frontier(g)] == ["pkg:requests"]


def test_satisfied_and_unknown_are_excluded():
    g = (
        DepGraph()
        .with_node(_node("pkg:a", NodeType.PACKAGE, "a", State.SATISFIED))
        .with_node(_node("pkg:b", NodeType.PACKAGE, "b", State.UNKNOWN))
    )
    assert scheduler_frontier(g) == ()


def test_missing_without_check_command_is_excluded():
    g = DepGraph().with_node(_node("pkg:c", NodeType.PACKAGE, "c", State.MISSING, check=None))
    assert scheduler_frontier(g) == ()


def test_node_with_unsatisfied_dependency_is_held_back():
    g = (
        DepGraph()
        .with_node(_node("syslib:libpq", NodeType.SYSTEM_LIB, "libpq", State.MISSING, layer=Layer.SYSTEM))
        .with_node(_node("pkg:app", NodeType.PACKAGE, "app", State.MISSING))
        .with_edge(Edge(src="pkg:app", dst="syslib:libpq", relation=EdgeType.REQUIRES))
    )
    assert [n.id for n in scheduler_frontier(g)] == ["syslib:libpq"]


def test_dependencies_ordered_before_dependents():
    # The gate (every dependency SATISFIED) holds a dependent back while its dep is
    # still MISSING, so a dependent and its MISSING dependency never co-occur in one
    # frontier. Ordering within a frontier therefore applies to independently
    # actionable nodes: topo_order tie-breaks them by layer rank (SYSTEM before PIP).
    # Both siblings are MISSING with no edges between them, so both surface this pass,
    # lower-layer first.
    g = (
        DepGraph()
        .with_node(_node("syslib:libpq", NodeType.SYSTEM_LIB, "libpq", State.MISSING, layer=Layer.SYSTEM))
        .with_node(_node("pkg:psycopg2", NodeType.PACKAGE, "psycopg2", State.MISSING))
    )
    front = [n.id for n in scheduler_frontier(g)]
    # SYSTEM layer ranks before PIP, so the system lib is scheduled first.
    assert front == ["syslib:libpq", "pkg:psycopg2"]


def test_diamond_orders_root_first():
    # d depends on b and c; b and c depend on a → a before b,c before d
    g = DepGraph()
    for nid in ("a", "b", "c", "d"):
        g = g.with_node(_node(f"pkg:{nid}", NodeType.PACKAGE, nid, State.MISSING))
    g = (
        g.with_edge(Edge(src="pkg:b", dst="pkg:a", relation=EdgeType.REQUIRES))
        .with_edge(Edge(src="pkg:c", dst="pkg:a", relation=EdgeType.REQUIRES))
        .with_edge(Edge(src="pkg:d", dst="pkg:b", relation=EdgeType.REQUIRES))
        .with_edge(Edge(src="pkg:d", dst="pkg:c", relation=EdgeType.REQUIRES))
    )
    front = [n.id for n in scheduler_frontier(g)]
    # all deps are MISSING so only the root is actionable this pass
    assert front == ["pkg:a"]


def test_service_nodes_excluded():
    g = DepGraph().with_node(
        _node("service:postgres", NodeType.SERVICE, "postgres", State.MISSING, layer=Layer.SERVICES)
    )
    assert scheduler_frontier(g) == ()


def test_config_nodes_excluded():
    """Config (tier 6) is advisory-only and never enters the executor frontier.

    Its `printenv X` check is structurally unsatisfiable in a fresh-shell exec, and
    a genuinely-required var is set reactively via the discover-task (tests certify
    SUFFICIENT). A CONFIG node with a real check_command — which would otherwise be
    actionable — must still be excluded. See unified-executor-loop-delta.md §9.
    """
    g = DepGraph().with_node(
        _node("config:POSTGRES_DB", NodeType.CONFIG, "POSTGRES_DB", State.MISSING,
              check="printenv POSTGRES_DB", layer=Layer.CONFIG)
    )
    assert scheduler_frontier(g) == ()


# ── Edit B: emittable nodes excluded from LLM frontier ───────────────────────

def test_emittable_node_excluded_from_frontier():
    """An emittable (reciped, deps-satisfied) MISSING node must NOT surface to the LLM.

    Rationale: emit-prefix path (Edit B).  The deterministic batch drain handles
    nodes where _is_emittable returns True; the LLM only receives the irreducible
    non-emittable residual.  See emit-prefix-plan.md Edit B.
    """
    # A PACKAGE node with a pinned version is emittable (_is_emittable → True).
    emittable_node = Node(
        id="pkg:requests",
        type=NodeType.PACKAGE,
        name="requests",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.STATIC_SCAN,
        state=State.MISSING,
        check_command="python -c 'import requests'",
        version="2.31.0",   # pinned version → _is_emittable returns True
    )
    g = DepGraph().with_node(emittable_node)
    # The emittable node must NOT appear in the scheduler frontier.
    assert scheduler_frontier(g) == (), (
        "emittable node (has pinned version → deterministic prefix can handle it) "
        "must be excluded from the LLM frontier"
    )


def test_non_emittable_missing_node_with_check_still_included():
    """A non-emittable MISSING node (no pinned version) with a check_command IS included.

    This is the residual the LLM needs to handle.
    """
    # PACKAGE without a version → _is_emittable returns False → LLM must handle it.
    non_emittable_node = Node(
        id="pkg:mystery",
        type=NodeType.PACKAGE,
        name="mystery",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.STATIC_SCAN,
        state=State.MISSING,
        check_command="python -c 'import mystery'",
        version=None,   # no pinned version → not emittable
    )
    g = DepGraph().with_node(non_emittable_node)
    front = scheduler_frontier(g)
    assert len(front) == 1 and front[0].id == "pkg:mystery", (
        "non-emittable MISSING node with check_command must remain in the LLM frontier"
    )
