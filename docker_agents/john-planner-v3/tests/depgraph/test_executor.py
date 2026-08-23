"""Task 4 — Executor: CommandResult, LocalSubprocessExecutor, FakeExecutor."""

from __future__ import annotations

import sys

from conftest import FakeExecutor, make_result

import python_deps.depgraph.executor as executor_mod
from python_deps.depgraph.executor import (
    CommandResult,
    DockerExecutor,
    Executor,
    LocalSubprocessExecutor,
)


def test_command_result_ok_property():
    assert CommandResult("c", 0, "", "").ok is True
    assert CommandResult("c", 3, "", "boom").ok is False


def test_local_executor_runs_real_command():
    ex = LocalSubprocessExecutor()
    res = ex.run(f'{sys.executable} -c "print(1)"')
    assert res.ok
    assert res.returncode == 0
    assert res.stdout.strip() == "1"


def test_local_executor_captures_nonzero_and_stderr():
    ex = LocalSubprocessExecutor()
    res = ex.run(
        f'{sys.executable} -c "import sys; sys.stderr.write(\'boom\'); sys.exit(3)"'
    )
    assert not res.ok
    assert res.returncode == 3
    assert "boom" in res.stderr


def test_local_executor_satisfies_protocol():
    assert isinstance(LocalSubprocessExecutor(), Executor)


def test_local_executor_timeout_returns_nonzero():
    ex = LocalSubprocessExecutor()
    res = ex.run(f'{sys.executable} -c "import time; time.sleep(5)"', timeout=1)
    assert not res.ok
    assert "timeout" in res.stderr.lower()


def test_fake_executor_substring_longest_match_wins():
    fake = FakeExecutor(
        responses={
            "pip": make_result(stdout="generic-pip"),
            "pip install numpy": make_result(stdout="numpy-specific"),
        }
    )
    res = fake.run("python -m pip install numpy==1.26.4")
    assert res.stdout == "numpy-specific"  # longest matching key wins


def test_fake_executor_default_fallback():
    fake = FakeExecutor(default=make_result(stdout="fallback", returncode=0))
    res = fake.run("anything at all")
    assert res.stdout == "fallback"


def test_fake_executor_no_match_returns_127():
    fake = FakeExecutor()
    res = fake.run("uv pip compile")
    assert res.returncode == 127
    assert res.stderr == "no fake response"


def test_fake_executor_records_calls():
    fake = FakeExecutor()
    fake.run("a")
    fake.run("b")
    assert fake.calls == ["a", "b"]


def test_fake_executor_satisfies_protocol():
    assert isinstance(FakeExecutor(), Executor)


# --- DockerExecutor --platform param (additive; default None = unchanged) ---


def test_docker_run_command_includes_platform_when_set():
    ex = DockerExecutor("python:3.11-slim-bookworm", platform="linux/amd64")
    cmd = ex._run_command()
    assert "--platform linux/amd64" in cmd


def test_docker_run_command_omits_platform_by_default():
    ex = DockerExecutor("python:3.11-slim-bookworm")
    cmd = ex._run_command()
    assert "--platform" not in cmd
    # Byte-identical to the historical (pre-platform) docker run command.
    assert cmd == (
        f"docker run -d --name {ex._name} python:3.11-slim-bookworm sleep infinity"
    )


def test_platform_flag_reaches_docker_run_argv(monkeypatch):
    captured: list[str] = []

    def fake_run(command: str, *, timeout: int) -> CommandResult:
        captured.append(command)
        return make_result(stdout="fakecontainerid\n", returncode=0)

    monkeypatch.setattr(executor_mod, "_run_subprocess", fake_run)
    with DockerExecutor("python:3.11-slim-bookworm", platform="linux/amd64"):
        pass
    run_argv = captured[0]  # first _run_subprocess call is the `docker run`
    assert "--platform" in run_argv
    assert "linux/amd64" in run_argv


def test_platform_flag_absent_from_docker_run_argv_when_none(monkeypatch):
    captured: list[str] = []

    def fake_run(command: str, *, timeout: int) -> CommandResult:
        captured.append(command)
        return make_result(stdout="fakecontainerid\n", returncode=0)

    monkeypatch.setattr(executor_mod, "_run_subprocess", fake_run)
    with DockerExecutor("python:3.11-slim-bookworm"):
        pass
    run_argv = captured[0]
    assert "--platform" not in run_argv
