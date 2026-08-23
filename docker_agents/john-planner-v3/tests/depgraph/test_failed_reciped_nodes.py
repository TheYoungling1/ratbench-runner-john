import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph, Node, Edge, NodeType, Layer, State, EdgeType, DiscoveredBy,
)
from python_deps.depgraph.emit import failed_reciped_nodes  # noqa: E402


def _pkg(nid, name, state, *, version="1.0", check="true"):
    return Node(id=nid, type=NodeType.PACKAGE, name=name, layer=Layer.PIP,
                discovered_by=DiscoveredBy.STATIC_SCAN, state=state,
                check_command=check, version=version)


def test_missing_reciped_node_is_a_culprit():
    g = DepGraph().with_node(_pkg("pkg:a", "a", State.MISSING))
    assert [n.id for n in failed_reciped_nodes(g)] == ["pkg:a"]


def test_satisfied_node_is_not_a_culprit():
    g = DepGraph().with_node(_pkg("pkg:a", "a", State.SATISFIED))
    assert failed_reciped_nodes(g) == ()


def test_node_without_check_is_excluded():
    g = DepGraph().with_node(_pkg("pkg:a", "a", State.MISSING, check=None))
    assert failed_reciped_nodes(g) == ()


def test_config_node_is_never_a_culprit():
    cfg = Node(id="config:X", type=NodeType.CONFIG, name="X", layer=Layer.CONFIG,
               discovered_by=DiscoveredBy.STATIC_SCAN, state=State.MISSING,
               check_command="printenv X")
    assert failed_reciped_nodes(DepGraph().with_node(cfg)) == ()


def test_node_with_unsatisfied_dep_is_held_back():
    g = (DepGraph()
         .with_node(_pkg("pkg:dep", "dep", State.MISSING))
         .with_node(_pkg("pkg:app", "app", State.MISSING))
         .with_edge(Edge(src="pkg:app", dst="pkg:dep", relation=EdgeType.REQUIRES)))
    assert [n.id for n in failed_reciped_nodes(g)] == ["pkg:dep"]
