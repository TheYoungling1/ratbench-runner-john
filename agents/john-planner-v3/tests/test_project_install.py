"""Tier-1 Fix 2: derive exactly one project install from achieved state."""
from src.envstate.ledger import ActionEvent, ActionLedger
from src.envstate.project_install import (
    name_in_closure,
    project_install_from_ledger,
    resolve_project_install,
)
from src.envstate.world_model import Fact


def _ev(cmd, rc=0, step=1):
    return ActionEvent(
        step=step, task_id=None, cmd=cmd, rc=rc, stdout="", stdout_path=None,
        stderr_path=None, env_revision_before=0, env_revision_after=0,
        mutation_class="language_package_install", container_id="c1", summary="",
    )


def test_name_in_closure_normalizes():
    inst = (Fact("My_Proj", "1.0"), Fact("requests", "2"))
    assert name_in_closure(inst, "my-proj")
    assert not name_in_closure(inst, "flask")
    assert not name_in_closure(inst, None)


def test_ledger_editable_detected():
    led = ActionLedger()
    led.append(_ev("pip install -e ."))
    assert project_install_from_ledger(led) == "pip install -e ."


def test_ledger_editable_with_extras_normalized():
    led = ActionLedger()
    led.append(_ev('pip install -e ".[test]"'))
    assert project_install_from_ledger(led) == "pip install -e ."


def test_ledger_editable_absolute_repo_root():
    led = ActionLedger()
    led.append(_ev("pip install -e /app"))
    assert project_install_from_ledger(led) == "pip install -e ."


def test_ledger_non_editable_local():
    led = ActionLedger()
    led.append(_ev("pip install ."))
    assert project_install_from_ledger(led) == "pip install ."


def test_ledger_ignores_pypi_and_requirements():
    led = ActionLedger()
    led.append(_ev("pip install requests"))
    led.append(_ev("pip install -r requirements.txt"))
    assert project_install_from_ledger(led) is None


def test_resolve_uses_ledger_editable_with_no_deps():
    # Non-empty closure (a real pinned dep) -> --no-deps is safe (deps are pinned).
    led = ActionLedger()
    led.append(_ev("pip install -e ."))
    out = resolve_project_install((Fact("requests", "2"),), "anything", ledger=led)
    assert out == "pip install -e . --no-deps"


def test_resolve_closure_default_editable(tmp_path):
    # Closure carries a real dependency besides the project -> --no-deps retained.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    out = resolve_project_install(
        (Fact("x", "0.1"), Fact("requests", "2")), "x",
        ledger=ActionLedger(), project_root=str(tmp_path),
    )
    assert out == "pip install -e . --no-deps"


def test_resolve_none_when_project_not_installed():
    out = resolve_project_install(
        (Fact("requests", "2"),), "myproj", ledger=ActionLedger(), project_root=None
    )
    assert out is None


def test_resolve_exactly_once_for_repeated_installs():
    led = ActionLedger()
    led.append(_ev("pip install -e ."))
    led.append(_ev("pip install -e ."))
    out = resolve_project_install((Fact("requests", "2"),), "x", ledger=led)
    assert out == "pip install -e . --no-deps"
    assert isinstance(out, str)


# --- MEDIUM 1: --no-deps is unsafe when the closure carries no dependencies ------


def test_resolve_omits_no_deps_on_empty_closure_ledger():
    """Empty closure (failed/empty pin probe): `pip install -e . --no-deps` would
    install the project with ZERO deps -> guaranteed ModuleNotFoundError. Omit
    --no-deps so normal resolution fills the deps."""
    led = ActionLedger()
    led.append(_ev("pip install -e ."))
    assert resolve_project_install((), "anything", ledger=led) == "pip install -e ."


def test_resolve_omits_no_deps_when_only_project_in_closure(tmp_path):
    """The closure contains only the project itself (no deps pinned) -> still unsafe."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    out = resolve_project_install(
        (Fact("x", "0.1"),), "x", ledger=ActionLedger(), project_root=str(tmp_path)
    )
    assert out == "pip install -e ."


# --- LOW: monorepo sub-dir editable install --------------------------------------


def test_ledger_subdir_editable_detected():
    led = ActionLedger()
    led.append(_ev("pip install -e packages/foo"))
    assert project_install_from_ledger(led) == "pip install -e packages/foo"


def test_ledger_subdir_non_editable_detected():
    led = ActionLedger()
    led.append(_ev("pip install packages/foo"))
    assert project_install_from_ledger(led) == "pip install packages/foo"


def test_ledger_bare_name_without_slash_is_not_local():
    led = ActionLedger()
    led.append(_ev("pip install -e somepkg"))   # no separator -> treated as PyPI name
    assert project_install_from_ledger(led) is None


# --- pyads swing-repo bug: prefer successful by-name install over fabricated editable ---


def test_resolve_prefers_successful_pypi_over_failed_editable(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'pyads'\n", encoding="utf-8")
    led = ActionLedger()
    led.append(_ev("pip install -e .", rc=1, step=10))    # FAILED editable (meson)
    led.append(_ev("pip install pyads", rc=0, step=15))   # SUCCESSFUL by-name PyPI
    out = resolve_project_install(
        (Fact("pyads", "3.6.0"), Fact("requests", "2")), "pyads",
        ledger=led, project_root=str(tmp_path),
    )
    assert out == "pip install pyads==3.6.0 --no-deps"


def test_resolve_does_not_overmatch_unrelated_failed_install(tmp_path):
    # A successful UNRELATED install (`bar`) must NOT trigger emitting the project pin or
    # otherwise change behavior: with no successful by-name install of `foo`, keep the
    # editable fallback.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'foo'\n", encoding="utf-8")
    led = ActionLedger()
    led.append(_ev("pip install -e .", rc=1, step=10))    # failed editable for foo
    led.append(_ev("pip install bar", rc=0, step=15))     # successful UNRELATED
    out = resolve_project_install(
        (Fact("foo", "1.0"), Fact("bar", "2")), "foo",
        ledger=led, project_root=str(tmp_path),
    )
    assert out == "pip install -e . --no-deps"
