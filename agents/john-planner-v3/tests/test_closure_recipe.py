"""Tier-1 Fix 2: closure-authoritative recipe = pinned closure + ONE project install."""
from src.envstate.ledger import ActionEvent, ActionLedger
from src.envstate.recipe import build_closure_recipe
from src.envstate.world_model import Fact


def _ev(cmd, rc=0, step=1):
    return ActionEvent(
        step=step, task_id=None, cmd=cmd, rc=rc, stdout="", stdout_path=None,
        stderr_path=None, env_revision_before=0, env_revision_after=0,
        mutation_class="language_package_install", container_id="c1", summary="",
    )


def test_pin_then_single_project_install(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup; setup(name='x')")
    inst = (Fact("requests", "2.31.0"), Fact("x", "0.1.0"))
    led = ActionLedger()
    led.append(_ev("pip install -e ."))
    cmds = build_closure_recipe(inst, project_name="x", ledger=led, project_root=str(tmp_path))

    assert any("requests==2.31.0" in c for c in cmds)          # closure pinned
    assert not any("x==0.1.0" in c for c in cmds)              # project excluded from pin
    assert cmds[-1] == "pip install -e . --no-deps"           # one project install, last
    assert sum(1 for c in cmds if c.strip().startswith("pip install -e")) == 1


def test_no_project_install_when_not_installed():
    inst = (Fact("requests", "2.31.0"),)
    cmds = build_closure_recipe(
        inst, project_name="myproj", ledger=ActionLedger(), project_root=None
    )
    assert all("-e ." not in c for c in cmds)
    assert any("requests==2.31.0" in c for c in cmds)


def test_empty_closure_no_project_returns_empty():
    assert build_closure_recipe((), project_name=None, ledger=ActionLedger()) == []
