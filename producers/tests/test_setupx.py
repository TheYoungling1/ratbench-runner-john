# producers/tests/test_setupx.py — the SetupX trajectory transforms + SetupXProducer.
#
# SetupX is STUBBED (injected via runner=) — no SetupX checkout, no docker, no keys, no Postgres.
import shlex

from producers.base import RepoSpec
from producers.setupx import REPLAY_BASENAME, plan_replay, render_dockerfile


def _shell(cmd, exit_code=0):
    return {"action": {"thought": "", "action_type": "SHELL_COMMAND",
                       "content": {"command": cmd}},
            "result": {"exit_code": exit_code, "stdout": "", "stderr": ""}}


def _set_env(key, value):
    return {"action": {"thought": "", "action_type": "SET_ENV",
                       "content": {"env_key": key, "env_value": value}},
            "result": {"exit_code": 0, "stdout": f"[SET_ENV] {key}={value}", "stderr": ""}}


def _xpu(cmd, ok=True, sid="xpu_1"):
    outcome = "SUCCESS" if ok else "FAIL"
    return {"action": {"thought": "", "action_type": "TRY_XPU_SUGGESTION",
                       "content": {"xpu_suggestion_id": sid, "command": cmd, "reasoning": ""}},
            "result": {"exit_code": 0, "stdout": f"[XPU {outcome}] {sid}\n", "stderr": ""}}


def _rollback(n_frames=1, ok=True):
    return {"action": {"thought": "", "action_type": "ROLLBACK_ENV",
                       "content": {"n_frames": n_frames}},
            "result": {"exit_code": 0 if ok else 1, "stdout": "[ROLLBACK_ENV] ok", "stderr": ""}}


def _verify():
    return {"action": {"thought": "", "action_type": "VERIFY", "content": {"hint": "run pytest"}},
            "result": {"exit_code": 0, "stdout": "collected 12 items", "stderr": ""}}


def test_shell_commands_replay_in_order():
    steps, lossy = plan_replay([_shell("pip install -e ."), _shell("pip install pytest")])
    assert steps == [("run", "pip install -e ."), ("run", "pip install pytest")]
    assert lossy is False


def test_failed_commands_are_kept_because_the_agent_kept_them():
    # SetupX does not undo a non-zero command; only ROLLBACK_ENV and a failed XPU trial undo state.
    # Dropping it here would diverge from the container the agent actually finished with.
    steps, _ = plan_replay([_shell("apt-get install -y libfoo", exit_code=100),
                            _shell("apt-get install -y libfoo-dev")])
    assert steps == [("run", "apt-get install -y libfoo"),
                     ("run", "apt-get install -y libfoo-dev")]


def test_verify_and_finish_entries_contribute_no_steps():
    steps, _ = plan_replay([_shell("pip install pytest"), _verify()])
    assert steps == [("run", "pip install pytest")]


def test_set_env_survives_as_its_own_step():
    steps, _ = plan_replay([_set_env("PIP_NO_CACHE_DIR", "1"), _shell("pip install -e .")])
    assert steps == [("env", "PIP_NO_CACHE_DIR", "1"), ("run", "pip install -e .")]


def test_a_successful_xpu_trial_is_replayed():
    steps, lossy = plan_replay([_xpu("apt-get install -y libpq-dev", ok=True)])
    assert steps == [("run", "apt-get install -y libpq-dev")]
    assert lossy is False


def test_a_failed_xpu_trial_is_dropped_because_the_agent_auto_rolled_back():
    steps, _ = plan_replay([_shell("pip install -e ."),
                            _xpu("pip install broken-pkg", ok=False),
                            _shell("pip install pytest")])
    assert steps == [("run", "pip install -e ."), ("run", "pip install pytest")]


def test_rollback_truncates_back_to_the_checkpoint_the_trial_created():
    # ROLLBACK_ENV(1) pops the surviving pre-XPU checkpoint and restores to it, discarding the
    # trial's own command and everything after it.
    steps, _ = plan_replay([_shell("a"), _xpu("b", ok=True), _shell("c"), _rollback(1)])
    assert steps == [("run", "a")]


