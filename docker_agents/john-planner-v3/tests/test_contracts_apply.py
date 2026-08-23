# tests/test_contracts_apply.py
from src.envstate.contracts.apply import apply_patch
from src.envstate.contracts.patch import GraphPatch
from src.envstate.contracts.graph import ContractGraph
from src.envstate.contracts.nodes import Node, Edge


def test_apply_adds_and_caps_notes():
    g = ContractGraph(diagnostic_notes=tuple(str(i) for i in range(9)))
    p = GraphPatch(add_contracts=(Node("contract:python_import:cv2", "Contract", {"level": "atomic"}),),
                   diagnostic_notes=("new1", "new2"))
    g2 = apply_patch(g, p)
    assert g2.has_node("contract:python_import:cv2")
    assert len(g2.diagnostic_notes) == 10 and g2.diagnostic_notes[-1] == "new2"   # capped, newest kept


def test_update_blocker_classification_merges_fields():
    g = ContractGraph(nodes=(Node("blocker:b", "Blocker",
        {"root_or_downstream": "unknown", "active": True, "summary": "old"}),))
    p = GraphPatch(update_blocker_classification=({"blocker_id": "blocker:b",
                                                   "root_or_downstream": "root", "summary": "new"},))
    g2 = apply_patch(g, p)
    b = g2.node("blocker:b")
    assert b.data["root_or_downstream"] == "root" and b.data["summary"] == "new" and b.data["active"] is True


def test_update_blockers_replaces_node():
    import dataclasses
    g = ContractGraph(nodes=(Node("blocker:b", "Blocker", {"active": True}),))
    replaced = dataclasses.replace(g.node("blocker:b"), data={"active": False})
    g2 = apply_patch(g, GraphPatch(update_blockers=(replaced,)))
    assert g2.node("blocker:b").data["active"] is False
