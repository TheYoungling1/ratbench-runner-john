# tests/bench/test_fixture_nodejs.py
import os
import pathlib
import shutil

import pytest

from bench.docker_client import SubprocessDocker
from bench.schema import HarvestedEnv, RepoSpec
from bench.measure import measure

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "nodejs"


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("RUN_DOCKER_FIXTURES") != "1",
    reason="needs docker + network; set RUN_DOCKER_FIXTURES=1 to run (e.g. on the VM)",
)
def test_nodejs_fixture_scores_known_answer():
    files = {p.name: p.read_text() for p in FIX.iterdir() if p.name != "Dockerfile"}
    dockerfile = (FIX / "Dockerfile").read_text()
    env = HarvestedEnv(
        agent="fixture",
        repo=RepoSpec("fixture/node", "https://example.com/fixture/node", language="nodejs"),
        dockerfile=dockerfile, setup_scripts=files, base_image="node:20", status="ok", meta={})
    row = measure(env, docker=SubprocessDocker(), build_timeout=600, test_timeout=600)
    assert row.build_ok is True and row.ebsr is True and row.executed is True
    assert row.total == 3 and row.passed == 2 and row.failed == 1
    assert row.pass_rate == 0.6667 and row.status == "executed"
