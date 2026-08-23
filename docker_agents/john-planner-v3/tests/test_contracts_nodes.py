# tests/test_contracts_nodes.py
from src.envstate.contracts.nodes import Node, Edge, node_to_dict, node_from_dict, edge_to_dict
from src.envstate.contracts import nodes as nodes_mod


def test_node_roundtrip_flattens_data():
    n = Node("contract:python_import:cv2", "Contract",
             {"level": "atomic", "kind": "python_import", "subject": "cv2"})
    d = node_to_dict(n)
    assert d["id"] == "contract:python_import:cv2" and d["kind"] == "python_import"
    assert node_from_dict(d) == n


def test_contract_status_event_removed():
    assert not hasattr(nodes_mod, "ContractStatusEvent")
