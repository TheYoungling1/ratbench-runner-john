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


# ── telemetry: the Claude Code stream is the ONLY record of cost/turns/trajectory ──────────
import json

from producers.claudecode_dockerfile import _persist_stream

_TELEMETRY_STREAM = "\n".join(json.dumps(o) for o in [
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "pip install -e ."}}]}},
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 7,
     "total_cost_usd": 1.25, "stop_reason": "end_turn",
     "usage": {"input_tokens": 100, "output_tokens": 20,
               "cache_creation_input_tokens": 30, "cache_read_input_tokens": 50}},
])


def test_persist_stream_writes_durable_artifacts(tmp_path):
    out_dir = str(tmp_path / "output" / "o" / "r")
    _persist_stream(out_dir, _TELEMETRY_STREAM, "some stderr")
    assert (tmp_path / "output" / "o" / "r" / "claude_stream.jsonl").read_text() \
        == _TELEMETRY_STREAM
    assert "Bash" in (tmp_path / "output" / "o" / "r" / "claude_actions.log").read_text()
    assert (tmp_path / "output" / "o" / "r" / "claude_stderr.txt").read_text() == "some stderr"


def test_persist_stream_returns_economy_in_write_env_packet_keys(tmp_path):
    econ = _persist_stream(str(tmp_path), _TELEMETRY_STREAM, "")
    assert econ["tokens_in"] == 180          # 100 + 30 + 50
    assert econ["tokens_out"] == 20
    assert econ["total_tokens"] == 200
    assert econ["turns_used"] == 7
    assert econ["cost_usd"] == 1.25
    assert econ["llm_calls"] == 1
    assert econ["tool_calls"] == 1


def test_persist_stream_never_raises_on_unwritable_dir(tmp_path):
    # Anti-vanish (design §1): the Dockerfile is the deliverable. A telemetry IO failure must
    # never fail the produce, so persistence is best-effort and still returns the parsed numbers.
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory")
    econ = _persist_stream(str(blocker / "nested"), _TELEMETRY_STREAM, "")
    assert econ["cost_usd"] == 1.25


def test_producer_passes_economy_through_to_produced_env(tmp_path):
    def _stub(repo, ctx, **kw):
        return {"dockerfile": "FROM python:3.11\nRUN git clone x /testbed\nRUN pip install pytest",
                "base_image": "python:3.11",
                "economy": {"tokens_in": 180, "tokens_out": 20, "turns_used": 7,
                            "cost_usd": 1.25, "llm_calls": 1}}

    env = ClaudeCodeDockerfileProducer(runner=_stub).produce(
        RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))
    assert env.status == "produced"
    assert env.economy["turns_used"] == 7
    assert env.economy["cost_usd"] == 1.25
    assert env.economy["tokens_in"] == 180
    assert env.economy["produce_s"] is not None      # still stamped by produce()
