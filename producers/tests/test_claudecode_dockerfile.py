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
    # A turn IS an LLM call: one `assistant` event in the fixture. The result event's own
    # num_turns (7, user+assistant) is deliberately NOT what turns_used reports — it does not
    # exist on a capped or walled run, and it is not the unit the turn cap counts.
    assert econ["turns_used"] == 1
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


# ── the prompt must NOT be in the agent's own command line ────────────────────────────────
#
# MEASURED on rust-full50-20260727-145029: 3 of the first 12 repos died mid-run. Every one ended
# on the agent running `pkill -f "cargo test --no-run"` to clear a hung compile. The producer used
# to pass the prompt as an argv (`claude -p "<prompt>"`), and the Rust prompt contains the literal
# string `cargo test --no-run` four times — so the pattern matched the agent's OWN process. Read
# straight off a live container: `pgrep -af "cargo test --no-run"` returned
#     19 claude -p You are configuring a Rust repository at /testbed ...
# The agent killed itself. Casualties were risingwave and hyperswitch (no Dockerfile.gen ->
# status=error) and zed (Dockerfile already written -> status=produced but every economy field
# null, because the CLI never emitted its final type=="result" event).
#
# Feeding the prompt on stdin removes the whole class: nothing repo- or language-specific is left
# in the process table for a `pkill -f` to hit. The prompt BYTES are unchanged, so this is a
# transport fix, not a prompt change — python50/node50 stay comparable.

class _Stop(Exception):
    """Sentinel: unwind the live runner the moment the agent would have been invoked."""


def _invoke_live_runner(monkeypatch, tmp_path, language="rust"):
    """Drive the REAL `run_claudecode_dockerfile` with docker + the RAT tree stubbed out, and
    return the `(claude_cmd, stdin_text)` it was about to hand the agent.

    Worth the setup: the argv is built inside this function, so the injected-stub route the other
    tests use (which replaces the whole runner) cannot see the defect this guards against."""
    import subprocess as sp
    import sys
    import types

    from producers import claudecode_dockerfile as mod

    fake = types.ModuleType("libkit.command")
    fake.download_repo = lambda *a, **k: None
    fake.init_output_and_repo = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "libkit.command", fake)
    monkeypatch.setattr(mod, "ensure_rat_on_path", lambda: None)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    monkeypatch.setattr(sp, "run",
                        lambda *a, **k: sp.CompletedProcess(a[0] if a else [], 0, "", ""))

    seen = {}

    def _capture(cmd, timeout, stdin_text=None, **kw):
        seen["cmd"], seen["stdin"] = cmd, stdin_text
        raise _Stop

    monkeypatch.setattr(mod, "_capture_claude_stream", _capture)
    repo = RepoSpec("o/r", "https://github.com/o/r", language=language)   # frozen dataclass
    try:
        mod.run_claudecode_dockerfile(repo, _ctx(tmp_path), llm="claude-sonnet")
    except _Stop:
        pass
    return seen["cmd"], seen["stdin"]


def test_prompt_is_not_in_the_command_line(monkeypatch, tmp_path):
    """The argv must not carry the prompt — that is what let `pkill -f` match the agent itself."""
    cmd, stdin_text = _invoke_live_runner(monkeypatch, tmp_path)
    argv = " ".join(cmd)
    assert "cargo test --no-run" not in argv
    assert "You are configuring" not in argv
    # ...and the prompt still reaches the agent, just by the other door.
    assert stdin_text and "You are configuring" in stdin_text
    assert "cargo test --no-run" in stdin_text      # unchanged bytes, different transport


def test_docker_exec_keeps_stdin_open(monkeypatch, tmp_path):
    """`docker exec` without -i closes stdin, so the piped prompt would never arrive."""
    cmd, _ = _invoke_live_runner(monkeypatch, tmp_path)
    assert cmd[:2] == ["docker", "exec"]
    assert "-i" in cmd[:cmd.index("claude")], "docker exec needs -i to attach stdin"
    # -p must stay flag-only; a bare `-p` with no operand is what makes the CLI read stdin.
    assert cmd[cmd.index("claude") + 1] == "-p"
    assert cmd[cmd.index("claude") + 2].startswith("--")


def test_no_language_prompt_leaks_into_argv(monkeypatch, tmp_path):
    """Rust is where it was measured, but every prompt names the command the agent may pkill."""
    for lang in ("python", "nodejs", "rust", "java"):
        cmd, stdin_text = _invoke_live_runner(monkeypatch, tmp_path, language=lang)
        assert "You are configuring" not in " ".join(cmd), lang
        assert stdin_text, lang


def test_capture_forwards_stdin_to_subprocess(monkeypatch):
    import subprocess as sp

    from producers import claudecode_dockerfile as mod

    seen = {}

    def _run(*a, **k):
        seen.update(k)
        return sp.CompletedProcess(["claude"], 0, stdout="out", stderr="err")

    monkeypatch.setattr(sp, "run", _run)
    assert mod._capture_claude_stream(["claude"], 10, "THE PROMPT") == ("out", "err")
    assert seen["input"] == "THE PROMPT"


def test_capture_timeout_still_decodes_when_stdin_used(monkeypatch):
    """The timeout path is the common one; adding `input=` must not regress its byte decoding."""
    import subprocess as sp

    from producers import claudecode_dockerfile as mod

    def _boom(*a, **k):
        raise sp.TimeoutExpired(cmd=["claude"], timeout=1, output=b"partial", stderr=b"err")

    monkeypatch.setattr(sp, "run", _boom)
    assert mod._capture_claude_stream(["claude"], 1, "THE PROMPT") == ("partial", "err")


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
