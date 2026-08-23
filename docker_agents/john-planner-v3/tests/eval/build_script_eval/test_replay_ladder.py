import pytest
# Import replay first: it inserts the repo `src/` onto sys.path so the bare
# `python_deps` package (used below for CommandResult) resolves.
from src.eval.build_script_eval import replay
from src.eval.build_script_eval.replay import _disconnect_network_cmd, run_replay_ladder
from src.eval.language_package_eval.coverage import _docker_available
from python_deps.depgraph.executor import CommandResult


def test_disconnect_network_cmd_targets_bridge_and_container():
    cmd = _disconnect_network_cmd("probe-abc123")
    assert cmd[:3] == ["docker", "network", "disconnect"]
    assert "probe-abc123" in cmd


# --------------------------------------------------------------------------
# Docker-free fake-container harness: exercises the whole ladder deterministically
# (install / env_works / bootstrap / test rungs) without a real container.
# --------------------------------------------------------------------------

def _rc(returncode=0, stdout="", stderr=""):
    return CommandResult(command="<cmd>", returncode=returncode, stdout=stdout, stderr=stderr)


def _phase_for(command: str) -> str:
    # Most-specific substring first: the collect and suite runs both contain
    # "-m pytest", and the bootstrap command contains "pytest" too.
    if "bash -x /setup.sh" in command:
        return "install"
    if "pip install" in command:
        return "bootstrap"
    if "--collect-only" in command:
        return "collect"
    if "-m pytest" in command or "pytest -q" in command:
        return "test"
    if "import " in command:
        return "import"
    return "other"


class _FakeBox:
    """Stands in for coverage.py's _MountedContainer. `.run` returns a scripted
    CommandResult keyed by the command's phase; unscripted phases default to rc0."""

    def __init__(self, script: dict[str, CommandResult]):
        self._script = script
        self.name = "fake"
        self.container_dir = "/workspace/repo"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, command: str, *, timeout: int = 300) -> CommandResult:
        return self._script.get(_phase_for(command), _rc())


def _patch(monkeypatch, script):
    box = _FakeBox(script)
    monkeypatch.setattr(replay, "_MountedContainer", lambda *a, **k: box)
    monkeypatch.setattr(replay, "_write_file", lambda *a, **k: None)
    monkeypatch.setattr(replay.subprocess, "run", lambda *a, **k: None)
    return box


def test_install_failure_stops_at_none(monkeypatch):
    _patch(monkeypatch, {"install": _rc(1, stderr="+ pip install -e .\nERROR: build failed")})
    res = run_replay_ladder("/repo", "img", "setup", "triv")
    assert res.install_ok is False
    assert res.env_works is False
    assert res.highest_rung == "none"
    assert res.reason == "install_failed"
    assert res.first_failure is not None


def test_env_broken_from_import_only(monkeypatch):
    # import fails; gaps must derive from the IMPORT output only.
    _patch(monkeypatch, {
        "import": _rc(1, stderr="ModuleNotFoundError: No module named 'triv'"),
    })
    res = run_replay_ladder("/repo", "img", "setup", "triv")
    assert res.install_ok is True
    assert res.env_works is False
    assert res.reason == "env_broken"
    assert {(g["tier"], g["id"]) for g in res.gaps} == {("PACKAGE", "triv")}


def test_bootstrap_failure_never_manufactures_a_gap(monkeypatch):
    # import clean, but the pytest bootstrap itself fails ("pip: command not
    # found" would classify to TOOL:pip). env_works must be True (import passed,
    # collect skipped), test rung skipped, and NO gap manufactured from the
    # bootstrap output.
    _patch(monkeypatch, {
        "bootstrap": _rc(127, stderr="pip: command not found"),
    })
    res = run_replay_ladder("/repo", "img", "setup", "triv")
    assert res.install_ok is True
    assert res.env_works is True
    assert res.collect_ok is None
    assert res.tests_ran is False
    assert res.tests_passed is False
    assert res.reason == "pytest_unavailable"
    assert res.highest_rung == "env_works"
    assert res.gaps == ()
    assert res.first_failure is None


def test_tests_ran_but_failed(monkeypatch):
    _patch(monkeypatch, {
        "test": _rc(1, stdout="1 failed, 0 passed"),
    })
    res = run_replay_ladder("/repo", "img", "setup", "triv")
    assert res.install_ok is True
    assert res.env_works is True
    assert res.tests_ran is True
    assert res.tests_passed is False
    assert res.reason == "tests_failed"
    assert res.highest_rung == "tests_ran"


