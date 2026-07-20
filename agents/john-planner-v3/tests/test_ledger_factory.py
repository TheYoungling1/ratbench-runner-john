from src.envstate.ledger import ActionEvent, make_action_event


def test_make_action_event_maps_success_to_rc0():
    ev = make_action_event(step=3, cmd="python -m pytest -q", success=True, stdout="2 passed",
                           env_revision_before=4, env_revision_after=4, mutation_class=None, container_id="c1")
    assert isinstance(ev, ActionEvent)
    assert ev.rc == 0 and ev.cmd == "python -m pytest -q" and ev.env_revision_after == 4


def test_make_action_event_failure_rc1():
    ev = make_action_event(step=1, cmd="x", success=False, stdout="boom",
                           env_revision_before=0, env_revision_after=0, mutation_class=None, container_id="")
    assert ev.rc == 1
