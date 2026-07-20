# tests/bench/test_docker_client.py
import subprocess
import bench.docker_client as dc


def test_image_size_mb_parses_bytes(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: type("P", (), {"stdout": "524288000", "returncode": 0})())
    assert dc.SubprocessDocker().image_size_mb("img") == 500.0


def test_image_size_mb_none_on_garbage(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: type("P", (), {"stdout": "nope", "returncode": 1})())
    assert dc.SubprocessDocker().image_size_mb("img") is None


def test_exec_timeout_returns_124(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)
    monkeypatch.setattr(subprocess, "run", boom)
    rc, out, timed = dc.SubprocessDocker().exec("c", ["echo", "hi"], timeout=1)
    assert rc == 124 and timed is True


def test_build_timeout_returns_124(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="docker build", timeout=1)
    monkeypatch.setattr(subprocess, "run", boom)
    rc, log = dc.SubprocessDocker().build("t", "/ctx", timeout=1)
    assert rc == 124 and "timed out" in log
