"""Characterization of the ddmin answer-key minimizer (answer_key.minimize).

The gate receives a frozenset; minimize returns the 1-minimal load-bearing
subset, or raises if the full superset is already insufficient.
"""
import pytest

from src.eval.package_installability.answer_key import minimize


def test_minimize_keeps_only_load_bearing():
    # gate passes iff "libB" is present; ddmin must reduce to exactly {libB}
    superset = ["libA", "libB", "libC"]
    gate = lambda subset: "libB" in subset
    assert set(minimize(superset, gate)) == {"libB"}


def test_minimize_empty_when_gate_always_true():
    assert minimize(["x", "y"], lambda s: True) == []


def test_minimize_raises_when_superset_insufficient():
    # full set is never green -> sufficiency check fails loudly (design §5 step 2)
    with pytest.raises(ValueError):
        minimize(["a", "b"], lambda s: False)
