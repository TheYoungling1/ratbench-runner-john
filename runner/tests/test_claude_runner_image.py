# runner/tests/test_claude_runner_image.py — the claude-runner preflight.
#
# claude-runner:latest is a LOCAL-ONLY image (no registry: `docker pull` fails), so any
# `docker system prune` silently removes it. Without it the ccdf producer's anti-vanish guard
# turns all 50 repos into status="error" — which reads on disk as a completed run scoring zero.
# The preflight converts that silent zero into a working run or one loud failure.
import os
import subprocess

import pytest

from runner.cli import _ensure_claude_runner


class _FakeRun:
    """Records docker invocations and replays canned return codes."""

    def __init__(self, *codes):
        self.codes = list(codes)
        self.calls = []
        self.kwargs = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.kwargs.append(kw)          # recorded so tests can assert HOW docker was invoked
        rc = self.codes.pop(0) if self.codes else 0
        return subprocess.CompletedProcess(argv, rc)


def test_noop_for_non_claude_model(tmp_path):
    fake = _FakeRun()
    _ensure_claude_runner("dockeragent", repo_root=str(tmp_path), runner=fake)
    assert fake.calls == []


def test_noop_when_image_already_present(tmp_path):
    fake = _FakeRun(0)                       # docker image inspect -> 0
    _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake)
    assert len(fake.calls) == 1
    assert fake.calls[0][:3] == ["docker", "image", "inspect"]


def test_builds_when_image_missing(tmp_path):
    fake = _FakeRun(1, 0)                    # inspect -> 1 (missing), build -> 0
    _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake)
    assert len(fake.calls) == 2
    build = fake.calls[1]
    assert build[:2] == ["docker", "build"]
    assert "claude-runner:latest" in build
    assert any(a.endswith("docker/claude-runner.Dockerfile") for a in build)
    # The build CONTEXT is the docker/ dir, not the repo root (the Dockerfile has no COPY, so a
    # whole-repo context would just ship megabytes to the daemon for nothing).
    assert build[-1] == os.path.join(str(tmp_path), "docker")


def test_build_output_is_not_captured_so_the_failure_is_actually_loud(tmp_path):
    # The whole point of the preflight is a LOUD failure. `inspect` is quiet (capture_output),
    # but the build must stream to the terminal — capturing it would silently defeat that while
    # still passing every other test here.
    fake = _FakeRun(1, 0)
    _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake)
    assert fake.kwargs[0].get("capture_output") is True     # inspect: quiet
    assert not fake.kwargs[1].get("capture_output")         # build: streams


def test_build_failure_aborts_the_run(tmp_path):
    # Loud beats silent: a failed build must stop the run BEFORE any repo produces, not let 50
    # repos each error out into a zero row.
    fake = _FakeRun(1, 1)                    # inspect -> missing, build -> fail
    with pytest.raises(SystemExit):
        _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake)


def test_also_guards_the_live_claudecode_lane(tmp_path):
    fake = _FakeRun(0)
    _ensure_claude_runner("claudecode", repo_root=str(tmp_path), runner=fake)
    assert len(fake.calls) == 1


def test_honors_claude_runner_image_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_RUNNER_IMAGE", "my-runner:v2")
    fake = _FakeRun(0)
    _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake)
    assert "my-runner:v2" in fake.calls[0]


def test_vendored_dockerfile_exists_and_installs_the_cli():
    # The recipe used to live only in the legacy /opt/harness tree; it must be in THIS repo so a
    # fresh box can build it.
    import os
    # runner/tests/<this file> -> runner/tests -> runner -> repo root
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "docker", "claude-runner.Dockerfile")
    text = open(path).read()
    assert "@anthropic-ai/claude-code" in text
    assert "useradd" in text        # bypassPermissions is refused as root
