# producers/tests/test_setupx.py — the SetupX trajectory transforms + SetupXProducer.
#
# SetupX is STUBBED (injected via runner=) — no SetupX checkout, no docker, no keys, no Postgres.
from producers.setupx import plan_replay


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
