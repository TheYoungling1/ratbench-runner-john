from src.envstate.contracts.validation import validate_patch, MAX_PROMOTIONS_PER_CYCLE
from src.envstate.contracts.patch import GraphPatch
from src.envstate.contracts.graph import ContractGraph
from src.envstate.contracts.nodes import Node, Edge

KNOWN_CMDS = frozenset({"cmd:001"})


def _graph_with_goal():
    return ContractGraph(nodes=(Node("contract:goal:repo_imports_work", "Contract",
                                     {"level": "goal", "layer": "deps"}),))


def test_rejects_attempt_creation_by_maintainer():
    p = GraphPatch(add_contracts=(Node("attempt:x", "Attempt", {}),))
    errs = validate_patch(_graph_with_goal(), p, scope="maintainer", known_command_ids=KNOWN_CMDS)
    assert any("Attempt" in e for e in errs)


def test_rejects_blocker_without_command_evidence():
    p = GraphPatch(add_blockers=(Node("blocker:b", "Blocker",
        {"signature": "x", "active": True, "evidence_refs": ["cmd:999"]}),),
        add_edges=(Edge("blocker:b", "violates", "contract:goal:repo_imports_work"),))
    errs = validate_patch(_graph_with_goal(), p, scope="maintainer", known_command_ids=KNOWN_CMDS)
    assert any("evidence" in e.lower() for e in errs)


def test_rejects_orphan_atomic_contract():
    p = GraphPatch(add_contracts=(Node("contract:python_import:cv2", "Contract",
        {"level": "atomic", "layer": "deps"}),))  # no depends_on/violates linking it under backbone
    errs = validate_patch(_graph_with_goal(), p, scope="maintainer", known_command_ids=KNOWN_CMDS)
    assert any("backbone" in e.lower() or "orphan" in e.lower() for e in errs)


def test_rejects_too_many_promotions():
    contracts = tuple(Node(f"contract:python_import:p{i}", "Contract", {"level": "atomic", "layer": "deps"})
                      for i in range(MAX_PROMOTIONS_PER_CYCLE + 1))
    p = GraphPatch(add_contracts=contracts)
    errs = validate_patch(_graph_with_goal(), p, scope="maintainer", known_command_ids=KNOWN_CMDS)
    assert any("inventory" in e.lower() or "too many" in e.lower() for e in errs)


def test_valid_maintainer_patch_passes():
    p = GraphPatch(
        add_blockers=(Node("blocker:b", "Blocker",
            {"signature": "x", "active": True, "evidence_refs": ["cmd:001"]}),),
        add_edges=(Edge("blocker:b", "violates", "contract:goal:repo_imports_work"),))
    assert validate_patch(_graph_with_goal(), p, scope="maintainer", known_command_ids=KNOWN_CMDS) == []
