import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.envstate.gates import GateResult, evaluate_testability_gate
from src.envstate.constants import VERIFY_TEST_CMD


def test_testability_passed_when_callable_true():
    r = evaluate_testability_gate(lambda: True)
    assert isinstance(r, GateResult)
    assert r.name == "testability"
    assert r.passed is True
    assert r.command == VERIFY_TEST_CMD
    assert r.provisional is False


def test_testability_failed_when_callable_false():
    r = evaluate_testability_gate(lambda: False)
    assert r.name == "testability"
    assert r.passed is False
    assert r.provisional is False


def test_testability_calls_callable_exactly_once():
    calls = []
    evaluate_testability_gate(lambda: calls.append(1) or True)
    assert calls == [1]


def test_gate_result_is_frozen():
    import dataclasses
    r = evaluate_testability_gate(lambda: True)
    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        r.passed = False  # type: ignore[misc]
