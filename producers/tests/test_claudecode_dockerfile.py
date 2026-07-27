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


def test_produce_appends_no_pytest_for_a_node_repo(tmp_path):
    # The regression this whole seam exists to stop: `RUN pip install ... pytest` on a node base
    # (no pip) fails the BUILD, so every Node repo would score build_fail before a test could run.
    def _stub(repo, ctx, **kw):
        return {"dockerfile": "FROM node:20\nRUN git clone x /testbed\nRUN npm ci",
                "base_image": "node:20"}

    env = ClaudeCodeDockerfileProducer(runner=_stub).produce(
        RepoSpec("o/r", "https://github.com/o/r", language="nodejs"), _ctx(tmp_path))
    assert env.status == "produced"
    assert "pip" not in env.dockerfile and "pytest" not in env.dockerfile


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


# ── _ensure_test_runner: the per-language staple-on ─────────────────────────────────────────

from producers.claudecode_dockerfile import _ensure_test_runner

_PY_DF = "FROM python:3.11\nRUN git clone x /testbed"
_NODE_DF = "FROM node:20\nRUN git clone x /testbed\nRUN npm ci"


def test_ensure_test_runner_appends_pytest_for_python():
    out = _ensure_test_runner(_PY_DF, "python")
    assert out.endswith("RUN pip install --no-cache-dir pytest\n")


def test_ensure_test_runner_is_idempotent_when_pytest_present():
    have = _PY_DF + "\nRUN pip install pytest\n"
    assert _ensure_test_runner(have, "python") == have          # unchanged, not appended twice
    assert _ensure_test_runner(have, "python").count("pip install") == 1


def test_ensure_test_runner_is_a_noop_for_node_even_without_pytest():
    # NodeLanguage.ensure_cmd installs the JUnit reporters at measure time, and pip does not exist
    # on a node base — appending the Python line here breaks the build outright.
    for alias in ("nodejs", "node", "javascript", "typescript"):
        assert _ensure_test_runner(_NODE_DF, alias) == _NODE_DF, alias


def test_ensure_test_runner_treats_unknown_language_as_python():
    # get_profile falls back to Python, so a language-less RepoSpec keeps the old behavior exactly.
    for lang in ("", None, "cobol"):
        assert _ensure_test_runner(_PY_DF, lang).endswith(
            "RUN pip install --no-cache-dir pytest\n"), lang


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


# ── the timeout path: the COMMON case (budget cap / wall) and the one that regressed before ──
#
# On TimeoutExpired, CPython fills exc.stdout/exc.stderr with RAW BYTES even though text=True was
# passed, and either may be None. A naive str() would write "b'...'" into the trajectory log.

def test_capture_decodes_stdout_on_success(monkeypatch):
    import subprocess as sp

    from producers import claudecode_dockerfile as mod

    # _capture_claude_stream does a function-local `import subprocess`, so it resolves the same
    # module object we patch here.
    monkeypatch.setattr(sp, "run",
                        lambda *a, **k: sp.CompletedProcess(["claude"], 0,
                                                            stdout="out", stderr="err"))
    assert mod._capture_claude_stream(["claude"], 10) == ("out", "err")


def test_capture_decodes_bytes_from_timeout(monkeypatch):
    import subprocess as sp

    from producers import claudecode_dockerfile as mod

    def _boom(*a, **k):
        raise sp.TimeoutExpired(cmd=["claude"], timeout=1,
                                output=b"partial trajectory", stderr=b"partial err")

    monkeypatch.setattr(sp, "run", _boom)
    out, err = mod._capture_claude_stream(["claude"], 1)
    assert out == "partial trajectory" and err == "partial err"   # decoded, not "b'...'"
    assert "b'" not in out


def test_capture_handles_none_output_from_timeout(monkeypatch):
    import subprocess as sp

    from producers import claudecode_dockerfile as mod

    def _boom(*a, **k):
        raise sp.TimeoutExpired(cmd=["claude"], timeout=1)      # output/stderr default to None

    monkeypatch.setattr(sp, "run", _boom)
    assert mod._capture_claude_stream(["claude"], 1) == ("", "")


# ── persistence must be TOTAL, not just OSError-tolerant ──────────────────────────────────

def test_persist_stream_writes_utf8_regardless_of_ambient_locale(tmp_path):
    # Without an explicit encoding these writes inherit the locale; under C/POSIX (the default in
    # minimal images) one non-ASCII char raises UnicodeEncodeError, which is NOT an OSError.
    stream = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "installed café ☕ — done"}]}})
    _persist_stream(str(tmp_path), stream, "")
    text = (tmp_path / "claude_actions.log").read_text(encoding="utf-8")
    assert "café ☕" in text


def test_persist_stream_swallows_non_oserror_failures(tmp_path, monkeypatch, capsys):
    # A telemetry bug must never downgrade a paid-for produce to status="error".
    from producers import claudecode_dockerfile as mod

    def _boom(*a, **k):
        raise ValueError("not an OSError")

    monkeypatch.setattr(mod.os, "makedirs", _boom)
    econ = _persist_stream(str(tmp_path / "x"), _TELEMETRY_STREAM, "")
    assert econ["cost_usd"] == 1.25                       # numbers still returned
    assert "telemetry persist failed" in capsys.readouterr().out   # but not silent