def test_full_ladder_all_green(monkeypatch):
    _patch(monkeypatch, {})  # every phase defaults to rc0
    res = run_replay_ladder("/repo", "img", "setup", "triv")
    assert res.install_ok is True
    assert res.env_works is True
    assert res.collect_ok is True
    assert res.tests_ran is True
    assert res.tests_passed is True
    assert res.highest_rung == "tests_passed"
    assert res.reason is None
    assert res.gaps == ()


def test_install_failure_includes_tool_gap_for_missing_gcc(monkeypatch):
    _patch(monkeypatch, {"install": _rc(
        1, stderr="unable to execute 'gcc': No such file or directory\n"
                  "error: command 'gcc' failed: No such file or directory\n"
                  "[notice] A new release of pip is available: 23.0 -> 24.0\n"
                  "[notice] To update, run: pip install --upgrade pip\n",
    )})
    res = run_replay_ladder("/repo", "img", "setup", "triv")
    assert res.install_ok is False
    assert {(g["tier"], g["id"]) for g in res.gaps} == {("TOOL", "gcc")}
    assert res.first_failure["command"] is not None
    assert "[notice]" not in res.first_failure["command"]


def test_vacuous_env_works_guard_no_import_no_collect(monkeypatch):
    # bootstrap fails AND no top_import was given ⇒ nothing was ever verified
    # (no import check ran, collect was skipped): env_works must be False, not
    # vacuously True.
    _patch(monkeypatch, {"bootstrap": _rc(127, stderr="pip: command not found")})
    res = run_replay_ladder("/repo", "img", "setup", None)
    assert res.install_ok is True
    assert res.env_works is False
    assert res.highest_rung == "install"
    assert res.reason == "unverified_no_import_no_collect"
    assert res.gaps == ()
    assert res.first_failure is None


def test_bootstrap_failure_with_import_ok_keeps_env_works_true(monkeypatch):
    # bootstrap fails but top_import WAS given and the import check passed
    # (unscripted "import" phase defaults to rc0) ⇒ import ran and verified
    # the env, so env_works stays True (unchanged behavior).
    _patch(monkeypatch, {"bootstrap": _rc(127, stderr="pip: command not found")})
    res = run_replay_ladder("/repo", "img", "setup", "triv")
    assert res.install_ok is True
    assert res.env_works is True
    assert res.highest_rung == "env_works"
    assert res.reason == "pytest_unavailable"


@pytest.mark.skipif(not _docker_available(), reason="docker unavailable")
def test_ladder_on_trivial_pure_python_repo(tmp_path):
    # a repo that installs cleanly, imports, and has one passing test
    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\nrequires=['setuptools']\nbuild-backend='setuptools.build_meta'\n"
        "[project]\nname='triv'\nversion='0.0.0'\n"
    )
    (tmp_path / "triv").mkdir()
    (tmp_path / "triv" / "__init__.py").write_text("x = 1\n")
    (tmp_path / "test_triv.py").write_text("from triv import x\n\ndef test_x():\n    assert x == 1\n")
    setup_sh = "#!/usr/bin/env bash\nset -e\npip install -e .\n"
    res = run_replay_ladder(str(tmp_path), "python:3.11-slim", setup_sh, "triv", test_timeout=180)
    assert res.install_ok is True
    assert res.env_works is True
    assert res.tests_ran is True
    assert res.tests_passed is True
    assert res.highest_rung == "tests_passed"


def test_collect_framework_error_keeps_env_works_true(monkeypatch):
    # import clean, bootstrap clean, but --collect-only errors on a pytest
    # framework/config incompatibility (no real missing dependency). env_works
    # must stay True; only collect_ok flips to False.
    for stderr in (
        "tests/test_x.py: PytestRemovedIn10Warning: nose-style ...\n"
        "filterwarnings = error -> collection ERROR",
        "ImportError: cannot import name 'notset' from '_pytest.config'",
    ):
        _patch(monkeypatch, {"collect": _rc(1, stderr=stderr)})
        res = run_replay_ladder("/repo", "img", "setup", "triv")
        assert res.install_ok is True
        assert res.env_works is True
        assert res.collect_ok is False
        assert res.highest_rung == "env_works"
        assert res.reason == "collect_incompatible"
        assert res.gaps == ()
        assert res.first_failure is not None


def test_collect_real_module_gap_fails_env_works(monkeypatch):
    # a genuine missing dependency during collection IS an env gap.
    _patch(monkeypatch, {"collect": _rc(
        1, stderr="ModuleNotFoundError: No module named 'pytest_asyncio'")})
    res = run_replay_ladder("/repo", "img", "setup", "triv")
    assert res.install_ok is True
    assert res.env_works is False
    assert res.collect_ok is False
    assert res.reason == "env_broken"
    assert res.highest_rung == "install"
    assert {(g["tier"], g["id"]) for g in res.gaps} == {("PACKAGE", "pytest_asyncio")}