def test_rollback_of_more_frames_than_exist_lands_on_the_initial_clone():
    steps, _ = plan_replay([_shell("a"), _shell("b"), _rollback(5)])
    assert steps == []


def test_a_rollback_that_upstream_reported_as_failed_changes_nothing():
    steps, _ = plan_replay([_shell("a"), _rollback(1, ok=False)])
    assert steps == [("run", "a")]


def test_env_set_before_a_rollback_survives_it():
    # EnvironmentManager._env_vars is never snapshotted: rollback_to_checkpoint recreates the
    # container with `environment=self._env_vars.copy()`, the CURRENT dict. Only files are undone.
    steps, _ = plan_replay([_xpu("b", ok=True), _set_env("CFLAGS", "-O0"), _rollback(1)])
    assert steps == [("env", "CFLAGS", "-O0")]


def _xpu_skip(sid="xpu_1"):
    # The suggestion had no executable command: agent.py records this AFTER taking the checkpoint
    # and returns WITHOUT rolling back, so the frame stays on the stack.
    return {"action": {"thought": "", "action_type": "TRY_XPU_SUGGESTION",
                       "content": {"xpu_suggestion_id": sid, "command": None, "reasoning": ""}},
            "result": {"exit_code": 1,
                       "stdout": f"[XPU SKIP] {sid}: no commands; not executed", "stderr": ""}}


def _xpu_blocked(sid="xpu_9"):
    # The id was not in the current pool: agent.py returns BEFORE create_checkpoint, no frame.
    return {"action": {"thought": "", "action_type": "TRY_XPU_SUGGESTION",
                       "content": {"xpu_suggestion_id": sid, "command": None, "reasoning": ""}},
            "result": {"exit_code": 1,
                       "stdout": f"[XPU BLOCKED] suggestion {sid} not available", "stderr": ""}}


def test_a_skipped_xpu_trial_keeps_its_checkpoint_so_a_later_rollback_lands_right():
    # Popping the SKIP frame here would make the rollback truncate one frame too far.
    steps, _ = plan_replay([_shell("a"), _xpu_skip(), _shell("b"), _rollback(1)])
    assert steps == [("run", "a")]


def test_a_blocked_xpu_trial_pushes_no_checkpoint_at_all():
    steps, _ = plan_replay([_shell("a"), _xpu_blocked(), _shell("b"), _rollback(1)])
    assert steps == []


def test_a_successful_xpu_trial_with_no_recorded_command_is_flagged_lossy():
    # The agent fell back to atom-rendered suggestion.commands, which to_dict() never recorded.
    entry = _xpu(None, ok=True)
    steps, lossy = plan_replay([_shell("a"), entry])
    assert steps == [("run", "a")]
    assert lossy is True


def test_empty_history_is_empty_not_an_error():
    assert plan_replay([]) == ([], False)
    assert plan_replay(None) == ([], False)


def test_a_lossy_xpu_trial_that_was_rolled_back_is_not_lossy():
    # The unrecorded command never survived, so the replay is faithful and must not say otherwise.
    steps, lossy = plan_replay([_shell("a"), _xpu(None, ok=True), _rollback(1)])
    assert steps == [("run", "a")]
    assert lossy is False


def test_a_later_failed_trial_does_not_undo_an_earlier_lossy_one():
    # The FAIL auto-rollback pops only the frame that trial itself pushed, which sits AFTER the
    # lossy trial's command — so that command survives and the replay really is missing it.
    steps, lossy = plan_replay([_xpu(None, ok=True), _xpu("z", ok=False)])
    assert steps == []
    assert lossy is True


def test_a_deeper_rollback_over_a_lossy_trial_clears_the_flag():
    steps, lossy = plan_replay([_xpu(None, ok=True), _shell("b"), _rollback(2)])
    assert steps == []
    assert lossy is False


def test_a_non_numeric_n_frames_falls_back_to_one_frame():
    steps, lossy = plan_replay([_shell("a"), _rollback("two")])
    assert steps == []
    assert lossy is False


