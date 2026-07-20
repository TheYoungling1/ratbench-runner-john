import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Layer, Node, NodeType, State,
)
from src.envstate.gates import (
    GateResult, evaluate_installability_gate, evaluate_gates,
)


def _syslib(state: State) -> Node:
    return Node(
        id="syslib:libfoo.so",
        type=NodeType.SYSTEM_LIB,
        name="libfoo.so",
        layer=Layer.SYSTEM,
        discovered_by=DiscoveredBy.RESOLVER,
        state=state,
        check_command="ldconfig -p | grep -q libfoo",
        chosen_fix="apt:libfoo-dev",
    )


def test_installability_passed_when_all_installable_satisfied():
    g = DepGraph().with_node(_syslib(State.SATISFIED))
    r = evaluate_installability_gate(g)
    assert isinstance(r, GateResult)
    assert r.name == "installability"
    assert r.passed is True
    assert r.provisional is True


def test_installability_failed_when_installable_missing():
    g = DepGraph().with_node(_syslib(State.MISSING))
    r = evaluate_installability_gate(g)
    assert r.passed is False
    assert r.provisional is True
    assert "syslib:libfoo.so" in r.evidence


def test_installability_none_graph_is_failed_not_crash():
    r = evaluate_installability_gate(None)
    assert r.passed is False
    assert r.provisional is True


def test_installability_evidence_truncated_to_cap():
    g = DepGraph()
    for i in range(400):
        g = g.with_node(Node(
            id=f"syslib:lib{i}.so", type=NodeType.SYSTEM_LIB, name=f"lib{i}.so",
            layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER,
            state=State.MISSING, check_command="x", chosen_fix="apt:lib",
        ))
    r = evaluate_installability_gate(g)
    assert r.passed is False
    assert len(r.evidence) <= 500


def test_evaluate_gates_returns_installability_then_testability():
    g = DepGraph().with_node(_syslib(State.SATISFIED))
    gates = evaluate_gates(g, lambda: False)
    assert len(gates) == 2
    assert gates[0].name == "installability"
    assert gates[1].name == "testability"
    assert gates[0].passed is True      # installable satisfied
    assert gates[1].passed is False     # tests not passing
