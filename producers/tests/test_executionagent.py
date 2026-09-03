# producers/tests/test_executionagent.py — the pure synthesize transform + ExecutionAgentProducer.
#
# ExecutionAgent itself is STUBBED (injected via runner=) — no real repo, no docker, no keys.
import os

from producers.base import ProduceContext, RepoSpec, write_env_packet
from producers.executionagent import (
    COMMANDS_BASENAME,
    ExecutionAgentProducer,
    parse_trace,
    render_replay,
    sanitize_commands,
    synthesize,
)

# EA's documented Dockerfile shape (prompt_files/steps_list.json): base + apt + clone under /app.
# Deliberately NO dependency install — that is the whole reason this producer exists.
_EA_DF = ("FROM python:3.10-slim\n"
          "RUN apt-get update && apt-get install -y git\n"
          "WORKDIR /app\n"
          "RUN git clone https://github.com/o/r.git || exit 0\n"
          "WORKDIR /app/r\n"
          'CMD ["bash"]\n')

_EA_CMDS = ("#!/usr/bin/env bash\n"
            "set -e  # Exit on error\n"
            "\n# Command 1\ncd /app/r && python -m venv .venv\n"
            "\n# Command 2\n. .venv/bin/activate && pip install -e .[test]\n")


def _ctx(tmp_path):
    return ProduceContext(llm="openrouter/deepseek/deepseek-v4-flash", workdir=str(tmp_path))


def _repo():
    return RepoSpec("o/r", "https://github.com/o/r", commit="abc123", language="python")


def test_synthesize_replays_commands_then_rehomes():
    df, scripts = synthesize(_EA_DF, _EA_CMDS)
    # The install transcript is COPY'd in and replayed as a build layer...
    assert f"COPY {COMMANDS_BASENAME} /tmp/{COMMANDS_BASENAME}" in df
    assert scripts[COMMANDS_BASENAME].startswith("#!")
    # ...before the re-home, so the venv/editable install lands while the repo is still at /app.
    assert df.index("COPY " + COMMANDS_BASENAME) < df.index("mv \"$d\" /testbed")
    assert df.rstrip().endswith("WORKDIR /testbed")
    assert "ENV PATH=/testbed/.venv/bin:$PATH" in df
    # The EA body is preserved (append, not rewrite).
    assert df.startswith("FROM python:3.10-slim")


def test_replay_never_fails_the_build():
    # A non-zero transcript is EXPECTED (EA records failed probes too); the env it leaves behind
    # is what we measure, so the replay layer must not abort the build.
    df, _ = synthesize(_EA_DF, _EA_CMDS)
    assert "|| true" in [ln[-7:] for ln in df.splitlines() if ln.startswith("RUN timeout")][0]


def test_testbed_is_a_real_dir_not_a_symlink():
    # bench's C1 guard uses `find -P`: a symlinked /testbed is non_conforming.
    import re
    df, _ = synthesize(_EA_DF, _EA_CMDS)
    assert not re.search(r"ln\s+-s\S*\s+\S+\s+/testbed(?:\s|\"|$)", df)
    assert 'ln -sfn /testbed "$d"' in df       # reverse symlink only


def test_sanitize_flips_set_e_so_one_failed_probe_does_not_drop_the_installs():
    out = sanitize_commands(_EA_CMDS)
    assert "set +e" in out and "set -e" not in out
    assert "pip install -e .[test]" in out     # the command AFTER a would-be abort survives


def test_sanitize_handles_an_empty_transcript():
    out = sanitize_commands("")
    assert out.startswith("#!") and "set +e" in out


def test_forced_exit_dockerfile_gets_no_replay_layer():
    # forced_exit_cycle/ ships no commands.sh; its Dockerfile is meant to stand alone.
    df, scripts = synthesize(_EA_DF, None)
    assert scripts == {}
    assert COMMANDS_BASENAME not in df
    assert 'mv "$d" /testbed' in df


def test_produce_pins_the_clone_to_the_dataset_commit(tmp_path):
    def stub(repo, ctx, *, llm, budget):
        return {"dockerfile": _EA_DF, "commands_sh": _EA_CMDS, "note": "", "economy": {}}

    env = ExecutionAgentProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.status == "produced"
    assert "git -C /app/r fetch --depth 1 origin abc123" in env.dockerfile
    assert env.conformance == "synthesized"
    assert env.base_image == "python:3.10-slim"