def test_history_that_is_not_a_list_of_entries_yields_no_steps():
    # The report is another agent's JSON; a shape we did not expect must not raise.
    assert plan_replay({"a": 1}) == ([], False)
    assert plan_replay(["oops", None, 7]) == ([], False)
    assert plan_replay([{"action": "not-a-dict", "result": None}]) == ([], False)


def test_a_rollback_whose_exit_code_is_a_string_still_rolls_back():
    # Reading "0" as a non-zero code would silently keep work the agent undid.
    entry = _rollback(1)
    entry["result"]["exit_code"] = "0"
    assert plan_replay([_shell("a"), entry]) == ([], False)


def test_a_rollback_with_no_recorded_exit_code_still_rolls_back():
    entry = _rollback(1)
    del entry["result"]["exit_code"]
    assert plan_replay([_shell("a"), entry]) == ([], False)


def _replayed_commands(scripts):
    """The replayed commands as bash receives them, in order.

    Each is one quoted argument to its own `bash -c`, and a multiline command spans several physical
    lines, so unquote rather than reading the script text.
    """
    chunks = scripts[REPLAY_BASENAME].split("\nbash -c ")[1:]
    return [shlex.split(chunk)[0] for chunk in chunks]


def _repo(commit="abc123"):
    return RepoSpec("o/r", "https://github.com/o/r", commit=commit, language="python")


def test_the_clone_is_pinned_to_the_dataset_sha_before_anything_is_installed():
    df, scripts = render_dockerfile(_repo(), [("run", "pip install -e .")])
    assert "git clone https://github.com/o/r /workspace/repo" in df
    assert "fetch --depth 1 origin abc123" in df
    assert "checkout --detach abc123" in df
    assert df.index("checkout --detach abc123") < df.index(REPLAY_BASENAME)


def test_a_repo_with_no_commit_clones_shallow_without_a_pin():
    df, _ = render_dockerfile(_repo(commit=None), [("run", "true")])
    assert "git clone --depth=1 https://github.com/o/r /workspace/repo" in df
    assert "checkout --detach" not in df


def test_the_repo_is_rehomed_to_testbed_with_a_reverse_symlink():
    # bench probes `git -C /testbed rev-parse --show-toplevel` and compares it to "/testbed"
    # (bench/bench/contract.py); a symlinked /testbed resolves to its physical path, mismatches, and
    # is classified non_conforming. So /testbed must be the real directory; the reverse symlink
    # keeps paths the replay baked in resolving.
    df, _ = render_dockerfile(_repo(), [("run", "true")])
    assert "mv /workspace/repo /testbed" in df
    assert "ln -sfn /testbed /workspace/repo" in df
    assert df.rstrip().endswith("WORKDIR /testbed")
    assert df.index("mv /workspace/repo /testbed") > df.index(REPLAY_BASENAME)


def test_safe_directory_is_declared_for_the_new_worktree_path():
    # bench's contract probe classifies the container by `git -C /testbed rev-parse --show-toplevel`,
    # which fails closed to non_conforming if git refuses the directory as dubiously owned.
    df, _ = render_dockerfile(_repo(), [("run", "true")])
    assert "safe.directory /testbed" in df
    assert df.index("mv /workspace/repo /testbed") < df.index("safe.directory /testbed")


def test_env_steps_become_both_an_export_in_the_replay_and_a_dockerfile_env():
    # export: so later replayed commands see it, as they did under SetupX's `environment=`.
    # ENV: so it is still set in the container bench measures.
    # Use a value that actually needs quoting: shlex.quote("-O0") is a no-op, so asserting on
    # "ENV CFLAGS='-O0'" would fail against a correct renderer.
    df, scripts = render_dockerfile(_repo(), [("env", "CFLAGS", "-O0 -g"), ("run", "pip install -e .")])
    assert "ENV CFLAGS='-O0 -g'" in df
    assert "export CFLAGS='-O0 -g'" in scripts[REPLAY_BASENAME]


