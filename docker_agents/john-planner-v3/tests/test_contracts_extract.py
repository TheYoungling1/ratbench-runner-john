# tests/test_contracts_extract.py
from src.envstate.contracts.extract import extract_blocker_subject, promote_atomic_contracts
from src.envstate.contracts.graph import ContractGraph


def test_extract_module_not_found():
    assert extract_blocker_subject("ModuleNotFoundError: No module named 'yaml'") == ("yaml", "module_not_found")


def test_extract_missing_binary():
    assert extract_blocker_subject("pg_config: command not found") == ("pg_config", "missing_binary")
    assert extract_blocker_subject("pg_config executable not found") == ("pg_config", "missing_binary")


def test_extract_missing_system_library():
    subj, kind = extract_blocker_subject("ImportError: libGL.so.1: cannot open shared object file")
    assert subj == "libGL.so.1" and kind == "missing_system_library"


def test_extract_unknown_returns_none():
    assert extract_blocker_subject("some unrelated text") == (None, "unknown")


def test_promote_creates_contract_for_module_not_found():
    nodes = promote_atomic_contracts(ContractGraph.empty(),
                                     ["ModuleNotFoundError: No module named 'yaml'"])
    assert any(n.id == "contract:python_import:yaml" for n in nodes)


def test_promote_is_idempotent_against_existing():
    from src.envstate.contracts.nodes import Node
    g = ContractGraph(nodes=(Node("contract:python_import:yaml", "Contract", {"level": "atomic"}),))
    assert promote_atomic_contracts(g, ["ModuleNotFoundError: No module named 'yaml'"]) == []
