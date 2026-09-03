# producers/executionagent.py
#
# The ExecutionAgent SYNTHESIZER producer (https://github.com/sola-st/ExecutionAgent, ISSTA'25).
#
# ExecutionAgent is NOT a Dockerfile-emitting agent in our sense. Its own prompt (see
# prompt_files/steps_list.json) tells it: "Avoid including build and test steps as part of the
# Dockerfile to reduce errors during image creation." So the Dockerfile it writes is only a BASE
# (`WORKDIR /app` + `git clone` + a few apt packages); every dependency install then happens as
# live `linux_terminal` calls inside the running container. On success EA replays that as
#   success_artifacts/{Dockerfile, commands.sh, launch.sh}
# where launch.sh = `docker build` + `docker exec bash -l /tmp/commands.sh`.
#
# Rebuilding EA's Dockerfile ALONE therefore measures a repo with NO dependencies installed — a
# guaranteed EBSR-0 that says nothing about the agent. `synthesize` folds the two halves back into
# ONE image (the same env launch.sh reaches) and re-homes /app/<project> to /testbed, which is
# where bench runs pytest. The transform is PURE + unit-tested; running EA itself is live-only.
from __future__ import annotations

import json
import os
import re
import subprocess
import time

from producers.base import ProduceContext, ProducedEnv, inject_clone_pin

# bench.schema is on the path via producers.base's shim (imported above).
from bench.schema import RepoSpec  # noqa: E402

COMMANDS_BASENAME = "ea_commands.sh"
# Wall clock for the replay layer. commands.sh is EA's *whole* transcript, so it re-runs the test
# suite during the build; on a big repo that is the slowest step in the packet.
REPLAY_TIMEOUT_S = 2400


# ── The pure synthesize transform ────────────────────────────────────────────────────────────
#
# `_REHOME` finds the clone by looking for a git worktree under /app (EA's documented WORKDIR)
# and falls back to a shallow `find` when the agent picked somewhere else. It runs BEFORE any
# `mv`-into-/testbed can be skipped: an EA Dockerfile that already homes at /testbed exits early.
# /testbed must be a REAL dir (a symlink defeats bench's `find -P` guard); the reverse symlink
# keeps the venv/editable-install paths that commands.sh baked in resolving.
_REHOME = (
    "# bench re-home: EA homes the clone at /app/<project>; bench measures /testbed.\n"
    "RUN set -e; \\\n"
    "    if [ -d /testbed/.git ]; then exit 0; fi; \\\n"
    "    d=\"\"; \\\n"
    "    for c in /app/*/; do if [ -d \"$c.git\" ]; then d=\"${c%/}\"; break; fi; done; \\\n"
    "    if [ -z \"$d\" ]; then \\\n"
    "      g=$(find / -maxdepth 4 -type d -name .git -not -path '/proc/*' 2>/dev/null | head -n1); \\\n"
    "      d=\"${g%/.git}\"; \\\n"
    "    fi; \\\n"
    "    test -n \"$d\" && test \"$d\" != \"/\"; \\\n"
    "    mv \"$d\" /testbed && ln -sfn /testbed \"$d\"\n"
    # EA's python guidelines tell it to build a venv at the project root. Naming it here is what
    # makes bench's `python -m pytest` resolve the installed deps; a missing dir on PATH is inert,
    # so this is a no-op for the system-python runs.
    "ENV VIRTUAL_ENV=/testbed/.venv\n"
    "ENV PATH=/testbed/.venv/bin:$PATH\n"
    "WORKDIR /testbed\n"
)

_SET_E = re.compile(r"^\s*set -e.*$", re.MULTILINE)


# ── Rebuilding the post-container sequence from run.jsonl ────────────────────────────────────
#
# EA's own success_artifacts/commands.sh is INCOMPLETE: exit_artifacts._extract_container_commands
# harvests `linux_terminal` calls ONLY, but after the build `write_to_file` writes straight INTO
# the container (tools.py appends those as `(target, "container", dest, content)`). Every patch,
# conftest.py, config or .env the agent authored in-container is therefore missing from
# commands.sh — replaying it alone silently drops files the tests need, which lands as a false
# EBSR-0 charged to the agent.
#
# run.jsonl logs every cycle's `tool_name` + full `tool_args` IN ORDER, so the real sequence is
# recoverable: take everything after the LAST Dockerfile write (once a container is running EA
# REFUSES further Dockerfile writes, so the last one is the build that succeeded) and replay
# terminal commands and file writes interleaved, as they actually happened.
_DOCKERFILE_ARG_KEYS = ("file_path", "path", "filename")
_CONTENT_ARG_KEYS = ("content", "text")


