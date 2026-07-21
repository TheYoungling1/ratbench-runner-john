# producers/tests/test_base.py — write_env_packet contract (design §1)
import json
import os

from producers.base import ProducedEnv, RepoSpec, write_env_packet


def _repo(full_name="o/r"):
    return RepoSpec(full_name, f"https://github.com/{full_name}")


def test_produced_writes_dockerfile_meta_and_copy_siblings(tmp_path):
    env = ProducedEnv(
        repo=_repo("o/r1"),
        dockerfile="FROM x\nCOPY setup.sh /tmp/s\nRUN bash /tmp/s",
        setup_scripts={"setup.sh": "echo hi"},
        base_image="python:3.12-slim",
        status="produced",
        producer_name="dockeragent",
        economy={"produce_s": 2.5, "tokens_in": 100, "tokens_out": 40},
    )
    repo_dir = write_env_packet(str(tmp_path), env)

    df = os.path.join(repo_dir, "eval_build", "Dockerfile")
    assert os.path.isfile(df) and open(df).read().startswith("FROM x")
    assert open(os.path.join(repo_dir, "eval_build", "setup.sh")).read() == "echo hi"

    meta = json.load(open(os.path.join(repo_dir, "_meta.json")))
    assert meta["status"] == "produced"
    assert meta["producer"] == "dockeragent"
    assert meta["contract_version"] == 1
    assert meta["base_image"] == "python:3.12-slim"
    assert meta["full_name"] == "o/r1" and meta["repo_url"] == "https://github.com/o/r1"
    # economy keys flow into _meta.json (bench reads them for the efficiency columns)
    assert meta["produce_s"] == 2.5 and meta["tokens_in"] == 100 and meta["tokens_out"] == 40
    assert meta["llm_calls"] is None and meta["turns_used"] is None   # absent => explicit null


def test_unmeasurable_writes_meta_but_no_eval_build(tmp_path):
    # status != "produced" (no rebuildable artifact) must NOT create eval_build/ — an empty
    # eval_build/ would be mis-read by harvest as a *vanished* Dockerfile (design §1 verified-fix).
    env = ProducedEnv(
        repo=_repo("o/r2"),
        dockerfile=None,
        status="unmeasurable",
        note="live mutation, no static artifact",
        conformance="native",
    )
    repo_dir = write_env_packet(str(tmp_path), env)

    assert not os.path.isdir(os.path.join(repo_dir, "eval_build"))
    meta = json.load(open(os.path.join(repo_dir, "_meta.json")))
    assert meta["status"] == "unmeasurable"
    assert meta["note"] == "live mutation, no static artifact"


def test_produced_without_dockerfile_skips_eval_build(tmp_path):
    # Defensive: "produced" but dockerfile=None (shouldn't happen) still must NOT create eval_build/.
    env = ProducedEnv(repo=_repo("o/r3"), dockerfile=None, status="produced")
    repo_dir = write_env_packet(str(tmp_path), env)
    assert not os.path.isdir(os.path.join(repo_dir, "eval_build"))
    assert json.load(open(os.path.join(repo_dir, "_meta.json")))["status"] == "produced"


def test_error_status_writes_meta_only(tmp_path):
    env = ProducedEnv(repo=_repo("o/r4"), dockerfile=None, status="error",
                      note="agent produced no Dockerfile")
    repo_dir = write_env_packet(str(tmp_path), env)
    assert not os.path.isdir(os.path.join(repo_dir, "eval_build"))
    meta = json.load(open(os.path.join(repo_dir, "_meta.json")))
    assert meta["status"] == "error" and meta["note"] == "agent produced no Dockerfile"


def test_rewrite_to_error_removes_stale_eval_build(tmp_path):
    # FIX 4: a repo first produced (eval_build/Dockerfile on disk) then re-produced as error must
    # NOT keep the stale Dockerfile — harvest would otherwise still build+measure it.
    repo = _repo("o/rewrite")
    write_env_packet(str(tmp_path), ProducedEnv(repo=repo, dockerfile="FROM x", status="produced"))
    stale = os.path.join(str(tmp_path), "o", "rewrite", "eval_build", "Dockerfile")
    assert os.path.isfile(stale)                    # produced first

    write_env_packet(str(tmp_path), ProducedEnv(repo=repo, dockerfile=None, status="error",
                                                note="agent produced no Dockerfile"))
    assert not os.path.isdir(os.path.join(str(tmp_path), "o", "rewrite", "eval_build"))
    assert json.load(open(os.path.join(str(tmp_path), "o", "rewrite", "_meta.json")))["status"] == "error"


def test_rewrite_to_unmeasurable_removes_stale_eval_build(tmp_path):
    # Same guard for a produced -> unmeasurable rewrite.
    repo = _repo("o/rewrite2")
    write_env_packet(str(tmp_path), ProducedEnv(repo=repo, dockerfile="FROM x", status="produced"))
    assert os.path.isdir(os.path.join(str(tmp_path), "o", "rewrite2", "eval_build"))
    write_env_packet(str(tmp_path), ProducedEnv(repo=repo, dockerfile=None, status="unmeasurable"))
    assert not os.path.isdir(os.path.join(str(tmp_path), "o", "rewrite2", "eval_build"))
