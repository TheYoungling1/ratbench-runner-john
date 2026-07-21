# producers/tests/test_claudecode_dockerfile.py — ClaudeCodeDockerfileProducer (design §3.2)
#
# The agent is STUBBED (injected via runner=) — no docker, no keys, no live agent.
from producers.base import ProduceContext, RepoSpec
from producers.claudecode_dockerfile import ClaudeCodeDockerfileProducer


def _ctx(tmp_path):
    return ProduceContext(llm="claude-sonnet", workdir=str(tmp_path))


def test_produce_success(tmp_path):
    def _stub(repo, ctx, **kw):
        return {"dockerfile": "FROM python:3.11\nRUN git clone x /testbed\nRUN pip install pytest",
                "base_image": "python:3.11", "head_sha": "deadbeef"}

    p = ClaudeCodeDockerfileProducer(llm="claude-sonnet", runner=_stub)
    env = p.produce(RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "produced"
    assert env.conformance == "native"
    assert env.producer_name == "claudecode-dockerfile"
    assert env.dockerfile.startswith("FROM python:3.11")
    assert env.base_image == "python:3.11"
    assert env.head_sha == "deadbeef"
    assert env.economy["produce_s"] is not None


def test_produce_ensures_pytest_when_absent(tmp_path):
    def _stub(repo, ctx, **kw):
        return {"dockerfile": "FROM python:3.11\nRUN git clone x /testbed", "base_image": "python:3.11"}

    env = ClaudeCodeDockerfileProducer(runner=_stub).produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "produced"
    assert "pytest" in env.dockerfile          # the fresh-container measure needs pytest


def test_produce_no_gen_is_error(tmp_path):
    def _stub(repo, ctx, **kw):
        return {"dockerfile": None}            # agent wrote no /testbed/Dockerfile.gen

    env = ClaudeCodeDockerfileProducer(runner=_stub).produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "error" and env.dockerfile is None
    assert "Dockerfile.gen" in env.note


def test_produce_agent_error_is_error_not_raise(tmp_path):
    # Anti-vanish (design §1): an agent/docker failure yields status="error", never a raise.
    def _boom(repo, ctx, **kw):
        raise RuntimeError("agent exploded")

    env = ClaudeCodeDockerfileProducer(runner=_boom).produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "error" and env.dockerfile is None
    assert "agent exploded" in env.note


def test_produce_pins_clone_when_commit(tmp_path):
    # Fix #1: the agent's Dockerfile clones into /testbed at HEAD -> the MEASURED build must be
    # pinned. A commit on the RepoSpec injects a checkout right after the clone.
    def _stub(repo, ctx, **kw):
        return {"dockerfile": "FROM python:3.11\nRUN git clone https://github.com/o/r /testbed\n"
                              "RUN pip install pytest",
                "base_image": "python:3.11"}

    env = ClaudeCodeDockerfileProducer(runner=_stub).produce(
        RepoSpec("o/r", "https://github.com/o/r", commit="c0ffee"), _ctx(tmp_path))
    assert env.status == "produced"
    assert "git -C /testbed checkout --detach c0ffee" in env.dockerfile
    assert env.dockerfile.index("git clone") < env.dockerfile.index("checkout --detach")
    assert env.note == ""


def test_produce_no_commit_leaves_clone_unpinned(tmp_path):
    def _stub(repo, ctx, **kw):
        return {"dockerfile": "FROM python:3.11\nRUN git clone https://github.com/o/r /testbed\n"
                              "RUN pip install pytest",
                "base_image": "python:3.11"}

    env = ClaudeCodeDockerfileProducer(runner=_stub).produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "produced"
    assert "checkout --detach" not in env.dockerfile


def test_producer_is_registered():
    from producers import PRODUCERS
    assert PRODUCERS["claudecode-dockerfile"] is ClaudeCodeDockerfileProducer