def _arg(args: dict, keys: tuple) -> str | None:
    for k in keys:
        v = args.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def parse_trace(jsonl_text: str) -> list | None:
    """Ordered post-container steps from EA's run.jsonl, as ``[("cmd", str) | ("file", path,
    content)]``. Returns None when the log has no usable Dockerfile boundary, so the caller can
    fall back to EA's own commands.sh."""
    events = []
    for line in (jsonl_text or "").splitlines():
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue                                  # a truncated tail line is not fatal
        if rec.get("event") != "cycle_tool":
            continue
        args = rec.get("tool_args")
        events.append((rec.get("tool_name"), args if isinstance(args, dict) else {}))

    boundary = None
    for i, (tool, args) in enumerate(events):
        target = _arg(args, _DOCKERFILE_ARG_KEYS) or ""
        if tool == "write_to_file" and os.path.basename(target).lower().startswith("dockerfile"):
            boundary = i                              # keep the LAST one
    if boundary is None:
        return None

    steps: list = []
    for tool, args in events[boundary + 1:]:
        if tool == "linux_terminal":
            cmd = args.get("command")
            if isinstance(cmd, str) and cmd.strip():
                steps.append(("cmd", cmd))
        elif tool == "write_to_file":
            path = _arg(args, _DOCKERFILE_ARG_KEYS)
            content = _arg(args, _CONTENT_ARG_KEYS)
            if path and content is not None:
                steps.append(("file", path, content))
    return steps


def _heredoc(content: str) -> tuple:
    """A heredoc delimiter that does not occur as a line of `content`, plus the content. Quoted at
    the call site, so nothing in the payload is expanded."""
    tag = "EA_EOF"
    lines = content.splitlines()
    n = 0
    while tag in lines:
        n += 1
        tag = f"EA_EOF_{n}"
    return tag, content


def render_replay(steps: list, project_dir_hint: str = "") -> str:
    """Render ordered `steps` as ONE bash script: terminal commands verbatim, file writes as
    heredocs (with their parent dir created first) at the position they actually occurred.

    Written as a single script so `cd`s and venv activations carry across steps exactly as they did
    in EA's persistent screen session. `set +e`, not `set -e`: in the real run each tool call was
    independent and a failure never stopped the next command, so aborting on the first failed
    exploratory probe would be LESS faithful, not more (EA's own generator gets this wrong)."""
    out = ["#!/usr/bin/env bash", "# reconstructed from run.jsonl (commands + in-container file "
           "writes, in order)", "set +e", ""]
    if project_dir_hint:
        out += [f"cd {project_dir_hint} 2>/dev/null || true", ""]
    for step in steps:
        if step[0] == "cmd":
            out += [step[1], ""]
        else:
            _, path, content = step
            tag, body = _heredoc(content)
            out += [f"mkdir -p \"$(dirname '{path}')\" 2>/dev/null",
                    f"cat > '{path}' <<'{tag}'", body, tag, ""]
    return "\n".join(out) + "\n"


def sanitize_commands(commands_sh: str) -> str:
    """Make EA's commands.sh safe to replay as ONE docker build layer.

    EA records EVERY `linux_terminal` call after container creation — including the exploratory
    ones that failed — and the generator prepends `set -e`. Replayed verbatim that aborts at the
    first failed probe and loses every install after it, so `set -e` is flipped to `set +e`. The
    caller still runs the script under `|| true`: a non-zero transcript must not fail the build,
    because the env it leaves behind is exactly what we want to measure. A missing/empty script
    yields a no-op script rather than an empty RUN."""
    body = _SET_E.sub("set +e", commands_sh or "").strip()
    if not body:
        return "#!/usr/bin/env bash\nset +e\n: no commands recorded\n"
    if not body.startswith("#!"):
        body = "#!/usr/bin/env bash\n" + body
    if "set +e" not in body:
        body = body.replace("\n", "\nset +e\n", 1)
    return body + "\n"


