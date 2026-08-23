from src.eval.graph_repair_ablation.oracle import (
    Injection, PILOT_INJECTIONS, FAILURE_CLASSES, select,
)

def test_five_pilot_injections_one_per_class():
    assert len(PILOT_INJECTIONS) == 5
    assert {i.failure_class for i in PILOT_INJECTIONS} == set(FAILURE_CLASSES)

def test_every_injection_is_wellformed():
    for i in PILOT_INJECTIONS:
        assert i.injection_id and i.repo and i.base_image
        assert i.mutation["op"] in {"strip_line", "add_install_pkg", "add_pin"}
        assert i.correct_action["kind"] in {"install", "drop", "repin"}
        assert i.correct_action["target"]

def test_select_by_class_and_id():
    assert len(select(classes={"SYSLIB_MISSING"})) == 1
    only = PILOT_INJECTIONS[0].injection_id
    assert [x.injection_id for x in select(only={only})] == [only]

def test_select_unknown_raises():
    import pytest
    with pytest.raises(ValueError):
        select(classes={"NOPE"})
