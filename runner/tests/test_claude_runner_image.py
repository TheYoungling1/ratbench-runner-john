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


def _write_dataset(tmp_path, languages):
    import json
    p = tmp_path / "ds.json"
    p.write_text(json.dumps([{"full_name": f"o/r{i}", "language": lang}
                             for i, lang in enumerate(languages)]))
    return str(p)


def test_builds_only_the_workbench_the_dataset_needs(tmp_path):
    # A Rust dataset must not build the python/node workbench, and vice versa: each build is ~1GB
    # and several minutes.
    fake = _FakeRun(1, 0)                    # inspect -> missing, build -> ok
    ds = _write_dataset(tmp_path, ["Rust", "Rust"])
    _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake,
                          repos_json=ds)
    tags = [c[3] for c in fake.calls if c[:3] == ["docker", "image", "inspect"]]
    assert tags == ["claude-runner-rust:latest"]
    build = [c for c in fake.calls if c[1] == "build"][0]
    assert build[3] == "claude-runner-rust:latest"
    assert build[5].endswith("claude-runner-rust.Dockerfile")


def test_mixed_language_dataset_builds_every_needed_workbench(tmp_path):
    fake = _FakeRun(1, 0, 1, 0)              # two (inspect-missing, build-ok) pairs
    ds = _write_dataset(tmp_path, ["Java", "Python"])
    _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake,
                          repos_json=ds)
    tags = sorted(c[3] for c in fake.calls if c[:3] == ["docker", "image", "inspect"])
    assert tags == ["claude-runner-java:latest", "claude-runner:latest"]


def test_no_dataset_keeps_the_original_single_workbench(tmp_path):
    # Tier/variety runs pass no --repos-json. Those are Python, and must behave exactly as before.
    fake = _FakeRun(0)
    _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake,
                          repos_json=None)
    assert len(fake.calls) == 1
    assert fake.calls[0][3] == "claude-runner:latest"


def test_unreadable_dataset_falls_back_to_the_default_workbench(tmp_path):
    # A malformed dataset must not abort the run before it starts; the default is the safe guess.
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    fake = _FakeRun(0)
    _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake,
                          repos_json=str(bad))
    assert fake.calls[0][3] == "claude-runner:latest"


# ── regressions caught in review ────────────────────────────────────────────────────────────

def test_present_override_image_is_used_as_is_and_never_rebuilt(tmp_path):
    # An override that EXISTS is honoured with a single inspect and no build: the user pinned a
    # specific image, and the preflight must not second-guess or replace it.
    #
    # (An earlier revision built the DEFAULT recipe and stamped the override's tag on it, to avoid
    # deriving a nonexistent docker/my-runner.Dockerfile. That is wrong now the workbench is
    # per-language — it would hand a Rust or Java agent a python/node image with no cargo and no
    # JDK. See test_missing_override_image_fails_loudly_instead_of_fabricating_one.)
    import os
    fake = _FakeRun(0)                       # inspect -> present
    os.environ["CLAUDE_RUNNER_IMAGE"] = "my-runner:v1"
    try:
        _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake)
    finally:
        del os.environ["CLAUDE_RUNNER_IMAGE"]
    assert len(fake.calls) == 1
    assert fake.calls[0] == ["docker", "image", "inspect", "my-runner:v1"]


def test_native_claudecode_lane_always_gets_the_default_workbench(tmp_path):
    # benchmark.py:201 hard-wires ClaudeCodeModel to claude-runner:latest regardless of dataset
    # language, so dataset-aware selection must NOT apply to that lane — otherwise a Rust dataset
    # builds claude-runner-rust and every worker then fails on the image nobody built.
    fake = _FakeRun(1, 0)
    ds = _write_dataset(tmp_path, ["Rust", "Rust"])
    _ensure_claude_runner("claudecode", repo_root=str(tmp_path), runner=fake, repos_json=ds)
    tags = [c[3] for c in fake.calls if c[:3] == ["docker", "image", "inspect"]]
    assert tags == ["claude-runner:latest"]


def test_wrapped_repos_dataset_is_understood(tmp_path):
    # benchmark.load_repos accepts {"repos": [...]} as well as a bare list. Iterating the dict form
    # yields string keys, .get() raises, and the broad handler silently degrades to the Python
    # workbench — a Rust run would then reach the producer with no cargo image built.
    import json
    p = tmp_path / "wrapped.json"
    p.write_text(json.dumps({"repos": [{"full_name": "o/r", "language": "Rust"}]}))
    fake = _FakeRun(1, 0)
    _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake,
                          repos_json=str(p))
    tags = [c[3] for c in fake.calls if c[:3] == ["docker", "image", "inspect"]]
    assert tags == ["claude-runner-rust:latest"]


def test_missing_override_image_fails_loudly_instead_of_fabricating_one(tmp_path):
    # CLAUDE_RUNNER_IMAGE means "use exactly this image". If it is absent we cannot know what the
    # user wanted inside it: building the default python/node recipe and stamping their tag on it
    # hands a Rust or Java agent a workbench with no cargo and no JDK, and for a mixed-language
    # dataset there is no single right recipe to guess. Fail loudly — that is the entire point of
    # this preflight.
    import os
    import pytest as _pytest
    fake = _FakeRun(1)                       # inspect -> missing
    os.environ["CLAUDE_RUNNER_IMAGE"] = "my-runner:v1"
    try:
        with _pytest.raises(SystemExit) as exc:
            _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake)
    finally:
        del os.environ["CLAUDE_RUNNER_IMAGE"]
    assert "my-runner:v1" in str(exc.value)
    assert not any(c[1] == "build" for c in fake.calls)   # never guessed a recipe
