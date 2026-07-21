# tests/bench/test_fixture_rust.py
import os
import pathlib
import shutil

import pytest

from bench.docker_client import SubprocessDocker
from bench.schema import HarvestedEnv, RepoSpec
from bench.measure import measure

FIX = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "rust"


def _read_tree(root):
    """Return {relative_path: content} for every file under root except the Dockerfile."""
    out = {}
    for p in root.rglob("*"):
        if p.is_file() and p.name != "Dockerfile":
            out[str(p.relative_to(root))] = p.read_text()
    return out


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("RUN_DOCKER_FIXTURES") != "1",
    reason="needs docker + network; set RUN_DOCKER_FIXTURES=1 to run (e.g. on the VM)",
)
def test_rust_fixture_scores_known_answer():
    env = HarvestedEnv(
        agent="fixture",
        repo=RepoSpec("fixture/rust", "https://example.com/fixture/rust", language="rust"),
        dockerfile=(FIX / "Dockerfile").read_text(), setup_scripts=_read_tree(FIX),
        base_image="rust:1.82", status="ok", meta={})
    row = measure(env, docker=SubprocessDocker(), build_timeout=900, test_timeout=600)
    assert row.build_ok is True and row.ebsr is True and row.executed is True
    assert row.total == 3 and row.passed == 2 and row.failed == 1
    assert row.pass_rate == 0.6667 and row.status == "executed"
