# tests/bench/test_fixture_java.py
import os
import pathlib
import shutil

import pytest

from bench.docker_client import SubprocessDocker
from bench.schema import HarvestedEnv, RepoSpec
from bench.measure import measure

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "java"


def _read_tree(root):
    out = {}
    for p in root.rglob("*"):
        if p.is_file() and p.name != "Dockerfile":
            out[str(p.relative_to(root))] = p.read_text()
    return out


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("RUN_DOCKER_FIXTURES") != "1",
    reason="needs docker + network; set RUN_DOCKER_FIXTURES=1 to run (e.g. on the VM)",
)
def test_java_fixture_scores_known_answer():
    env = HarvestedEnv(
        agent="fixture",
        repo=RepoSpec("fixture/java", "https://example.com/fixture/java", language="java"),
        dockerfile=(FIX / "Dockerfile").read_text(), setup_scripts=_read_tree(FIX),
        base_image="maven:3.9-eclipse-temurin-17", status="ok", meta={})
    row = measure(env, docker=SubprocessDocker(), build_timeout=900, test_timeout=900)
    assert row.build_ok is True and row.ebsr is True and row.executed is True
    assert row.total == 3 and row.passed == 2 and row.failed == 1
    assert row.pass_rate == 0.6667 and row.status == "executed"
