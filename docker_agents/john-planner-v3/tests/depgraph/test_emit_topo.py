from python_deps.depgraph.emit import topo_order
from python_deps.depgraph.schema import (
    DepGraph, Edge, EdgeType, Layer, Node, NodeType, State, DiscoveredBy,
)


def _n(nid, name, layer, ntype=NodeType.PACKAGE):
    return Node(id=nid, type=ntype, name=name, layer=layer,
                discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING, version="1.0")


def test_topo_dependency_before_dependent():
    a = _n("pkg:a", "a", Layer.PIP)
    b = _n("pkg:b", "b", Layer.PIP)
    # a requires b  =>  b must come before a
    g = DepGraph(nodes=(a, b),
                 edges=(Edge(src="pkg:a", dst="pkg:b", relation=EdgeType.REQUIRES),))
    order = [n.name for n in topo_order(g, (a, b))]
    assert order.index("b") < order.index("a")


def test_topo_layer_rank_tiebreak():
    tool = _n("tool:gcc", "gcc", Layer.TOOLCHAIN, NodeType.TOOL)
    pkg = _n("pkg:z", "z", Layer.PIP)
    g = DepGraph(nodes=(pkg, tool))  # no edges -> pure layer-rank order
    order = [n.name for n in topo_order(g, (pkg, tool))]
    assert order == ["gcc", "z"]  # TOOLCHAIN(2) before PIP(3)


def test_topo_cycle_is_deterministic_not_crash():
    a = _n("pkg:a", "a", Layer.PIP)
    b = _n("pkg:b", "b", Layer.PIP)
    g = DepGraph(nodes=(a, b), edges=(
        Edge(src="pkg:a", dst="pkg:b", relation=EdgeType.REQUIRES),
        Edge(src="pkg:b", dst="pkg:a", relation=EdgeType.REQUIRES),
    ))
    order = [n.name for n in topo_order(g, (a, b))]
    assert sorted(order) == ["a", "b"]  # all present, no crash
