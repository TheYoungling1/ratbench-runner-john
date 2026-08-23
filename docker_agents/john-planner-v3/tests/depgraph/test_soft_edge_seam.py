from python_deps.depgraph.schema import (
    DepGraph, Node, Edge, NodeType, Layer, State, DiscoveredBy, EdgeType,
)
from python_deps.depgraph.schedule import _dependencies_satisfied


def _two_nodes():
    dependent = Node(id="pkg:app", type=NodeType.PACKAGE, name="app", layer=Layer.PIP,
                     discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING)
    dep = Node(id="config:DATABASE_URL", type=NodeType.CONFIG, name="DATABASE_URL",
               layer=Layer.CONFIG, discovered_by=DiscoveredBy.STATIC_SCAN, state=State.MISSING)
    return dependent, dep


def test_hard_unsatisfied_dep_blocks():
    dependent, dep = _two_nodes()
    g = DepGraph().with_node(dependent).with_node(dep).with_edge(
        Edge(src="pkg:app", dst="config:DATABASE_URL", relation=EdgeType.REQUIRES))
    assert _dependencies_satisfied(g, dependent) is False


def test_soft_unsatisfied_dep_does_not_block():
    dependent, dep = _two_nodes()
    g = DepGraph().with_node(dependent).with_node(dep).with_edge(
        Edge(src="pkg:app", dst="config:DATABASE_URL", relation=EdgeType.REQUIRES,
             data={"hard": False}))
    assert _dependencies_satisfied(g, dependent) is True