def test_a_multiline_env_value_is_left_out_of_the_dockerfile_but_kept_in_the_replay():
    # ENV has no line continuation, so a newline in the value is a Dockerfile parse error. The
    # export in the replay script still carries it, and bash handles it fine.
    df, scripts = render_dockerfile(_repo(), [("env", "PATCH", "line1\nline2")])
    assert "ENV PATCH" not in df
    assert "export PATCH=" in scripts[REPLAY_BASENAME]


def test_every_replayed_command_reenters_the_work_dir_like_upstream_did():
    # Upstream runs each command as `bash -c "cd /workspace/repo && <cmd>"`, so a `cd` in one
    # command never leaked into the next. Re-cd-ing before each step reproduces that.
    _, scripts = render_dockerfile(_repo(), [("run", "cd docs"), ("run", "pip install -e .")])
    body = scripts[REPLAY_BASENAME]
    assert body.count("cd /workspace/repo &&") == 2


def test_the_replay_tolerates_failing_steps():
    # The agent's own run continued past non-zero exits; a `set -e` replay would abort the build on
    # the first probe command that upstream shrugged off.
    _, scripts = render_dockerfile(_repo(), [("run", "false")])
    assert "set +e" in scripts[REPLAY_BASENAME]
    assert scripts[REPLAY_BASENAME].rstrip().endswith("exit 0")


def test_multiline_commands_survive_rendering():
    # The command is quoted into a single `bash -c` argument, so it no longer appears raw in the
    # script text — but it must arrive at bash byte-for-byte. Unquoting the line proves it does.
    heredoc = "cat > conftest.py <<'PY'\nimport sys\nPY"
    _, scripts = render_dockerfile(_repo(), [("run", heredoc)])
    assert _replayed_commands(scripts) == [f"cd /workspace/repo && {heredoc}"]


def test_a_malformed_command_cannot_take_the_rest_of_the_script_with_it():
    # `set +e` survives non-zero exits, not parse errors: a bare line with an unbalanced quote makes
    # bash abort the whole script (swallowing the following steps into its string) and the build
    # fails at `RUN bash /tmp/setupx_replay.sh`. Upstream ran each command in its own
    # `bash -c`, so a broken one failed alone; quoting each command into one argument restores that.
    # A malformed command is a realistic trace member precisely because upstream recorded the
    # commands that failed.
    broken = "echo 'unbalanced"
    _, scripts = render_dockerfile(_repo(), [("run", broken), ("run", "pip install -e .")])
    assert _replayed_commands(scripts) == [f"cd /workspace/repo && {broken}",
                                           "cd /workspace/repo && pip install -e ."]


def test_env_exports_stay_bare_so_they_reach_the_steps_that_follow():
    # The exports are ours and always well-formed, and a subshell would strip them of their point:
    # a multiline value has its ENV line skipped, so the export is the ONLY thing carrying it to
    # later commands.
    _, scripts = render_dockerfile(_repo(), [("env", "PATCH", "line1\nline2"), ("run", "true")])
    assert "\nexport PATCH=" in scripts[REPLAY_BASENAME]


def test_an_env_value_containing_a_dollar_sign_is_not_expanded():
    # The riskiest quoting case: unquoted, Docker and bash would both expand it away.
    df, scripts = render_dockerfile(_repo(), [("env", "PROMPT", "$HOME/x")])
    assert "ENV PROMPT='$HOME/x'" in df
    assert "export PROMPT='$HOME/x'" in scripts[REPLAY_BASENAME]


def test_an_env_key_that_is_not_a_valid_name_never_reaches_the_dockerfile():
    # `env_key` comes from another agent's JSON with only a truthiness check. A newline in the key
    # breaks the instruction outright; a space silently misfires into Docker's legacy `ENV key value`
    # form, where `ENV FOO BAR='x'` sets FOO to "BAR='x'". bash rejects a bad name loudly, so the
    # export can stay.
    df, scripts = render_dockerfile(_repo(), [("env", "FOO BAR", "x"), ("env", "A\nB", "y")])
    assert "ENV FOO BAR" not in df
    assert "ENV A" not in df
    assert "export FOO BAR=x" in scripts[REPLAY_BASENAME]


