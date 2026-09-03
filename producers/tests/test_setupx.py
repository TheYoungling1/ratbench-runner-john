# producers/tests/test_setupx.py — the SetupX trajectory transforms + SetupXProducer.
#
# SetupX is STUBBED (injected via runner=) — no SetupX checkout, no docker, no keys, no Postgres.
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
    assert df.rstrip().endswith("WORKDIR /testbed") or "WORKDIR /testbed" in df
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
    heredoc = "cat > conftest.py <<'PY'\nimport sys\nPY"
    _, scripts = render_dockerfile(_repo(), [("run", heredoc)])
    assert heredoc in scripts[REPLAY_BASENAME]


def test_the_base_image_provides_the_python_binary_bench_invokes():
    # Every bench command is `python -m ...` (bench/bench/languages/python.py); ubuntu:22.04 ships
    # python3 only and no pip, which would fail the gate for a reason that is not the environment.
    df, _ = render_dockerfile(_repo(), [("run", "true")])
    assert df.startswith("FROM python:3.10\n")