def test_produce_writes_the_commands_script_into_the_build_context(tmp_path):
    def stub(repo, ctx, *, llm, budget):
        return {"dockerfile": _EA_DF, "commands_sh": _EA_CMDS, "note": "", "economy": {}}

    env = ExecutionAgentProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    out = str(tmp_path / "output")
    write_env_packet(out, env)
    # bench.harvest re-reads COPY siblings from this dir; a missing script = a build failure.
    assert os.path.isfile(os.path.join(out, "o", "r", "eval_build", COMMANDS_BASENAME))


def test_produce_is_anti_vanish_when_execution_agent_blows_up(tmp_path):
    def boom(repo, ctx, *, llm, budget):
        raise RuntimeError("EA crashed")

    env = ExecutionAgentProducer(runner=boom).produce(_repo(), _ctx(tmp_path))
    assert env.status == "error" and env.dockerfile is None
    assert "EA crashed" in env.note


def test_produce_reports_error_when_execution_agent_emits_nothing(tmp_path):
    def nothing(repo, ctx, *, llm, budget):
        return {"dockerfile": None, "commands_sh": None, "note": "no artifacts", "economy": {}}

    env = ExecutionAgentProducer(runner=nothing).produce(_repo(), _ctx(tmp_path))
    assert env.status == "error" and env.note == "no artifacts"


# ── run.jsonl reconstruction ─────────────────────────────────────────────────────────────────
# EA's commands.sh drops in-container `write_to_file` calls; run.jsonl keeps them, in order.
_JSONL = "\n".join([
    '{"event": "cycle_tool", "tool_name": "linux_terminal", "tool_args": {"command": "ls -la"}}',
    '{"event": "cycle_tool", "tool_name": "write_to_file", "tool_args": {"file_path": "Dockerfile", "content": "FROM python:3.10"}}',
    '{"event": "cycle_start", "message": "CYCLE 03 START"}',
    '{"event": "cycle_tool", "tool_name": "linux_terminal", "tool_args": {"command": "cd /app/r && python -m venv .venv"}}',
    '{"event": "cycle_tool", "tool_name": "write_to_file", "tool_args": {"file_path": "/app/r/conftest.py", "content": "import sys\\ncollect_ignore = []"}}',
    '{"event": "cycle_tool", "tool_name": "linux_terminal", "tool_args": {"command": "pip install -e .[test]"}}',
])


def test_parse_trace_keeps_file_writes_that_commands_sh_drops():
    steps = parse_trace(_JSONL)
    assert steps == [
        ("cmd", "cd /app/r && python -m venv .venv"),
        ("file", "/app/r/conftest.py", "import sys\ncollect_ignore = []"),
        ("cmd", "pip install -e .[test]"),
    ]
    # The pre-container exploration (`ls -la`) is NOT replayed — it ran on the host.
    assert all("ls -la" not in s[1] for s in steps)


def test_parse_trace_boundary_is_the_last_dockerfile_write():
    # A failed build means several Dockerfile writes; only the last one produced the container,
    # so commands between the attempts ran on the HOST and must not be replayed.
    jsonl = "\n".join([
        '{"event": "cycle_tool", "tool_name": "write_to_file", "tool_args": {"file_path": "Dockerfile", "content": "FROM bad"}}',
        '{"event": "cycle_tool", "tool_name": "linux_terminal", "tool_args": {"command": "host-only probe"}}',
        '{"event": "cycle_tool", "tool_name": "write_to_file", "tool_args": {"file_path": "Dockerfile", "content": "FROM python:3.10"}}',
        '{"event": "cycle_tool", "tool_name": "linux_terminal", "tool_args": {"command": "pip install ."}}',
    ])
    assert parse_trace(jsonl) == [("cmd", "pip install .")]


def test_parse_trace_returns_none_without_a_dockerfile_boundary():
    assert parse_trace('{"event": "cycle_tool", "tool_name": "linux_terminal", "tool_args": {"command": "ls"}}') is None
    assert parse_trace("") is None


def test_parse_trace_survives_a_truncated_tail_line():
    steps = parse_trace(_JSONL + '\n{"event": "cycle_tool", "tool_na')
    assert len(steps) == 3


def test_render_replay_writes_files_in_place_with_a_heredoc():
    out = render_replay(parse_trace(_JSONL))
    assert "cat > '/app/r/conftest.py' <<'EA_EOF'" in out
    assert "collect_ignore = []" in out
    # ...ordered between the commands that surround it, in one script so `cd`/venv state carries.
    assert out.index("python -m venv") < out.index("conftest.py") < out.index("pip install -e")
    assert "set +e" in out and "set -e\n" not in out


def test_render_replay_picks_a_delimiter_the_payload_cannot_close():
    steps = [("file", "/a", "line\nEA_EOF\nmore")]
    out = render_replay(steps)
    assert "<<'EA_EOF_1'" in out and out.rstrip().endswith("EA_EOF_1")
