# tests/test_contracts_schema.py
from src.envstate.contracts import schema

def test_three_node_three_edge_vocabulary():
    assert {nt.value for nt in schema.NodeType} == {"Contract", "Blocker", "Attempt"}
    assert {et.value for et in schema.EdgeType} == {"violates", "addresses", "depends_on"}
    assert {s.value for s in schema.ContractStatus} == {"satisfied", "violated", "unknown"}

def test_edge_rules_are_three_rows_typed():
    assert set(schema.EDGE_RULES) == {"violates", "addresses", "depends_on"}
    assert schema.EDGE_RULES["violates"] == (frozenset({"Blocker"}), frozenset({"Contract"}))
    assert schema.EDGE_RULES["addresses"] == (frozenset({"Attempt"}), frozenset({"Contract"}))
    assert schema.EDGE_RULES["depends_on"] == (frozenset({"Contract"}), frozenset({"Contract"}))

def test_ownership_constants_and_forbidden_fields():
    assert schema.MAINTAINER_CREATABLE_NODE_TYPES == frozenset({"Contract", "Blocker"})
    assert schema.MAINTAINER_FORBIDDEN_FIELDS == frozenset({"status", "outcome", "active"})

def test_blocker_attempt_enums_present():
    assert "missing_system_library" in {k.value for k in schema.BlockerKind}
    assert "ok_but_still_blocked" in {o.value for o in schema.AttemptOutcome}

def test_redact_secrets_kept():
    assert schema.redact_secrets("API_KEY=sk-abcdefgh12345678") != "API_KEY=sk-abcdefgh12345678"