def test_the_base_image_provides_the_python_binary_bench_invokes():
    # Every bench command is `python -m ...` (bench/bench/languages/python.py); ubuntu:22.04 ships
    # python3 only and no pip, which would fail the gate for a reason that is not the environment.
    df, _ = render_dockerfile(_repo(), [("run", "true")])
    assert df.startswith("FROM python:3.10\n")


import json
import os

import pytest

from producers.base import ProduceContext
from producers.setupx import SetupXProducer, child_env


def _ctx(tmp_path):
    return ProduceContext(llm="deepseek/deepseek-v4-flash", workdir=str(tmp_path), num_turn=9999)


def test_child_env_routes_the_bare_model_name_at_deepseeks_own_endpoint(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    env = child_env("deepseek/deepseek-v4-flash", "setupx-mirror:o__r", "o__r_123", 100)
    assert env["LLM_PROVIDER"] == "openai"
    assert env["OPENAI_BASE_URL"] == "https://api.deepseek.com/v1"
    assert env["OPENAI_API_KEY"] == "sk-test"
    # SetupX POSTs {"model": OPENAI_MODEL}; the litellm-style provider prefix is not a model name.
    assert env["OPENAI_MODEL"] == "deepseek-v4-flash"


def test_child_env_refuses_to_start_without_a_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        child_env("deepseek/deepseek-v4-flash", "setupx-mirror:o__r", "o__r_123", 100)


def test_child_env_freezes_the_experience_store(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("SETUPX_DB_DSN", "postgresql://postgres@localhost:5433/xpu_run")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-embed")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
    env = child_env("deepseek/deepseek-v4-flash", "setupx-mirror:o__r", "o__r_123", 100)
    assert env["FREEZE_TELEMETRY"] == "1"
    assert env["XPU_VECTOR_ENABLED"] == "1"
    # config.py reads `dns` first, XPU_DB_DNS second; set both so neither spelling wins by accident.
    assert env["dns"] == env["XPU_DB_DNS"] == "postgresql://postgres@localhost:5433/xpu_run"


def test_child_env_sends_embeddings_somewhere_other_than_deepseek(monkeypatch):
    # text_to_embedding falls back to OPENAI_API_KEY + OPENAI_BASE_URL, which now point at
    # DeepSeek — and DeepSeek serves no /embeddings route. An unset EMBEDDING_API_KEY would 404
    # every retrieval and silently turn the XPU arm into the no-XPU arm.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("SETUPX_DB_DSN", "postgresql://postgres@localhost:5433/xpu_run")
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="EMBEDDING_API_KEY"):
        child_env("deepseek/deepseek-v4-flash", "setupx-mirror:o__r", "o__r_123", 100)


def test_child_env_namespaces_the_checkpoint_images_per_run(monkeypatch):
    # Without this, two concurrent repos share the `setup_agent_checkpoint` repository: each one's
    # startup deletes the other's snapshots and their step_<n>_pre_xpu tags collide, so a rollback
    # restores the wrong repo's container. Requires tools/setupx-bench.patch.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    env = child_env("deepseek/deepseek-v4-flash", "setupx-mirror:o__r", "o__r_123", 100)
    assert env["SETUPX_CKPT_NS"] == "o__r_123"
    assert env["SETUPX_NETWORK_MODE"] == "bridge"


def test_child_env_passes_the_budget_as_llm_calls_not_steps(monkeypatch):
    # num_turn is a completion budget, matching sweagent_repo2run's per_instance_call_limit. It must
    # NOT be forwarded as --max-steps: a single VERIFY step spawns the verifier's whole ReAct loop,
    # so steps and calls differ by an unbounded factor and would not be comparable across arms.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    env = child_env("deepseek/deepseek-v4-flash", "setupx-mirror:o__r", "o__r_123", 100)
    assert env["SETUPX_MAX_LLM_CALLS"] == "100"


def test_child_env_marks_the_store_read_only_when_a_dsn_is_present(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("SETUPX_DB_DSN", "postgresql://postgres@localhost:5433/xpu_run")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-embed")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
    env = child_env("deepseek/deepseek-v4-flash", "setupx-mirror:o__r", "o__r_123", 100)
    assert env["XPU_READONLY"] == "1"


def test_child_env_points_setupx_at_the_pinned_mirror(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    env = child_env("deepseek/deepseek-v4-flash", "setupx-mirror:o__r", "o__r_123", 100)
    assert env["DOCKER_BASE_IMAGE"] == "setupx-mirror:o__r"
    assert env["DOCKER_WORK_DIR"] == "/workspace"


def test_producer_emits_a_packet_from_a_stubbed_run(tmp_path):
    def stub(repo, ctx, *, llm, num_turn):
        return {"history": [{"action": {"action_type": "SHELL_COMMAND",
                                        "content": {"command": "pip install -e ."}},
                             "result": {"exit_code": 0, "stdout": "", "stderr": ""}}],
                "completed": True, "steps_taken": 7,
                "phase2": {"success": True, "reason": "prosecutor found no substantive issue"},
                "economy": {"turns_used": 7}}

    env = SetupXProducer(llm="deepseek/deepseek-v4-flash", runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.status == "produced"
    assert env.conformance == "synthesized"
    assert env.producer_name == "setupx"
    assert env.head_sha == "abc123"
    assert env.base_image == "python:3.10"
    assert "pip install -e ." in env.setup_scripts[REPLAY_BASENAME]
    assert env.economy["turns_used"] == 7
    assert "produce_s" in env.economy
    assert env.unreplayed is False


def test_producer_still_emits_for_an_unfinished_phase_one(tmp_path):
    # A guilty verdict or an exhausted step budget still leaves a real environment. Scoring it is
    # more honest than recording status="error" and hiding the row. (Reachable only via step
    # exhaustion — a wall-clock timeout writes no report for the producer to read.)
    def stub(repo, ctx, *, llm, num_turn):
        return {"history": [{"action": {"action_type": "SHELL_COMMAND",
                                        "content": {"command": "pip install -e ."}},
                             "result": {"exit_code": 0, "stdout": "", "stderr": ""}}],
                "completed": False, "steps_taken": 40,
                "phase2": {"success": False, "reason": "setup agent timed out"},
                "economy": {}}

    env = SetupXProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.status == "produced"
    assert "timed out" in env.note


def test_producer_reports_an_empty_trajectory_as_an_error_not_an_empty_image(tmp_path):
    def stub(repo, ctx, *, llm, num_turn):
        return {"history": [], "completed": False, "steps_taken": 0,
                "phase2": {"success": None, "reason": "[error] phase 2 execution failed"},
                "economy": {}}

    env = SetupXProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.status == "error"
    assert env.dockerfile is None


def test_producer_never_raises_when_the_runner_blows_up(tmp_path):
    def stub(repo, ctx, *, llm, num_turn):
        raise RuntimeError("docker daemon is not running")

    env = SetupXProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    assert env.status == "error"
    assert "docker daemon" in env.note
    assert env.dockerfile is None


def test_the_written_packet_matches_the_shared_contract(tmp_path):
    def stub(repo, ctx, *, llm, num_turn):
        return {"history": [{"action": {"action_type": "SHELL_COMMAND",
                                        "content": {"command": "pip install -e ."}},
                             "result": {"exit_code": 0, "stdout": "", "stderr": ""}}],
                "completed": True, "steps_taken": 3, "phase2": {"success": True, "reason": "ok"},
                "economy": {}}

    from producers.base import write_env_packet
    out = tmp_path / "out"
    env = SetupXProducer(runner=stub).produce(_repo(), _ctx(tmp_path))
    write_env_packet(str(out), env)
    build = out / "o" / "r" / "eval_build"
    assert (build / "Dockerfile").exists()
    assert (build / REPLAY_BASENAME).exists()
    meta = json.loads((out / "o" / "r" / "_meta.json").read_text())
    assert meta["producer"] == "setupx"
    assert meta["status"] == "produced"
    assert meta["conformance"] == "synthesized"