def synthesize(dockerfile: str, commands_sh: str | None,
               timeout_s: int = REPLAY_TIMEOUT_S) -> tuple:
    """Fold EA's (Dockerfile, commands.sh) pair into ONE conforming Dockerfile.

    Returns ``(dockerfile, setup_scripts)`` where `setup_scripts` is the {basename: content} dict
    `write_env_packet` lands beside the Dockerfile as the build context (bench.harvest re-reads
    them from the COPY lines). `commands_sh=None` (the forced-exit artifacts, which ship no
    transcript) emits the re-home only — that Dockerfile is meant to be self-contained.
    """
    out = dockerfile.rstrip() + "\n"
    scripts: dict = {}
    if commands_sh is not None:
        scripts[COMMANDS_BASENAME] = sanitize_commands(commands_sh)
        out += (
            f"# bench replay: EA installs deps live via linux_terminal, not in its Dockerfile.\n"
            f"COPY {COMMANDS_BASENAME} /tmp/{COMMANDS_BASENAME}\n"
            f"RUN timeout {timeout_s} bash /tmp/{COMMANDS_BASENAME} "
            f"> /tmp/ea_commands.log 2>&1 || true\n"
        )
    return out + _REHOME, scripts


# ── Telemetry (EA persists no token/cost totals; these are what IS on disk) ───────────────────
def _harvest_economy(run_dir: str) -> dict:
    """LLM calls + cycles from the run dir. EA never writes token or cost totals anywhere, so
    those stay absent rather than guessed. Anti-vanish: never raises."""
    econ: dict = {}
    try:
        chats = os.path.join(run_dir, "cycles_chats")
        cycles = [d for d in os.listdir(chats) if d.startswith("cycle_")]
        econ["turns_used"] = len(cycles)
        econ["llm_calls"] = sum(
            len([f for f in os.listdir(os.path.join(chats, d)) if f.endswith(".json")])
            for d in cycles)
    except OSError:
        pass
    return econ


def _read_artifacts(run_dir: str) -> tuple:
    """Return ``(dockerfile, commands_sh, note)`` from EA's run dir.

    Prefers ``success_artifacts/`` (the agent called goals_accomplished). Falls back to
    ``forced_exit_cycle/Dockerfile`` — the budget-exhausted best effort, which ships no command
    transcript and is meant to stand alone; it is measured, but flagged in the note so a
    forced-exit row is never silently read as a success. ``(None, None, why)`` when neither exists.
    """
    ok = os.path.join(run_dir, "success_artifacts", "Dockerfile")
    if os.path.isfile(ok):
        with open(ok) as fh:
            df = fh.read()
        # Prefer the run.jsonl reconstruction (commands AND in-container file writes, interleaved);
        # fall back to EA's own commands.sh, which drops every file write. See parse_trace.
        steps = None
        try:
            with open(os.path.join(run_dir, "run.jsonl"), encoding="utf-8", errors="replace") as fh:
                steps = parse_trace(fh.read())
        except OSError:
            steps = None
        if steps is not None:
            n_files = sum(1 for s in steps if s[0] == "file")
            note = f"replayed from run.jsonl ({len(steps)} steps, {n_files} file writes)"
            return df, render_replay(steps), note
        cmds = ""
        cmds_path = os.path.join(run_dir, "success_artifacts", "commands.sh")
        if os.path.isfile(cmds_path):
            with open(cmds_path) as fh:
                cmds = fh.read()
        return df, cmds, "replayed from commands.sh (no run.jsonl; in-container file writes lost)"
    forced = os.path.join(run_dir, "forced_exit_cycle", "Dockerfile")
    if os.path.isfile(forced):
        with open(forced) as fh:
            return fh.read(), None, "forced_exit artifacts (budget exhausted; best effort)"
    return None, None, "ExecutionAgent produced no Dockerfile (no success_artifacts, no forced_exit_cycle)"


