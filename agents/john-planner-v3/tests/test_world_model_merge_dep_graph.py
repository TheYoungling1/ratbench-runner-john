from src.envstate.world_model import merge_map, initial_map
from python_deps.depgraph.schema import DepGraph, Node, NodeType, Layer, State, DiscoveredBy


def _graph(state):
    return DepGraph(nodes=(Node(id="pkg:flask", type=NodeType.PACKAGE, name="flask",
                                layer=Layer.PIP, discovered_by=DiscoveredBy.RESOLVER,
                                state=state, version="3.0.0"),))


def test_merge_map_replaces_dep_graph_when_passed():
    m = initial_map("img", "/app", "python 3.12", "pip", (), dep_graph=_graph(State.MISSING))
    m2 = merge_map(m, dep_graph=_graph(State.SATISFIED))
    assert m2.dep_graph.get("pkg:flask").state is State.SATISFIED
    assert m.dep_graph.get("pkg:flask").state is State.MISSING  # original untouched


def test_merge_map_leaves_dep_graph_when_omitted():
    g = _graph(State.MISSING)
    m = initial_map("img", "/app", "python 3.12", "pip", (), dep_graph=g)
    m2 = merge_map(m, done_flag=True)
    assert m2.dep_graph is g
