# producers/tests/test_non_producers.py — the three NON-PRODUCER gates (design §3 methods 5-6 +
# rat "Open call", LOCKED inline-only).
#
# rat / sweagent / claudecode (live) have NO rebuildable artifact. Each declares measurable=False
# and produce() returns a pure ProducedEnv(status="unmeasurable", dockerfile=None) — no docker, no
# LLM, no scoring. write_env_packet must land _meta.json (status=="unmeasurable") and create NO
# eval_build/ (an empty eval_build/ reads as a *vanished* Dockerfile to harvest).
import json
import os

import pytest

import producers
from producers.base import ProduceContext, ProducedEnv, RepoSpec, write_env_packet
from producers.claudecode_live import ClaudeCodeLiveProducer
from producers.rat import RatProducer
from producers.sweagent import SweAgentProducer

# (registry key, producer class) for every non-producer gate.
_GATES = [
    ("rat", RatProducer),
    ("sweagent", SweAgentProducer),
    ("claudecode", ClaudeCodeLiveProducer),
]


def _ctx(tmp_path):
    return ProduceContext(llm="deepseek/deepseek-v4-flash", workdir=str(tmp_path))


@pytest.mark.parametrize("name,cls", _GATES)
def test_registered_under_expected_key(name, cls):
    assert producers.PRODUCERS[name] is cls
    assert cls.name == name


@pytest.mark.parametrize("name,cls", _GATES)
def test_non_producer_flags(name, cls):
    # measurable=False => NEVER harvested into reproducible EBSR/ESSR; needs_llm=True => it still
    # runs live for its own inline score.
    assert cls.measurable is False
    assert cls.needs_llm is True


@pytest.mark.parametrize("name,cls", _GATES)
def test_produce_returns_unmeasurable_env(name, cls, tmp_path):
    env = cls(llm="deepseek/deepseek-v4-flash").produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert isinstance(env, ProducedEnv)
    assert env.dockerfile is None
    assert env.status == "unmeasurable"
    assert env.note                                   # non-empty reason
    assert env.producer_name == name


@pytest.mark.parametrize("name,cls", _GATES)
def test_produce_tolerates_extra_kwargs(name, cls, tmp_path):
    # get(name, **kw) may pass num_turn/base_image etc.; the gate must tolerate them.
    env = cls(llm="x", num_turn=30, base_image="auto").produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "unmeasurable" and env.dockerfile is None


@pytest.mark.parametrize("name,cls", _GATES)
def test_write_env_packet_meta_only_no_eval_build(name, cls, tmp_path):
    # Round-trip through write_env_packet: _meta.json exists with status=="unmeasurable" and NO
    # eval_build/ dir is created (mirrors test_base.py::test_unmeasurable_writes_meta_but_no_eval_build).
    env = cls().produce(RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    out_root = tmp_path / "out"
    out_root.mkdir()
    repo_dir = write_env_packet(str(out_root), env)

    assert not os.path.isdir(os.path.join(repo_dir, "eval_build"))
    meta = json.load(open(os.path.join(repo_dir, "_meta.json")))
    assert meta["status"] == "unmeasurable"
    assert meta["producer"] == name
    assert meta["note"]
