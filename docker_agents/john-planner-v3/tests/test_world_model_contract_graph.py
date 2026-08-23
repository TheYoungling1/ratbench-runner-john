from src.envstate.contracts.graph import ContractGraph
from src.envstate.contracts.nodes import Node
from src.envstate.world_model import initial_map, map_from_dict, map_to_dict, merge_map


def test_initial_map_has_empty_graph():
    m = initial_map("img", "/repo", "python 3.12", "pip", ("pyproject.toml",))
    assert isinstance(m.contract_graph, ContractGraph)
    assert m.contract_graph.nodes == ()


def test_merge_map_threads_graph_immutably():
    m = initial_map("img", "/repo", "python", "pip", ())
    g = ContractGraph(nodes=(Node("contract:a", "Contract", {}),))
    m2 = merge_map(m, contract_graph=g)
    assert m.contract_graph.nodes == ()  # original unchanged
    assert m2.contract_graph is g


def test_graph_survives_serialization_roundtrip():
    m = merge_map(
        initial_map("img", "/repo", "python", "pip", ()),
        contract_graph=ContractGraph(nodes=(Node("contract:a", "Contract", {"level": "atomic"}),)),
    )
    back = map_from_dict(map_to_dict(m))
    assert back.contract_graph.node("contract:a").data["level"] == "atomic"


def test_old_serialized_map_without_graph_still_loads():
    d = map_to_dict(initial_map("img", "/repo", "python", "pip", ()))
    d.pop("contract_graph")  # simulate a pre-graph serialized map
    back = map_from_dict(d)
    assert back.contract_graph.nodes == ()
