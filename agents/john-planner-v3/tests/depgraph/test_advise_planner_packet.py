# tests/depgraph/test_advise_planner_packet.py
from python_deps.depgraph.advise import render_depgraph_planner
from python_deps.depgraph.schema import (
    DepGraph, Edge, EdgeType, Layer, Node, NodeType, State, DiscoveredBy, Attempt,
)


def test_packet_has_chain_attempts_and_conflict():
    goal = Node(id="test:goal", type=NodeType.TEST, name="repo_tests_pass",
                layer=Layer.TESTS, discovered_by=DiscoveredBy.GOAL, state=State.MISSING)
    proj = Node(id="proj:app", type=NodeType.PROJECT, name="app", layer=Layer.PIP,
                discovered_by=DiscoveredBy.GOAL, state=State.MISSING)
    lxml = Node(id="pkg:lxml", type=NodeType.PACKAGE, name="lxml", layer=Layer.PIP,
                discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING, version=None,
                attempts=(Attempt(command="pip install lxml", outcome="failed", cycle=2),))
    g = DepGraph(
        nodes=(goal, proj, lxml),
        edges=(
            Edge(src="test:goal", dst="proj:app", relation=EdgeType.REQUIRES),
            Edge(src="proj:app", dst="pkg:lxml", relation=EdgeType.REQUIRES),
        ),
    )
    out = render_depgraph_planner(g)
    assert "FRONTIER" in out
    assert "lxml" in out
    assert "chain: lxml <- app <- repo_tests_pass" in out
    assert "pip install lxml -> failed" in out


def test_packet_conflict_bounds_rendered():
    a = Node(id="pkg:fastavro", type=NodeType.PACKAGE, name="fastavro", layer=Layer.PIP,
             discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING, version=None)
    b = Node(id="pkg:avro", type=NodeType.PACKAGE, name="avro", layer=Layer.PIP,
             discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING, version=None)
    g = DepGraph(nodes=(a, b), edges=(
        Edge(src="pkg:fastavro", dst="pkg:avro", relation=EdgeType.CONFLICTS_WITH,
             data={"summary": "fastavro needs X>=2, avro needs X<2"}),
    ))
    out = render_depgraph_planner(g)
    assert "conflict" in out.lower()
    assert "fastavro needs X>=2, avro needs X<2" in out


def test_certified_collapses_to_counts_and_empty_graph_blank():
    sat = Node(id="pkg:flask", type=NodeType.PACKAGE, name="flask", layer=Layer.PIP,
               discovered_by=DiscoveredBy.RESOLVER, state=State.SATISFIED, version="3.0.0")
    out = render_depgraph_planner(DepGraph(nodes=(sat,)))
    assert "CERTIFIED" in out and "pip 1" in out
    assert "flask" not in out          # certified nodes are counts, not lines
    assert render_depgraph_planner(DepGraph()) == ""


def test_frontier_excludes_non_installable_imports():
    """R1 (Task 6): a MISSING Import node must NOT appear in the rendered FRONTIER."""
    imp = Node(id="imp:foo", type=NodeType.IMPORT, name="foo", layer=Layer.NAMING,
               discovered_by=DiscoveredBy.STATIC_SCAN, state=State.MISSING)
    g = DepGraph(nodes=(imp,))
    out = render_depgraph_planner(g)
    # IMPORT is non-installable; partition().frontier excludes it -> not in FRONTIER
    assert "FRONTIER" not in out
    assert "foo" not in out


def test_runtime_config_and_service_nodes_surface_in_planner():
    """C1: RUNTIME CONFIG + SERVICE nodes must appear in render_depgraph_planner output."""
    test_node = Node(
        id="test:goal", type=NodeType.TEST, name="repo_tests_pass",
        layer=Layer.TESTS, discovered_by=DiscoveredBy.GOAL, state=State.MISSING,
    )
    config_node = Node(
        id="config:DATABASE_URL", type=NodeType.CONFIG, name="DATABASE_URL",
        layer=Layer.CONFIG, discovered_by=DiscoveredBy.RUNTIME, state=State.UNKNOWN,
        evidence="KeyError: 'DATABASE_URL'",
    )
    service_node = Node(
        id="service:postgres", type=NodeType.SERVICE, name="postgres",
        layer=Layer.CONFIG, discovered_by=DiscoveredBy.RUNTIME, state=State.UNKNOWN,
        evidence="psycopg2.OperationalError: could not connect to server",
    )
    g = DepGraph(nodes=(test_node, config_node, service_node))
    out = render_depgraph_planner(g)
    assert "RUNTIME-DISCOVERED" in out
    assert "DATABASE_URL" in out
    assert "postgres" in out
