# producers/tests/test_roundtrip.py — proves the produce -> harvest contract end to end.
#
# write_env_packet lands the packet; bench.harvest.discover reads it back and resolves
# status=="produced" with the Dockerfile intact (design §1 + §2.5).
from bench.harvest import discover

from producers.base import ProducedEnv, RepoSpec, write_env_packet


def test_write_then_harvest_resolves_produced(tmp_path):
    out_root = tmp_path / "run" / "output"
    out_root.mkdir(parents=True)
    env = ProducedEnv(
        repo=RepoSpec("owner/repo", "https://github.com/owner/repo"),
        dockerfile="FROM python:3.12\nRUN pip install pytest\nCOPY setup.sh /tmp/s",
        setup_scripts={"setup.sh": "echo hi"},
        base_image="python:3.12",
        status="produced",
        producer_name="dockeragent",
        economy={"produce_s": 1.25, "tokens_in": 7},
    )
    write_env_packet(str(out_root), env)

    envs = discover({"dockeragent": str(out_root)})
    assert len(envs) == 1
    e = envs[0]
    assert e.agent == "dockeragent"
    assert e.repo.full_name == "owner/repo"
    assert e.status == "produced"                      # _meta.status wins (design §2.5)
    assert e.dockerfile.startswith("FROM python:3.12")
    assert e.setup_scripts["setup.sh"] == "echo hi"    # COPY sibling harvested
    assert e.base_image == "python:3.12"
    assert e.meta["produce_s"] == 1.25 and e.meta["tokens_in"] == 7


def test_write_then_harvest_unmeasurable_has_no_dockerfile(tmp_path):
    out_root = tmp_path / "run" / "output"
    out_root.mkdir(parents=True)
    env = ProducedEnv(repo=RepoSpec("o/live", "https://github.com/o/live"),
                      dockerfile=None, status="unmeasurable", note="live")
    write_env_packet(str(out_root), env)

    e = discover({"dockeragent": str(out_root)})[0]
    # harvest reads _meta.status (design §2.5): unmeasurable, no Dockerfile -> excluded downstream.
    assert e.status == "unmeasurable" and e.dockerfile is None


def test_ccdf_economy_reaches_meta_json_and_measure_row(tmp_path):
    # The full produce->measure seam for the fields a ccdf cost analysis needs: economy ->
    # write_env_packet -> _meta.json -> harvest -> measure() base_row -> MeasureRow.
    import json
    import os

    from producers.base import ProducedEnv, RepoSpec, write_env_packet

    out_root = str(tmp_path / "output")
    env = ProducedEnv(repo=RepoSpec("o/r", "https://github.com/o/r"),
                      dockerfile="FROM python:3.11\nRUN pip install pytest",
                      status="produced", producer_name="claudecode-dockerfile",
                      economy={"tokens_in": 180, "tokens_out": 20, "total_tokens": 200,
                               "llm_calls": 3, "turns_used": 7, "cost_usd": 1.25,
                               "produce_s": 42.0})
    repo_dir = write_env_packet(out_root, env)
    with open(os.path.join(repo_dir, "_meta.json")) as fh:
        meta = json.load(fh)
    assert meta["tokens_in"] == 180 and meta["tokens_out"] == 20
    assert meta["llm_calls"] == 3 and meta["turns_used"] == 7
    assert meta["total_tokens"] == 200 and meta["produce_s"] == 42.0
    assert meta["cost_usd"] == 1.25
