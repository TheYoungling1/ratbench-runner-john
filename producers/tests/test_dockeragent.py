# producers/tests/test_dockeragent.py — DockerAgentProducer.produce (design §3 method 1)
#
# The adapter is STUBBED (injected via adapter_cls) — no real LLM, no docker.
import json
import os

from producers.base import ProduceContext, ProducedEnv, RepoSpec, write_env_packet
from producers.dockeragent import DockerAgentProducer


class _StubAdapter:
    """Stands in for MultiDockerEvalAdapter. `result` is what process_single_instance returns
    for the instance id (keyed as {id: result}, the shape the real adapter uses)."""
    result = {"dockerfile": "FROM x", "setup_scripts": {}, "base_image": "python:3.12"}

    def __init__(self, output_dir=None):
        self.output_dir = output_dir

    def process_single_instance(self, inst, **kw):
        return {inst["instance_id"]: dict(self.result)}


def _ctx(tmp_path):
    return ProduceContext(llm="deepseek/deepseek-v4-flash", workdir=str(tmp_path))


def test_produce_success(tmp_path):
    p = DockerAgentProducer(llm="deepseek/deepseek-v4-flash", adapter_cls=_StubAdapter)
    env = p.produce(RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "produced"
    # ensure-pytest (matches today's model) appends a pip install when pytest is absent, so we
    # assert the prefix rather than exact equality.
    assert env.dockerfile.startswith("FROM x")
    assert "pytest" in env.dockerfile
    assert env.base_image == "python:3.12"
    assert env.producer_name == "dockeragent"
    assert env.conformance == "native"
    assert env.economy["produce_s"] is not None


def test_produce_no_dockerfile_is_error(tmp_path):
    class _NoDockerfile(_StubAdapter):
        result = {"setup_scripts": {}, "logs": {"error": "boom"}}

    p = DockerAgentProducer(adapter_cls=_NoDockerfile)
    env = p.produce(RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "error"
    assert env.dockerfile is None
    assert "boom" in env.note


def test_produce_adapter_raises_is_error_not_raise(tmp_path):
    # Anti-vanish invariant (design §1): produce() never raises past its own boundary.
    class _Boom(_StubAdapter):
        def process_single_instance(self, inst, **kw):
            raise RuntimeError("adapter exploded")

    p = DockerAgentProducer(adapter_cls=_Boom)
    env = p.produce(RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "error" and env.dockerfile is None
    assert "adapter exploded" in env.note


def test_produce_keeps_existing_pytest_dockerfile(tmp_path):
    class _HasPytest(_StubAdapter):
        result = {"dockerfile": "FROM y\nRUN pip install pytest", "base_image": "python:3.11"}

    p = DockerAgentProducer(adapter_cls=_HasPytest)
    env = p.produce(RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    # already has pytest -> no extra RUN appended
    assert env.dockerfile == "FROM y\nRUN pip install pytest"


def test_produce_malformed_result_is_error_not_raise(tmp_path):
    # FIX 5: a non-dict adapter result must be caught inside produce() and yield a ProducedEnv,
    # not propagate (which in standalone use would leave no _meta at all).
    class _Malformed(_StubAdapter):
        def process_single_instance(self, inst, **kw):
            return ["not", "a", "dict"]

    env = DockerAgentProducer(adapter_cls=_Malformed).produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "error" and env.dockerfile is None
    assert env.note   # repr(exc) is populated


def test_produce_error_then_write_leaves_no_eval_build(tmp_path):
    # FIX 3: a produce error routed through the writer must NOT create eval_build/ (an empty
    # eval_build/ reads as a vanished Dockerfile to harvest).
    class _NoDockerfile(_StubAdapter):
        result = {"setup_scripts": {}, "logs": {"error": "nope"}}

    env = DockerAgentProducer(adapter_cls=_NoDockerfile).produce(
        RepoSpec("o/err", "https://github.com/o/err"), _ctx(tmp_path))
    assert env.status == "error"

    out_root = tmp_path / "out"
    out_root.mkdir()
    write_env_packet(str(out_root), env)
    assert not os.path.isdir(os.path.join(str(out_root), "o", "err", "eval_build"))
    assert json.load(open(os.path.join(str(out_root), "o", "err", "_meta.json")))["status"] == "error"
