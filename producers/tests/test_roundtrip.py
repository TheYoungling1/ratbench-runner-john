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
