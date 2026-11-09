# tests/bench/test_runtime_handoff.py
"""runtime_handoff.json (Graph2Env) is applied at measure time: env via `docker run -e`,
services started once before the test commands -- as Graph2Env's own EBSR runner does."""
import json
import subprocess

from bench import docker_client
from bench.harvest import discover
from bench.measure import measure, services_script
from bench.schema import HarvestedEnv, RepoSpec

REPO = RepoSpec("o/r", "https://github.com/o/r")
HANDOFF = {
    "version": 2,
    "services": [{"kind": "redis", "start": "redis-server --daemonize yes", "check": "redis-cli ping"},
                 {"kind": "bad", "start": "", "check": "x"}],
    "environment": {"PYTHONPATH": "/app/src:/app", "DJANGO_SETTINGS_MODULE": "s", "bad key": "x"},
    "capabilities": [], "test": {"command": "pytest"}, "mounts": [],
}


def _harvest(tmp_path, handoff):
    d = tmp_path / "out" / "o" / "r"
    d.mkdir(parents=True)
    (d / "Dockerfile").write_text("FROM x\n")
    if handoff is not None:
        (d / "runtime_handoff.json").write_text(handoff if isinstance(handoff, str) else json.dumps(handoff))
    [env] = discover({"g2e": str(tmp_path / "out")})
    return env


def test_harvest_reads_handoff_with_testbed_paths(tmp_path):
    rt = _harvest(tmp_path, HANDOFF).runtime
    assert rt["environment"] == {"PYTHONPATH": "/testbed/src:/testbed", "DJANGO_SETTINGS_MODULE": "s"}
    assert [s["kind"] for s in rt["services"]] == ["redis"]


def test_harvest_without_or_with_bad_handoff_is_empty(tmp_path):
    assert _harvest(tmp_path / "a", None).runtime == {}
    assert _harvest(tmp_path / "b", "{not json").runtime == {}
    assert _harvest(tmp_path / "c", {"version": 9, "environment": {"A": "1"}}).runtime == {}


class RecordingDocker:
    def __init__(self):
        self.run_kwargs, self.calls = None, []

    def build(self, tag, ctx, timeout=None):
        return 0, ""

    def image_size_mb(self, tag):
        return None

    def run_detached(self, tag, name, workdir, **kw):
        self.run_kwargs = kw

    def exec(self, name, argv, timeout=None):
        cmd = " ".join(argv)
        self.calls.append(cmd)
        return (0, "STATUS=conforming py_test_files=1", False) if "py_test_files" in cmd else (0, "", False)

    def rm(self, name, tag):
        pass


def _measure(runtime):
    docker = RecordingDocker()
    row = measure(HarvestedEnv(agent="g2e", repo=REPO, dockerfile="FROM x", status="ok",
                               runtime=runtime), docker=docker)
    return docker, row


def test_measure_applies_env_and_starts_services_before_tests():
    docker, row = _measure({"environment": {"A": "1"}, "services": HANDOFF["services"][:1]})
    assert docker.run_kwargs == {"env": {"A": "1"}}
    assert "redis-server --daemonize yes" in docker.calls[0]          # before pytest bootstrap
    assert row.meta["runtime_handoff"] == {"environment": ["A"], "services": ["redis"],
                                           "services_rc": 0}


def test_measure_without_handoff_is_unchanged():
    docker, row = _measure({})
    assert docker.run_kwargs == {} and "runtime_handoff" not in row.meta


def test_run_detached_passes_env(monkeypatch):
    seen = []
    monkeypatch.setattr(docker_client.subprocess, "run", lambda argv, **kw: seen.append(argv))
    docker_client.SubprocessDocker().run_detached("img", "c", "/testbed", env={"A": "x y"})
    assert seen[-1][:8] == ["docker", "run", "-d", "--name", "c", "-w", "/testbed", "-e"]
    assert "A=x y" in seen[-1]


def test_services_script_starts_once_and_reports_unready(tmp_path):
    marker, count = tmp_path / "up", tmp_path / "n"
    ok = services_script([{"kind": "f", "start": f"echo x >> {count}; touch {marker}",
                           "check": f"test -f {marker}"}])
    for _ in range(2):
        assert subprocess.run(["bash", "-c", ok]).returncode == 0
    assert count.read_text().count("x") == 1
    dead = services_script([{"kind": "dead", "start": "false", "check": "false"}]).replace("sleep 1", "true")
    out = subprocess.run(["bash", "-c", dead], capture_output=True, text=True)
    assert out.returncode == 1 and "dead" in out.stderr
