# tests/bench/test_schema.py
import dataclasses
import pytest
from bench.schema import RepoSpec, HarvestedEnv, MeasureRow


def test_repospec_defaults_and_frozen():
    r = RepoSpec(full_name="owner/repo", repo_url="https://github.com/owner/repo")
    assert r.language == "python"
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.language = "node"  # type: ignore[misc]


def test_harvested_env_minimal():
    e = HarvestedEnv(agent="v3", repo=RepoSpec("o/r", "https://github.com/o/r"), dockerfile="FROM x")
    assert e.status == "ok" and e.setup_scripts == {} and e.meta == {}


def test_measurerow_minimal_uses_defaults():
    row = MeasureRow(agent="v3", repo="o/r", env_status="ok", build_ok=True)
    assert row.collected_node_ids == () and row.passed_node_ids == ()
    assert row.tokens_in is None and row.image_delta_mb is None
    assert row.ebsr is False and row.pass_rate == 0.0
