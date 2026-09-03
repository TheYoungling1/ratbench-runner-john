# producers/setupx.py
#
# The `setupx` producer: OpenDataBox/SetupX (XPU knowledge store + speculative execution +
# prosecutor/judge verification) as a baseline arm.
#
# SetupX emits NO Dockerfile — it mutates a live container and writes a JSON report. What it does
# record is `setup.history`: every shell command it ran, in order. This producer replays that
# history into one build layer on top of a pinned clone and re-homes /workspace/repo -> /testbed,
# so the arm gets a real EBSR/ESSR row comparable with repo2run / executionagent / dockeragent.
# Same shape as producers/executionagent.py, different trace format.
#
# The replay is NOT a linear `for cmd in history` — SetupX keeps a LIFO stack of `docker commit`
# checkpoints and rolls back to them, so work the agent undid must be undone in the replay too.
# `plan_replay` mirrors EnvironmentManager.rollback_to_checkpoint exactly; see the tests.
from __future__ import annotations

from producers.base import ProduceContext, ProducedEnv

# bench.schema is on the path via producers.base's shim (imported above).
from bench.schema import RepoSpec  # noqa: E402


def _truncate(steps: list, mark: int) -> None:
    """Undo the run-steps after `mark` — but KEEP the env-steps.

    Upstream's rollback recreates the container from the checkpoint image with
    `environment=self._env_vars.copy()`: the CURRENT dict, which `set_env` only ever adds to and
    nothing ever restores. A SET_ENV issued after a checkpoint therefore SURVIVES the rollback —
    only filesystem state is undone. Marks still on the stack were pushed at or before `mark`, so
    they stay valid.
    """
    steps[mark:] = [s for s in steps[mark:] if s[0] == "env"]


def plan_replay(history: list | None) -> tuple:
    """Turn SetupX's `setup.history` into the steps that survive its rollbacks.

    Returns ``(steps, lossy)``. `steps` is a list of ``("run", command)`` / ``("env", key, value)``
    tuples in execution order. `lossy` is True when a surviving XPU trial succeeded but recorded no
    command (the agent fell back to atom-rendered `suggestion.commands`, which `AgentAction.to_dict`
    drops) — the caller surfaces that as `unreplayed`.

    Mirrors upstream exactly: `marks` is EnvironmentManager's `_history_snapshots` stack, holding
    the step index each checkpoint was taken at. `initial_clone` is the mark at 0, pushed by
    `_setup_container` after the clone; every TRY_XPU_SUGGESTION pushes another before its trial.
    """
    steps: list = []
    marks: list = [0]                      # the "initial_clone" checkpoint
    lossy = False
    for entry in history or []:
        action = (entry or {}).get("action") or {}
        result = (entry or {}).get("result") or {}
        content = action.get("content") or {}
        kind = action.get("action_type")

        if kind == "SHELL_COMMAND":
            command = content.get("command")
            if command:
                steps.append(("run", command))

        elif kind == "SET_ENV":
            key = content.get("env_key")
            if key:
                value = content.get("env_value")
                steps.append(("env", key, "" if value is None else str(value)))

        elif kind == "TRY_XPU_SUGGESTION":
            # FOUR outcomes, and they differ in what they leave on the snapshot stack.
            stdout = result.get("stdout") or ""
            if stdout.startswith("[XPU BLOCKED]"):
                pass                       # returns BEFORE create_checkpoint; pushes no frame
            else:
                marks.append(len(steps))   # agent.py takes step_<n>_pre_xpu before the trial
                if stdout.startswith("[XPU SUCCESS]"):
                    command = content.get("command")
                    if command:
                        steps.append(("run", command))
                    else:
                        lossy = True       # atom-rendered commands were never recorded
                elif stdout.startswith("[XPU SKIP]"):
                    pass                   # returns without rolling back; the frame SURVIVES
                else:                      # [XPU FAIL] => auto-rollback of one frame (branch F)
                    _truncate(steps, marks.pop())

        elif kind == "ROLLBACK_ENV":
            # exit_code 1 => upstream found an empty stack and returned False without popping.
            if marks and result.get("exit_code") == 0:
                n = min(max(1, int(content.get("n_frames") or 1)), len(marks))
                for _ in range(n):
                    target = marks.pop()   # restore to the LAST tag popped
                _truncate(steps, target)

        # VERIFY / FINISH carry no command. The VerifierAgent's own commands live in
        # `last_verify_messages`, not here, so a fix it made itself is lost — see the README note.
    return steps, lossy
