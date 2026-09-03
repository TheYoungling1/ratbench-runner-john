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

import shlex

from producers.base import ProduceContext, ProducedEnv

# bench.schema is on the path via producers.base's shim (imported above).
from bench.schema import RepoSpec  # noqa: E402

DEFAULT_BASE_IMAGE = "python:3.10"         # SetupX's own .env.example default. NOT ubuntu: every
                                           # bench command is `python -m ...` (bench/bench/languages/
                                           # python.py) and ubuntu ships python3 only, with no pip.
WORK_DIR = "/workspace/repo"               # DOCKER_WORK_DIR + "/repo"; where the agent worked
REPLAY_BASENAME = "setupx_replay.sh"


def _as_dict(value) -> dict:
    """`history` is another agent's JSON report — a boundary, so nothing here is assumed."""
    return value if isinstance(value, dict) else {}


def _as_int(value, default: int) -> int:
    """Coerce a JSON number-that-might-be-a-string; `"0"` must not read as a non-zero exit code."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
    for entry in history if isinstance(history, list) else []:
        action = _as_dict(_as_dict(entry).get("action"))
        result = _as_dict(_as_dict(entry).get("result"))
        content = _as_dict(action.get("content"))
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
                        # Atom-rendered commands were never recorded. Park a sentinel rather than
                        # setting a flag: a later rollback can discard this very trial, and then the
                        # replay IS faithful. `_truncate` drops sentinels exactly as it drops runs.
                        steps.append(("lossy",))
                elif stdout.startswith("[XPU SKIP]"):
                    pass                   # returns without rolling back; the frame SURVIVES
                else:                      # [XPU FAIL] => auto-rollback of one frame (branch F)
                    _truncate(steps, marks.pop())

        elif kind == "ROLLBACK_ENV":
            # exit_code 1 => upstream found an empty stack and returned False without popping.
            # A missing code reads as success: skipping a rollback the agent really made bakes in
            # work it undid, and that ships as a plausible-looking Dockerfile instead of an error.
            if marks and _as_int(result.get("exit_code"), 0) == 0:
                n = min(max(1, _as_int(content.get("n_frames"), 1)), len(marks))
                for _ in range(n):
                    target = marks.pop()   # restore to the LAST tag popped
                _truncate(steps, target)

        # VERIFY / FINISH carry no command. The VerifierAgent's own commands live in
        # `last_verify_messages`, not here, so a fix it made itself is lost — see the README note.

    # Only sentinels that SURVIVED the rollbacks count; strip them so the caller sees just the two
    # documented step shapes.
    lossy = any(s[0] == "lossy" for s in steps)
    return [s for s in steps if s[0] != "lossy"], lossy


def _clone_lines(repo: RepoSpec) -> str:
    """The pinned clone, in the exact shape the README's commit-pinning invariant prescribes."""
    if not repo.commit:
        # No dataset SHA: shallow-clone the default branch, same as SetupX's own bootstrap.
        return f"RUN git clone --depth=1 {repo.repo_url} {WORK_DIR}\n"
    return (f"RUN git clone {repo.repo_url} {WORK_DIR} \\\n"
            f" && git -C {WORK_DIR} fetch --depth 1 origin {repo.commit} \\\n"
            f" && git -C {WORK_DIR} checkout --detach {repo.commit}\n")


def render_replay(steps: list) -> str:
    """Render the surviving steps as ONE bash script.

    `set +e` because the agent's own run continued past non-zero exits — a `set -e` replay would
    abort the build on the first probe command upstream shrugged off. Each step re-enters the work
    dir because upstream ran every command in its own `bash -c "cd /workspace/repo && ..."`.
    """
    lines = ["#!/usr/bin/env bash",
             "# SetupX replay: the agent's surviving trajectory, folded into one build layer.",
             "set +e",
             ""]
    for step in steps:
        if step[0] == "env":
            lines.append(f"export {step[1]}={shlex.quote(step[2])}")
        else:
            lines.append(f"cd {WORK_DIR} && {step[1]}")
    # ponytail: no per-step timeout; bench's build_timeout bounds the whole layer. Add one
    # (upstream's DOCKER_TIMEOUT is 300s) if a hung step ever becomes a real failure mode.
    lines += ["", "exit 0"]
    return "\n".join(lines) + "\n"


def render_dockerfile(repo: RepoSpec, steps: list, *,
                      base_image: str = DEFAULT_BASE_IMAGE) -> tuple:
    """Render the conforming Dockerfile + its COPY siblings. Returns ``(dockerfile, scripts)``."""
    # A newline inside an ENV value breaks the instruction (ENV has no line continuation). The
    # `export` in the replay script still carries it, so drop only the ENV line.
    env_lines = "".join(f"ENV {k}={shlex.quote(v)}\n"
                        for _, k, v in (s for s in steps if s[0] == "env")
                        if "\n" not in v)
    df = (
        f"FROM {base_image}\n"
        "RUN apt-get update \\\n"
        " && apt-get install -y --no-install-recommends git ca-certificates \\\n"
        " && rm -rf /var/lib/apt/lists/*\n"
        + _clone_lines(repo) +
        f"WORKDIR {WORK_DIR}\n"
        + env_lines +
        f"COPY {REPLAY_BASENAME} /tmp/{REPLAY_BASENAME}\n"
        f"RUN bash /tmp/{REPLAY_BASENAME}\n"
        # No pytest guard here: bench runs its own `_ENSURE` (`python -m pip install -q pytest
        # pytest-timeout`) before the gate, and it needs `python` on PATH — which is why the base
        # image is python:3.10 and not ubuntu.
        # bench compares `git -C /testbed rev-parse --show-toplevel` to "/testbed"
        # (bench/bench/contract.py): a symlinked /testbed resolves to its physical path and is
        # classified non_conforming. So /testbed is the real dir; the reverse symlink keeps
        # venv/editable-install paths the replay baked in resolving.
        f"RUN mv {WORK_DIR} /testbed && ln -sfn /testbed {WORK_DIR}\n"
        # The probe is `git -C /testbed rev-parse --show-toplevel`, which fails closed to
        # non_conforming when git refuses the directory as dubiously owned.
        "RUN git config --global --add safe.directory /testbed\n"
        # ponytail: two guessed venv names. The agent may create a venv anywhere; a non-existent
        # PATH entry is harmless. Parse the replay for the real path if this ever misses.
        "ENV PATH=/testbed/.venv/bin:/testbed/venv/bin:$PATH\n"
        "WORKDIR /testbed\n"
    )
    return df, {REPLAY_BASENAME: render_replay(steps)}