# ── The live-only real runner ────────────────────────────────────────────────────────────────
def run_executionagent(repo: RepoSpec, ctx: ProduceContext, *, llm: str | None,
                       budget: int) -> dict:
    """LIVE-ONLY: drive `python -m execution_agent.main` for ONE repo and read its artifacts.

    EA is a separate checkout with its own venv (it needs Python >=3.10 and litellm) — point
    $EXECUTIONAGENT_ROOT at it and, if its deps are not in the runner's venv,
    $EXECUTIONAGENT_PYTHON at that venv's python. `--run-log-dir` pins the run dir so we don't
    have to guess EA's `<project>/<timestamp>/` path. Raises on failure; the producer wraps it for
    the anti-vanish invariant. Never exercised by unit tests (they inject a stub runner).
    """
    root = ctx.agent_root or os.environ.get("EXECUTIONAGENT_ROOT")
    if not root or not os.path.isdir(root):
        raise RuntimeError(
            "ExecutionAgent checkout not found: set EXECUTIONAGENT_ROOT to a clone of "
            "https://github.com/sola-st/ExecutionAgent (pip install -e . in its own venv).")
    python = os.environ.get("EXECUTIONAGENT_PYTHON") or os.path.join(root, "venv", "bin", "python")
    if not os.path.isfile(python):
        import sys
        python = sys.executable
    # litellm reads the provider key from the environment (OPENROUTER_API_KEY for an
    # `openrouter/...` slug); EA additionally *requires* a non-empty --api-key before it starts.
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("no OPENROUTER_API_KEY / OPENAI_API_KEY for ExecutionAgent")

    out_dir = os.path.join(ctx.workdir, "output", repo.full_name)
    workspace = os.path.join(out_dir, "ea_workspace")
    run_dir = os.path.join(workspace, "_run_logs", "run")
    os.makedirs(workspace, exist_ok=True)
    meta_path = os.path.join(out_dir, "ea_meta.json")
    with open(meta_path, "w") as fh:
        json.dump({
            "project_path": repo.full_name.split("/")[-1],
            "project_name": repo.full_name,
            "project_url": repo.repo_url,
            "language": (repo.language or "python").capitalize(),
            "budget": budget,
        }, fh, indent=2)

    model = llm or "gpt-4o-mini"
    proc = subprocess.run(
        [python, "-m", "execution_agent.main",
         "--experiment-file", meta_path,
         "--model", model, "--knowledge-model", model,
         "--api-key", key,
         "--workspace-root", workspace,
         "--run-log-dir", run_dir],
        cwd=root, timeout=ctx.timeout, capture_output=True, text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"})
    try:
        with open(os.path.join(out_dir, "ea_stdout.log"), "w", encoding="utf-8") as fh:
            fh.write((proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""))
    except OSError:
        pass   # telemetry must never fail a paid-for run

    dockerfile, commands_sh, note = _read_artifacts(run_dir)
    return {"dockerfile": dockerfile, "commands_sh": commands_sh, "note": note,
            "economy": _harvest_economy(run_dir)}


class ExecutionAgentProducer:
    """Synthesizer producer: run ExecutionAgent, then fold its Dockerfile + commands.sh into one
    conforming image homed at /testbed."""
    name = "executionagent"
    needs_llm = True
    measurable = True
    conformance = "synthesized"

    def __init__(self, llm: str | None = None, num_turn: int = 40, runner=None):
        self.llm = llm
        self.num_turn = num_turn
        self._runner = runner   # injectable for tests; None => the real live run_executionagent

    def produce(self, repo: RepoSpec, ctx: ProduceContext) -> ProducedEnv:
        # Anti-vanish invariant (design §1): the ENTIRE body is guarded so an EA crash yields a
        # ProducedEnv(status="error"), never a raised exception.
        start = time.time()
        try:
            runner = self._runner or run_executionagent
            budget = ctx.num_turn if ctx.num_turn is not None else self.num_turn
            res = runner(repo, ctx, llm=(ctx.llm or self.llm), budget=budget)

            economy = dict(res.get("economy") or {})
            economy.setdefault("produce_s", round(time.time() - start, 2))
            raw = res.get("dockerfile")
            note = res.get("note") or ""
            if not raw:
                return ProducedEnv(repo=repo, dockerfile=None, status="error",
                                   note=note or "ExecutionAgent produced no Dockerfile",
                                   conformance=self.conformance, producer_name=self.name,
                                   economy=economy)

            # EA's Dockerfile clones the default branch HEAD, so the MEASURED build would drift off
            # the dataset SHA. Pin it before the replay layer (commands.sh installs against the
            # checkout). A miss is surfaced, never silently dropped.
            if repo.commit:
                raw, injected = inject_clone_pin(raw, repo.commit, repo.repo_url)
                if not injected:
                    economy["pin_warning"] = "no git clone instruction to pin"
                    note = (note + "; " if note else "") + "unpinned clone"

            dockerfile, scripts = synthesize(raw, res.get("commands_sh"))
            base = re.search(r"^\s*FROM\s+(\S+)", dockerfile, re.MULTILINE)
            return ProducedEnv(repo=repo, dockerfile=dockerfile, setup_scripts=scripts,
                               base_image=(base.group(1) if base else None),
                               head_sha=repo.commit or "",
                               status="produced", conformance=self.conformance,
                               producer_name=self.name, economy=economy, note=note)
        except Exception as exc:                    # noqa: BLE001 — boundary guard, never propagate
            return ProducedEnv(repo=repo, dockerfile=None, status="error", note=repr(exc),
                               conformance=self.conformance, producer_name=self.name,
                               economy={"produce_s": round(time.time() - start, 2)})
