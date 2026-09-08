"""Pure, RAT-tree-free helpers for the claudecode-dockerfile PRODUCER.

Folded out of the runner's claudecode model + dockerfile helpers so producers/ never
imports the runner package or RAT model modules (the produce -> measure seam only
crosses via bench.schema). Stdlib-only, so producers stay importable without the RAT tree.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Optional

W = "/testbed"
# ANTHROPIC_API_KEY goes out as `x-api-key`, ANTHROPIC_AUTH_TOKEN as `Authorization: Bearer` —
# DeepSeek's own Claude Code instructions use the latter, so both are accepted.
AUTH_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
ENV_KEYS = AUTH_KEYS + ("ANTHROPIC_BASE_URL",)
# Hosts that ARE Anthropic. A base URL outside this set is a third-party endpoint.
_ANTHROPIC_HOSTS = ("anthropic.com", "claude.ai")
DOCKERFILE_GEN_PATH = "/testbed/Dockerfile.gen"

# The Claude Code CLI accepts model aliases (sonnet/opus/haiku) or full IDs.
_CLAUDE_PREFIXES = ("claude", "sonnet", "opus", "haiku")


def _as_text(v) -> str:
    """Decode subprocess output that may be str (text mode), bytes, or None."""
    if v is None:
        return ""
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else v


def budget_flags() -> list:
    """`--max-budget-usd` flags for the CLI: opt-in, not defaulted.

    The CLI prices every call with the ANTHROPIC rate card, whatever ANTHROPIC_BASE_URL points at,
    so a dollar cap is not a step budget: measured against this repo's own captured telemetry a
    trivial turn costs ~$0.047, i.e. the old $2.00 default truncated at ~43 calls and the 100-call
    cap never bound. The turn cap (num_turn) and the wall clock ARE the budget; the dollar cap is
    now opt-in via CLAUDE_MAX_BUDGET_USD for anyone running on Anthropic auth who wants a backstop.
    """
    budget = (os.environ.get("CLAUDE_MAX_BUDGET_USD") or "").strip()
    return ["--max-budget-usd", budget] if budget else []


# ── DeepSeek settled pricing ─────────────────────────────────────────────────────────────────
#
# MEASURED against api.deepseek.com/anthropic on 2026-09-04, because the CLI's own
# `total_cost_usd` is computed from the ANTHROPIC rate card whatever ANTHROPIC_BASE_URL points at
# — on a DeepSeek-routed run it overstates spend by ~50-100x and is not usable as a cost.
#
# The bridge reports usage in ANTHROPIC field names, and the split is real:
#   cold call: {"input_tokens": 14514, "cache_read_input_tokens": 0}
#   same prefix again: {"input_tokens": 50, "cache_read_input_tokens": 14464}
# So `input_tokens` is the cache-MISS count (it EXCLUDES cache reads, per Anthropic semantics) and
# `cache_read_input_tokens` is the HIT count. `cache_creation_input_tokens` is always 0: DeepSeek
# caches automatically and bills no cache-write, so there are exactly two input tiers, not three.
# An Anthropic `cache_control` block on the request changes nothing (measured; it is ignored).
#
# Rates are $/token OFF-PEAK, from api-docs.deepseek.com/quick_start/pricing (re-read 2026-09-04)
# and agreeing with producers/sweagent_repo2run_runner.py::_DEEPSEEK_DIRECT_COSTS. Peak is exactly
# double, so a run priced with the wrong window is off by 2x and one priced without the cache split
# is off by up to 31x on input — which dominates, since Claude Code's input is ~90% cache reads.
_DEEPSEEK_RATES = {
    "deepseek-v4-flash": {"hit": 7e-9, "miss": 2.2e-7, "out": 6.6e-7},
    "deepseek-v4-pro": {"hit": 2.2e-8, "miss": 6.6e-7, "out": 1.98e-6},
}
# Vendor: "Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC, Monday through Friday."
_PEAK_HOURS = frozenset({1, 2, 3, 6, 7, 8, 9})


def is_peak(when=None) -> bool:
    """True inside DeepSeek's peak window, where every rate doubles."""
    import datetime
    t = when or datetime.datetime.now(datetime.timezone.utc)
    return t.weekday() < 5 and t.hour in _PEAK_HOURS


def deepseek_cost(model: str, miss_tokens, hit_tokens, out_tokens, when=None):
    """Settled DeepSeek cost in USD, or None when the model is unpriced or tokens are missing.

    Never returns 0.0 as a fallback: a missing cost must not read as a free run.
    """
    rate = _DEEPSEEK_RATES.get(_deepseek_model(model))
    if rate is None or miss_tokens is None or out_tokens is None:
        return None
    mult = 2.0 if is_peak(when) else 1.0
    return mult * (_as_int(miss_tokens) * rate["miss"]
                   + _as_int(hit_tokens) * rate["hit"]
                   + _as_int(out_tokens) * rate["out"])


def _deepseek_model(model: str) -> str:
    """Which DeepSeek model actually SERVED a request.

    Measured: the bridge maps claude ids by family — `claude-sonnet-*` and `claude-haiku-*` are
    both served by deepseek-v4-flash, `claude-opus-*` by deepseek-v4-pro — and rejects the bare
    aliases (`sonnet` -> HTTP 400). The CLI resolves an alias before sending (`--model sonnet`
    goes on the wire as `claude-sonnet-5`), so what arrives here is a full id or a DeepSeek one.
    """
    m = (model or "").lower().removeprefix("deepseek/")
    if m in _DEEPSEEK_RATES:
        return m
    return "deepseek-v4-pro" if "opus" in m else "deepseek-v4-flash"


def is_deepseek(base_url: str) -> bool:
    """True when ANTHROPIC_BASE_URL points at DeepSeek's Anthropic-compatible bridge."""
    return "deepseek.com" in (base_url or "")


def settled_cost(info: dict, model: str, base_url: str) -> tuple:
    """`(cost_usd, usage_source)` for one Claude Code run.

    On a DeepSeek-routed run the CLI's own figure is discarded: it is Anthropic-priced whatever the
    base URL says. It is recomputed from the captured billing inputs instead — cache split and peak
    window — which is `usage_source: "computed"` in the metering plan's vocabulary. Off DeepSeek the
    CLI's number stands, tagged `"cli"`. Neither branch ever invents a 0.0.
    """
    if is_deepseek(base_url):
        cost = deepseek_cost(model, info.get("input_miss_tokens"), info.get("cache_read_tokens"),
                             info.get("tokens_out"))
        if cost is not None:
            return (cost, "computed")
        # A truncated stream has the input half and not the output half. Say so, rather than
        # publishing a knowingly-low figure or an unexplained null.
        return (None, "partial" if info.get("usage_partial") else None)
    cost = info.get("cost_usd")
    return (cost, "cli" if cost is not None else None)


def agent_env() -> dict:
    """Env forwarded into the workbench container: auth, plus the endpoint selector.

    ANTHROPIC_BASE_URL points the CLI at an Anthropic-COMPATIBLE third-party endpoint (e.g.
    https://api.deepseek.com/anthropic, which maps sonnet/haiku -> deepseek-v4-flash) so the Claude
    Code lanes can run the same LLM as every other variety.

    A subscription OAuth token is NEVER forwarded to a third-party endpoint. It is an Anthropic
    bearer; handing it to another provider's server leaks a credential, and if the CLI prefers it
    over the base URL the run silently stays on Anthropic — defeating the whole point of the
    redirect with nothing in the output to show it. Dropping it makes `has_auth` fail loudly
    instead: a redirected run must carry a key for the provider it is being pointed at.
    """
    env = {k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)}
    base = env.get("ANTHROPIC_BASE_URL", "")
    if base and not any(host in base for host in _ANTHROPIC_HOSTS):
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env.update(inference_env())
    return env


def inference_env() -> dict:
    """Inference settings forwarded into the container, matching sweagent_repo2run.

    That arm pins `thinking: {type: disabled}` (its config explains why: the paper's baselines ran
    on a non-reasoning model, so leaving reasoning on makes it a materially stronger agent than the
    one being reproduced). Same weights in two inference modes is a model difference dressed up as
    an agent difference, so this lane matches it.

    MEASURED, because the CLI exposes no flag for it: `--effort low|medium|high|max` all put the
    same `thinking: {"type": "adaptive", "display": "omitted"}` on the wire, while
    MAX_THINKING_TOKENS=0 puts `{"type": "disabled"}` there — which DeepSeek's bridge honours
    (thinking blocks disappear; 237 -> 164 output tokens on one probe).

    NOT matched: temperature. sweagent_repo2run pins 0.2 and Claude Code sends no temperature field
    at all, so the endpoint's default applies. Nothing short of rewriting the request body can
    change that; it is recorded per run rather than silently ignored.
    """
    mode = (os.environ.get("CLAUDE_THINKING") or "disabled").strip().lower()
    return {} if mode == "enabled" else {"MAX_THINKING_TOKENS": "0"}


def has_auth(env: dict) -> bool:
    """True when `env` carries a credential the CLI can actually authenticate with."""
    return any(env.get(k) for k in AUTH_KEYS)


# DeepSeek v4 intermittently serialises a command in its own DSML markup instead of using the
# tool protocol — `<｜｜DSML｜｜bash>\nls -la /repo\n</｜｜DSML｜｜bash>`. On the SWE-agent arm that
# breaks ThoughtActionParser LOUDLY (see producers/sweagent_repo2run_runner.normalize_dsml_fences,
# which rewrites it into a fence). Here it cannot break anything: DeepSeek's Anthropic bridge
# builds typed tool_use blocks server-side and this module only reads the CLI's JSON envelope.
#
# That is exactly why it is worth counting. A leak lands as ordinary assistant TEXT: no error, no
# parse failure, just a turn spent on a command that never ran — invisible unless someone reads the
# trajectory. The count makes it a number on the row instead. Deliberately wider than the
# SWE-agent regex: DSML tags plus the raw special-token markers of the same family.
_MARKUP_LEAK = re.compile(r"DSML|tool▁call|｜｜")


def count_markup_leaks(text: str) -> int:
    """1 if this text block carries leaked DeepSeek markup, else 0."""
    return 1 if text and _MARKUP_LEAK.search(text) else 0


def _assistant_message_id(line: str):
    """For an `assistant` stream event, its message id ("" when absent); None for anything else.

    One LLM call == one model RESPONSE, which is the unit SWE-agent's per_instance_call_limit
    counts. The CLI may emit one `assistant` event per content block, so a response carrying text
    plus a tool_use would count twice if events were counted directly — the id is what collapses
    them back into one call.
    """
    try:
        obj = json.loads(line)
    except Exception:                    # noqa: BLE001 — partial/garbage lines are not turns
        return None
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    msg = obj.get("message")
    mid = msg.get("id") if isinstance(msg, dict) else None
    return mid if isinstance(mid, str) else ""


def run_claude_capped(cmd: list, timeout, stdin_text: str | None = None,
                      max_turns: int | None = None) -> dict:
    """Run the Claude Code CLI under a TURN cap, returning ``{stdout, stderr, turns, timed_out}``.

    The CLI has no turn flag — 2.1.258 ships ``--max-budget-usd`` and nothing else — and a dollar
    cap is not a step budget, least of all against a DeepSeek-priced arm. So the cap is enforced
    from outside: read the stream-json as it arrives, count `assistant` events, stop at the Nth.

    Partial output is the POINT, not an edge case: a capped (or walled) run emits no final
    `result` event, so the events already read are the only record of what the agent did. stderr
    goes to a temp file rather than a pipe because nothing drains a second pipe while stdout is
    read line-by-line, and a full stderr pipe would deadlock the agent.

    Killing the process here kills the local `docker exec` CLIENT only — call `stop_agent` to stop
    the agent inside the container.
    """
    import subprocess
    import tempfile
    import threading

    chunks: list = []
    turns = 0
    seen: set = set()
    walled = threading.Event()
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as errf:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errf,
                                text=True, encoding="utf-8", errors="replace")

        def _wall():
            if proc.poll() is not None:  # already finished: this is not a timeout
                return
            walled.set()
            proc.kill()

        timer = threading.Timer(max(1, int(timeout or 0)), _wall)
        timer.start()
        try:
            try:
                # close() is what FLUSHES a prompt smaller than the 8KB buffer, so it is the call
                # that raises BrokenPipe when the agent died first — it must share the guard.
                if stdin_text is not None:
                    proc.stdin.write(stdin_text)
                proc.stdin.close()
            except OSError:              # agent already gone; keep whatever it emitted
                pass
            for line in proc.stdout:
                chunks.append(line)
                mid = _assistant_message_id(line)
                if mid is None or (mid and mid in seen):
                    continue             # not a response, or another block of one already counted
                if mid:
                    seen.add(mid)
                turns += 1
                if max_turns and turns >= max_turns:
                    break
        finally:
            timer.cancel()
            proc.kill()
            proc.wait()
            errf.seek(0)
            stderr = errf.read()
    return {"stdout": "".join(chunks), "stderr": stderr, "turns": turns,
            "timed_out": walled.is_set()}


def stop_agent(container: str) -> None:
    """Kill every non-init process inside `container`. Best-effort.

    Killing the local `docker exec` client does NOT stop the process it started inside the
    container, and what runs next races it: the live lane runs pytest in that SAME container, and
    the Dockerfile lane copies Dockerfile.gen out of it. So an agent stopped by the turn cap or by
    the wall would otherwise keep installing packages into the env being scored.

    Run as ROOT, not as `agent`. The prompts instruct the agent to install with `sudo pip` and
    `sudo apt-get`, and all three workbenches grant it passwordless sudo — so the processes most
    worth stopping are uid 0 and an `agent`-scoped kill cannot signal them (measured: the sudo
    wrapper dies, the root payload keeps running). Linux `kill(-1)` never signals PID 1, so the
    container survives regardless of who sends it. pkill would be the obvious tool, but the slim
    workbench has no procps (measured: `command -v pkill` is empty in python:3.11-slim) — `kill`
    is a shell builtin and always there.
    """
    import subprocess
    try:
        subprocess.run(["docker", "exec", container, "sh", "-c", "kill -9 -1"],
                       check=False, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:                    # noqa: BLE001 — a failed cleanup must not sink the run
        pass


def _normalize_model(llm: str) -> str:
    """Claude Code needs a Claude model. Fall back to 'sonnet' for non-Claude slugs
    (e.g. the runner's default deepseek/...), so `bench claudecode` works even if the
    variety llm wasn't overridden."""
    low = (llm or "").lower()
    if low.startswith(_CLAUDE_PREFIXES):
        return llm
    return "sonnet"


# ── Language profiles ────────────────────────────────────────────────────────────────────────
#
# The producer is otherwise language-blind: it drives an agent, pulls /testbed/Dockerfile.gen and
# hands it to bench/, which scores it with the per-language strategy in bench.languages. A profile
# is the produce-side half of that pairing — the FROM to ask for, the prompt to send, and whether
# the emitted Dockerfile needs a test-runner install stapled on.
#
# ORGANIZING PRINCIPLE: a prompt must state, verbatim, what the grader will actually run. If the
# prompt and bench.languages.<Lang>.gate_cmd drift, an agent failure and a harness mismatch become
# indistinguishable in the results. Every claim a prompt makes about grading is copied from the
# matching Language, not paraphrased.
#
# NOTE: producers/ must not import bench.languages (dependency direction: produce and measure share
# only bench.schema), so the alias table below is a deliberate duplicate of bench.languages._REGISTRY.
# A test in producers/tests asserts the two stay in lockstep — that assertion is the only thing
# stopping them from drifting apart.


@dataclass(frozen=True)
class LangProfile:
    """Everything the producer needs to run ONE language's setup+emit."""

    key: str                # canonical name; matches the paired bench.languages Language.name
    default_base: str       # the FROM the agent is told to write, absent CLAUDE_DOCKERFILE_BASE
    prompt_template: str    # format keys: {gen_path} {base} {full_name}
    # The Docker image the AGENT works inside. Distinct from default_base, which is the FROM the
    # agent writes into the emitted Dockerfile and is the only one ever measured. Python and Node
    # share one workbench because it ships both toolchains; Rust and Java each need their own,
    # since an agent with no cargo/JDK cannot verify its own setup.
    workbench: str = "claude-runner:latest"
    # (probe_regex, RUN line): the producer appends `RUN line` to the emitted Dockerfile when
    # probe_regex does not match it. A single tuple rather than two fields so the two halves
    # cannot be set independently. None => append nothing (the language's own ensure_cmd covers it).
    test_runner: Optional[tuple] = None


# Collection-only, matching producers/sweagent_repo2run_config.yaml (see the note on
# runner/live/claudecode.py::SETUP_PROMPT for the alignment and its two deviations). The quoted
# command is this harness's gate, rat/libkit/tools/run_pytest_collect.py.
#
# CHANGING THIS TEXT RE-BASELINES THE ARM: the Node prompt's comment records that a live python50
# run is scored against the exact Python text, so results produced before this edit are not
# comparable with results produced after it.
# PORTED FROM producers/sweagent_repo2run_config.yaml's `instance_template` — the per-task half
# of that agent config. Its `system_template` is NOT portable: it teaches SWE-agent's own scaffold
# (the {{WINDOW}}-line viewport, the DISCUSSION/command format, one command per turn, the `bash-$`
# marker), describing an interface Claude Code does not have. The trailing
# "(Current task:)/(Current directory:)/bash-$" lines are that REPL's turn markers, dropped for the
# same reason.
#
# Adapted ONLY where this lane's grader differs, never in what the task asks for:
#   /repo            -> /testbed              (this lane is `conforming`; repo2run re-homes)
#   /Dockerfile      -> {gen_path}            (what the producer copies out)
#   the clone+cp preamble -> a direct clone to /testbed, since there is nothing to re-home
#   "pytest /repo --collect-only -q" -> "python -m pytest /testbed --co -q", which is what
#       rat/libkit/tools/run_pytest_collect.py actually runs
# NOTE items 3 and 4 are forced by the environment, not by the task: the agent works as a non-root
# user with sudo while the image build is root, and the gate runs the system python3.
#
# NOT ported, and worth knowing: the paper's prompt has no prohibition on editing test files. The
# earlier Claude-side guard was dropped to keep this faithful, so an agent may now encode
# `RUN rm tests/...` into the Dockerfile and collect cleanly on what remains.
_PYTHON_PROMPT = (
    "We\'re currently setting up the environment for the following task. Here are the "
    "details:\n\n"
    "TASK:\n"
    "Set up the environment for the Python repository {full_name} "
    "(https://github.com/{full_name}) so that its test suite can be collected, and record the "
    "working setup as a Dockerfile at {gen_path}.\n\n"
    "INSTRUCTIONS:\n"
    "Now, you\'ll carry out this task on your own. Your session has begun in the repository\'s "
    "root directory. Use any bash commands you need. Edit and check files as needed.\n\n"
    "The goal is to generate a Dockerfile that can successfully build and run the tests in the "
    "repository using the command \"pytest --collect-only -q\".\n\n"
    "NOTE:\n"
    "1. The repository is cloned into /testbed.\n"
    "2. The Dockerfile should start with the following lines (if the base image is {base} and "
    "the repository is adamobeng/wddbfs):\n"
    "```\n"
    "FROM {base}\n"
    "RUN pip install pytest\n"
    "RUN git clone https://github.com/adamobeng/wddbfs.git /testbed\n"
    "WORKDIR /testbed\n"
    "```\n"
    "3. The build runs as ROOT from an EMPTY image: drop every `sudo` prefix from the commands "
    "you write into the Dockerfile, and do not rely on any file from this container.\n"
    "4. Install into the system Python — do NOT create a virtualenv, since the grader runs the "
    "system python3.\n"
    "Your task includes:\n"
    "0. **Generate Dockerfile**: Create the Dockerfile at {gen_path}.\n"
    "1. **Read Directory Structure**: Check the folder structure in the root directory.\n"
    "2. **Check the Configuration Files**: Inspect files like \"requirements.txt\", "
    "\"setup.py\", \"setup.cfg\", \"Pipfile*\", etc.\n"
    "3. **Determine Package Dependencies**: Handle dependencies and manage conflicting "
    "dependency versions.\n"
    "4. **Testing**: Ensure \"python -m pytest /testbed --co -q\" runs without errors.\n"
    "5. **Generate Dockerfile**: Write necessary installation or setup steps determined from "
    "the inspection. If you finish run testing successfully, you should modify the Dockerfile "
    "in {gen_path}.\n\n"
    "IMPORTANT TIPS:\n"
    "* Check the directory and files carefully.\n"
    "* Make sure Dockerfile commands are correct.\n"
    "* Use proper Dockerfile syntax and indentation.\n"
    "* Test the final Dockerfile by running \"python -m pytest /testbed --co -q\"."
)

# The Node prompt is NOT the Python one with the nouns swapped — the contract is different in kind.
# bench.languages.NodeLanguage sets short_circuit_gate = True, so a failed gate means ZERO tests run
# and the repo scores zero; the gate and run lines below are copied verbatim from its gate_cmd/run_cmd.
#
# Two non-obvious consequences are spelled out to the agent because they are invisible from inside
# the repo: (1) `npx --no-install` resolves ONLY binaries already in node_modules/.bin, so omitting
# devDependencies silently produces an empty report rather than an error; (2) the gate's `npm ci`
# wipes node_modules and reinstalls strictly from the lockfile, so a `--no-save` install does not
# survive into the run step.
#
# The one-turn paragraph is here and deliberately NOT in the Python prompt: a live python50 run is
# scored against the exact Python text above, so adding it there would break comparability. It exists
# because agents have ended a `claude -p` run by backgrounding an install or calling ScheduleWakeup
# and waiting for a re-invocation that single-shot mode never delivers — burning the whole budget for
# no Dockerfile. Adding it to Python is a deferred, deliberate re-baseline.
#
# Literal braces are DOUBLED ({{}}) — this string goes through str.format, where a bare {} is an
# auto-numbered field and raises IndexError.
_NODE_PROMPT = (
    "You are configuring a JavaScript/TypeScript (Node.js) repository at /testbed so its "
    "EXISTING test suite can run, and then writing a Dockerfile that reproduces your setup "
    "from scratch.\n\n"
    "YOU GET EXACTLY ONE TURN. Nothing will re-invoke you, no scheduled wakeup will ever fire, "
    "and any backgrounded or detached process is killed the moment you stop. Run every install "
    "in the FOREGROUND and wait for it to finish. Never end your turn intending to resume later "
    "— if you are running short on budget, write the best Dockerfile you can to {gen_path} NOW "
    "instead of deferring.\n\n"
    "The grader rebuilds your Dockerfile from a clean base and then, inside the fresh image, runs "
    "EXACTLY this gate at /testbed:\n"
    "    (npm ci || npm install) && node -e \"process.exit((require('./package.json')"
    ".scripts||{{}}).test?0:1)\"\n"
    "If that gate fails, NO tests are run at all and the repo scores ZERO. If it passes, the grader "
    "runs the suite with `npx --no-install jest --ci --reporters=default --reporters=jest-junit`, "
    "falling back to `npx --no-install mocha --reporter mocha-junit-reporter`.\n\n"
    "So, first, in THIS container: install any system packages the dependencies need to build "
    "(`sudo apt-get install -y ...`), then run `npm ci || npm install` at /testbed until it exits 0. "
    "Install devDependencies too — never `--production` or `--omit=dev` — because `npx --no-install` "
    "only finds binaries already in node_modules/.bin. Make sure /testbed/package.json defines a "
    "`test` script (scripts.test); if it does not, add one that runs the project's own test framework. "
    "Remember that the grader's `npm ci` DELETES node_modules and reinstalls strictly from the "
    "lockfile, so anything you need at test time must be recorded in package.json and the lockfile "
    "— a `--no-save` install will not survive. You may edit configuration files. DO NOT modify, add, "
    "or delete any test files. DO NOT run the full test suite yourself (running the gate command "
    "above to check your work is fine).\n\n"
    "Then write a self-contained Dockerfile to {gen_path} that reproduces this environment FROM A "
    "CLEAN BASE. It MUST:\n"
    "  - start `FROM {base}`;\n"
    "  - `RUN git clone https://github.com/{full_name} /testbed` and `WORKDIR /testbed` (do NOT "
    "rely on any files from this container — the build starts empty, and the grader requires "
    "/testbed to still be a git worktree);\n"
    "  - install the SAME system packages and Node dependencies you installed, as RUN steps. The "
    "build runs as ROOT, so DROP every `sudo` prefix (use `apt-get install -y ...`, `npm ci`);\n"
    "  - re-encode any edits you made to repo files as explicit RUN steps (e.g. `RUN sed -i ...`, "
    "`RUN npm pkg set scripts.test=...`, or a heredoc), since the clone is pristine;\n"
    "  - NOT run the test suite in the Dockerfile.\n"
    "When the Dockerfile is written, stop."
)

PYTHON_PROFILE = LangProfile(
    key="python",
    default_base="python:3.11",
    prompt_template=_PYTHON_PROMPT,
    # The fresh-container measure needs pytest; PythonLanguage.ensure_cmd tries to install it but
    # tolerates failure (`|| true`), so the emitted Dockerfile is the reliable place to guarantee it.
    test_runner=(r"\bpytest\b", "RUN pip install --no-cache-dir pytest"),
)

NODE_PROFILE = LangProfile(
    key="nodejs",
    # node:22, not the older node:20 LTS. Measured on cap-js-community/odata-v2-adapter: on
    # node:20 every one of its 32 vitest FILES failed to import (0/32, 4.2s) because a transitive
    # dep (@sap/cds@10) declares `engines.node >=22`; the identical Dockerfile on node:22 scores
    # 280/283. Node floors like that are invisible to a scan of the repo's OWN `engines` field —
    # this one arrived through a dependency — so the newer base is the safer default. Nothing in
    # the corpus pins an upper bound below 22; the one repo wanting an OLDER node (^10.24.1) is
    # already out of reach on either base.
    default_base="node:22",
    prompt_template=_NODE_PROMPT,
    # None, NOT a jest install: NodeLanguage.ensure_cmd already npm-installs the JUnit reporters at
    # measure time, and the Python line would `pip install` onto a node base with no pip — failing
    # the build of EVERY Node repo before a single test could run.
    test_runner=None,
)

_RUST_PROMPT = (
    "You are configuring a Rust repository at /testbed so its EXISTING test suite can run, and "
    "then writing a Dockerfile that reproduces your setup from scratch.\n\n"
    "YOU GET EXACTLY ONE TURN. Nothing will re-invoke you, no scheduled wakeup will ever fire, "
    "and any backgrounded or detached process is killed the moment you stop. Run every command in "
    "the FOREGROUND and wait for it to finish. Never end your turn intending to resume later — if "
    "you are running short on budget, write the best Dockerfile you can to {gen_path} NOW instead "
    "of deferring.\n\n"
    "The grader rebuilds your Dockerfile from a clean base and then, inside the fresh image, runs "
    "EXACTLY this gate at /testbed:\n"
    "    export PATH=/usr/local/cargo/bin:$PATH && cargo test --no-run\n"
    "If that gate fails, NO tests are run at all and the repo scores ZERO. If it passes, the "
    "grader runs the suite with `cargo nextest run --profile ci`.\n\n"
    "Two things about that gate decide whether your Dockerfile works:\n"
    "  - It runs under a LOGIN shell, which resets PATH. The grader prepends exactly one directory, "
    "/usr/local/cargo/bin, and relies on the rest of the default profile PATH for everything else. "
    "So `cargo` must be reachable at /usr/local/cargo/bin or on that default PATH — an `ENV PATH` "
    "in your Dockerfile does NOT survive, because the login shell overwrites it. The base image "
    "already satisfies this; if you install another toolchain, symlink its `cargo` into "
    "/usr/local/bin rather than editing PATH.\n"
    "  - It compiles from scratch in a container that has never built this crate, and it runs under "
    "a time limit. Nothing carries over from your container, so a crate that compiles only at gate "
    "time can exhaust that limit and score ZERO on a repo that was actually fine. Compiling in the "
    "Dockerfile is how you avoid that: end it with `RUN cargo test --no-run` so the crate registry, "
    "the pinned toolchain and ./target are baked into the image. That command COMPILES the tests "
    "without executing any, which is exactly what is wanted — it is not the forbidden step.\n\n"
    "So, first, in THIS container: install any system packages the crate needs to build "
    "(`sudo apt-get install -y ...` — pkg-config, libssl-dev and cmake are already present), then "
    "run `cargo test --no-run` at /testbed until it exits 0. If the repo has a rust-toolchain.toml "
    "or rust-toolchain file, LEAVE IT ALONE: rustup reads it and installs the pinned toolchain "
    "automatically. The gate runs at the repo ROOT. If this repository has no Cargo.toml at the "
    "root, the gate cannot run at all — add a root workspace manifest covering the real crates as "
    "part of your setup, and re-encode it in the Dockerfile. Do NOT create or edit "
    ".config/nextest.toml — the grader writes its own. You may edit configuration files. DO NOT "
    "modify, add, or delete any test files. DO NOT execute the test suite (running the gate command "
    "above to check your work is fine).\n\n"
    "Then write a self-contained Dockerfile to {gen_path} that reproduces this environment FROM A "
    "CLEAN BASE. It MUST:\n"
    "  - start `FROM {base}`;\n"
    "  - `RUN git clone https://github.com/{full_name} /testbed` and `WORKDIR /testbed` (do NOT "
    "rely on any files from this container — the build starts empty, and the grader requires "
    "/testbed to still be a git worktree);\n"
    "  - install the SAME system packages you installed, as RUN steps. The build runs as ROOT, so "
    "DROP every `sudo` prefix (use `apt-get install -y ...`);\n"
    "  - re-encode any edits you made to repo files as explicit RUN steps (e.g. `RUN sed -i ...` "
    "or a heredoc), since the clone is pristine;\n"
    "  - end with `RUN cargo test --no-run` to bake the build;\n"
    "  - NOT contain `cargo nextest run`, `cargo test` WITHOUT `--no-run`, or any other command "
    "that EXECUTES tests.\n"
    "When the Dockerfile is written, stop.\n"
)

RUST_PROFILE = LangProfile(
    key="rust",
    default_base="rust:1",
    prompt_template=_RUST_PROMPT,
    # rust:1, not -slim/alpine: rust.py hardcodes PATH=/usr/local/cargo/bin (the full image's
    # layout) and its ensure_cmd curls a prebuilt cargo-nextest, so curl must ship in the base.
    workbench="claude-runner-rust:latest",
    # None: RustLanguage.ensure_cmd installs cargo-nextest at measure time.
    test_runner=None,
)

_JAVA_PROMPT = (
    "You are configuring a Java repository at /testbed so its EXISTING test suite can run, and "
    "then writing a Dockerfile that reproduces your setup from scratch.\n\n"
    "YOU GET EXACTLY ONE TURN. Nothing will re-invoke you, no scheduled wakeup will ever fire, "
    "and any backgrounded or detached process is killed the moment you stop. Run every command in "
    "the FOREGROUND and wait for it to finish. Never end your turn intending to resume later — if "
    "you are running short on budget, write the best Dockerfile you can to {gen_path} NOW instead "
    "of deferring.\n\n"
    "The grader rebuilds your Dockerfile from a clean base and then, inside the fresh image, runs "
    "EXACTLY this at /testbed, after `export PATH=\"$JAVA_HOME/bin:$PATH\"`:\n"
    "    if [ -f pom.xml ]; then\n"
    "        if [ -x ./mvnw ]; then ./mvnw -q -B test-compile; else mvn -q -B test-compile; fi\n"
    "    else\n"
    "        if [ -x ./gradlew ]; then ./gradlew -q testClasses; else gradle -q testClasses; fi\n"
    "    fi\n"
    "If that gate fails, NO tests are run at all and the repo scores ZERO. If it passes, the "
    "grader runs the suite the same way — `./mvnw -B test` or `mvn -B test`, else `./gradlew test` "
    "or `gradle test` — and reads the Surefire / Gradle JUnit XML.\n\n"
    "Four things about that gate decide whether your Dockerfile works:\n"
    "  - It picks its branch on `[ -f pom.xml ]` AT THE REPOSITORY ROOT. If this project keeps its "
    "real pom.xml in a subdirectory, the gate takes the GRADLE branch and fails on a healthy repo — "
    "so add an aggregator pom.xml at the root that builds the real modules, and re-encode it in "
    "the Dockerfile. Equally, never move, rename or delete a root build file that is already "
    "there.\n"
    "  - It picks the wrapper on `[ -x ./mvnw ]` / `[ -x ./gradlew ]` — EXECUTABLE, not merely "
    "present. A wrapper that lost its execute bit silently falls back to the system tool at a "
    "different version. If the repo ships a wrapper, `chmod +x` it and re-encode that as a RUN "
    "step.\n"
    "  - The image is JDK 17. If this project cannot compile on 17, install the JDK it needs and "
    "set `ENV JAVA_HOME=/path/to/that/jdk` in your Dockerfile — the grader reads JAVA_HOME and "
    "will honor it. (Do not try the same trick with PATH; the grader runs under a login shell "
    "that resets PATH.)\n"
    "  - It compiles from scratch with an empty dependency cache, under a time limit. A project "
    "that only downloads its dependencies at gate time can exhaust that limit and score ZERO when "
    "it was actually fine. Compiling in the Dockerfile is how you avoid that: end it with the "
    "gate's own compile command so ~/.m2 (or the Gradle cache) and the build outputs are baked "
    "into the image. That command COMPILES the tests without executing any, which is exactly what "
    "is wanted — it is not the forbidden step.\n\n"
    "If this project builds with Gradle and ships no EXECUTABLE ./gradlew, the gate falls back to a "
    "system `gradle` that the base image does not have, and the repo scores ZERO. In that case "
    "install one yourself and re-encode it in the Dockerfile — but do NOT use "
    "`apt-get install gradle`, which is several major versions behind and cannot build a modern "
    "project. Download the distribution the project expects, unpack it, and symlink its `bin/gradle` "
    "to /usr/local/bin/gradle so that `gradle` itself resolves on the default PATH.\n\n"
    "So, first, in THIS container: install any system packages the build needs "
    "(`sudo apt-get install -y ...`), then run the gate command above at /testbed until it exits "
    "0. You may edit configuration files. DO NOT modify, add, or delete any test files. DO NOT "
    "execute the test suite (running the gate command above to check your work is fine).\n\n"
    "Then write a self-contained Dockerfile to {gen_path} that reproduces this environment FROM A "
    "CLEAN BASE. It MUST:\n"
    "  - start `FROM {base}`;\n"
    "  - `RUN git clone https://github.com/{full_name} /testbed` and `WORKDIR /testbed` (do NOT "
    "rely on any files from this container — the build starts empty, and the grader requires "
    "/testbed to still be a git worktree);\n"
    "  - install the SAME system packages you installed, as RUN steps. The build runs as ROOT, so "
    "DROP every `sudo` prefix (use `apt-get install -y ...`);\n"
    "  - re-encode any edits you made to repo files as explicit RUN steps (e.g. `RUN sed -i ...` "
    "or a heredoc), since the clone is pristine;\n"
    "  - end with the gate's own compile command to bake the dependency cache;\n"
    "  - NOT run `mvn test`, `gradle test`, or any other command that EXECUTES tests.\n"
    "When the Dockerfile is written, stop.\n"
)

JAVA_PROFILE = LangProfile(
    key="java",
    # JAVA_HOME here is /opt/java/openjdk — exactly the default java.py falls back to — and mvn is
    # on the login-shell PATH. No gradle binary; JavaLanguage prefers ./gradlew, which 12 of the
    # 13 Gradle repos in rat_java50.json ship.
    default_base="maven:3-eclipse-temurin-17",
    prompt_template=_JAVA_PROMPT,
    workbench="claude-runner-java:latest",
    # None: Surefire and Gradle write JUnit XML natively, so there is nothing to install.
    test_runner=None,
)

# Keys mirror bench.languages._REGISTRY exactly (see the note above); the aliases must resolve to
# the same language on both sides of the seam or the prompt describes a grader that never runs.
_PROFILES = {
    "python": PYTHON_PROFILE,
    "nodejs": NODE_PROFILE, "node": NODE_PROFILE,
    "javascript": NODE_PROFILE, "typescript": NODE_PROFILE,
    "rust": RUST_PROFILE,
    "java": JAVA_PROFILE,
}


def get_profile(language) -> LangProfile:
    """Return the LangProfile for `language`, defaulting to Python for unknown/empty names.

    The default mirrors bench.languages.get_language so produce and measure agree on what an
    unrecognized dataset `language` means — silently one language on one side of the seam and
    another on the other is the worst outcome."""
    return _PROFILES.get((language or "python").lower(), PYTHON_PROFILE)


def resolve_base(profile: LangProfile, env_base) -> str:
    """The FROM the agent is told to write: CLAUDE_DOCKERFILE_BASE if set, else the language default.

    `or`, not a dict default: an explicitly-exported-but-empty var must fall through to the language
    default rather than asking the agent for `FROM `."""
    return env_base or profile.default_base


def resolve_workbench(profile: LangProfile, env_image) -> str:
    """The image the agent works inside: CLAUDE_RUNNER_IMAGE if set, else the language's own.

    `or`, not a dict default: an exported-but-empty var must fall through to the profile rather
    than asking Docker to run the image named "" (same rule as resolve_base)."""
    return env_image or profile.workbench


def build_prompt(full_name: str, base: str, profile: LangProfile) -> str:
    """The agentic setup+emit prompt with the repo and emitted-base injected."""
    return profile.prompt_template.format(gen_path=DOCKERFILE_GEN_PATH, base=base,
                                          full_name=full_name)


def _as_int(value) -> int:
    """A token count from an untrusted stream field. Anything non-numeric (or a bool, which is
    an int in Python but never a token count) becomes 0, so one malformed `usage` field degrades
    to a wrong-but-harmless number instead of raising mid-parse."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _content_blocks(obj: dict) -> list:
    """The `message.content` blocks of a stream event, or [] for any shape that is not the
    expected ``{"message": {"content": [{...}, ...]}}``.

    Truthiness is NOT enough here: a truthy non-dict `message` (a bare string) sails past an
    ``or {}`` fallback and then raises on ``.get()``. Same for a non-list `content`."""
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    return content if isinstance(content, list) else []


def _flatten_tool_result(content) -> str:
    """Readable text from a tool_result's `content`.

    Claude Code emits this as a LIST of content blocks (``[{"type": "text", "text": "..."}]``)
    far more often than a bare string — that is the normal shape for Bash/Read/Edit output. A
    plain ``str()`` would put Python repr noise (``[{'type': 'text', ...}]``) into the action log
    for essentially every real run, defeating the point of having one."""
    if isinstance(content, str):
        return " ".join(content.split())
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(" ".join(parts).split())
    return "" if content is None else " ".join(str(content).split())


def _tool_input_summary(inp) -> str:
    """One-line rendering of a tool call's input. Bash/Read/Edit/Grep/WebFetch each carry a single
    field that IS the action, so show that rather than the whole JSON blob; anything else falls
    back to compact JSON."""
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "file_path", "path", "pattern", "url"):
        if isinstance(inp.get(key), str):
            return " ".join(inp[key].split())
    try:
        return json.dumps(inp)
    except (TypeError, ValueError):
        return ""


def summarize_stream(stream_text: str) -> dict:
    """Parse claude `--output-format stream-json` (one JSON object per line) into the economy
    numbers plus a readable action log.

    Tolerant of partial/truncated streams by design: a budget-capped or timed-out run emits no
    final `result` event, but the `tool_use` events it did emit are the only surviving record of
    what the agent attempted. Malformed lines are skipped; every result-derived field degrades to
    None rather than raising.

    Token accounting deliberately matches /opt/runs/ccdf_costs.py — tokens_in = input +
    cache_creation + cache_read — so ccdf numbers stay comparable with the earlier ccdf baselines.
    Cache reads dominate Claude Code usage (22k cache-read vs 2.5k fresh input on a trivial
    prompt), so this is NOT comparable to the raw prompt-token counts the deepseek agents report;
    the component split stays available in the persisted claude_stream.jsonl.
    """
    actions: list = []
    info: dict = {"turns": None, "cost_usd": None, "is_error": None, "stop_reason": None,
                  "tokens_in": None, "tokens_out": None, "total_tokens": None,
                  # The cache split, kept SEPARATELY from the tokens_in sum: DeepSeek prices a
                  # hit 31x cheaper than a miss, so collapsing them makes cost unrecoverable.
                  "input_miss_tokens": None, "cache_read_tokens": None,
                  "llm_calls": 0, "tool_calls": 0, "model_usage": {}, "rate_limited": False,
                  "dsml_text_blocks": 0, "usage_partial": False}
    seen: set = set()
    # Per-response usage, summed as a FALLBACK for a run with no final `result` event — i.e. every
    # turn-capped or walled run. Without it the hardest runs report no tokens at all and
    # bench.metrics averages cost/tokens over the cheap runs only.
    acc = {"in": 0, "out": 0, "n": 0, "miss": 0, "hit": 0, "deltas": 0}
    for raw in (stream_text or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:            # noqa: BLE001 — totality beats precision on untrusted input
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "stream_event":
            # --include-partial-messages emits one `message_delta` per RESPONSE carrying that
            # response's output_tokens. It is the only per-response source of them: the `assistant`
            # events always report output_tokens: 0, and the final `result` event does not exist on
            # a capped or walled run. Without this a truncated run cannot be priced at all — and
            # output is ~30% of the cost against input that is 98% cache reads.
            ev = obj.get("event")
            if isinstance(ev, dict) and ev.get("type") == "message_delta":
                u = ev.get("usage")
                if isinstance(u, dict):
                    acc["out"] += _as_int(u.get("output_tokens"))
                    acc["deltas"] += 1
            continue
        if kind == "system" and obj.get("subtype") == "init":
            actions.append(f"[init] session={str(obj.get('session_id', '?'))[:8]} "
                           f"cwd={obj.get('cwd', '?')}")
        elif kind == "assistant":
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            mid = msg.get("id") if isinstance(msg.get("id"), str) else ""
            if not mid or mid not in seen:      # one RESPONSE is one call, however many blocks
                if mid:
                    seen.add(mid)
                info["llm_calls"] += 1
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    acc["miss"] += (_as_int(usage.get("input_tokens"))
                                    + _as_int(usage.get("cache_creation_input_tokens")))
                    acc["hit"] += _as_int(usage.get("cache_read_input_tokens"))
                    acc["in"] += (_as_int(usage.get("input_tokens"))
                                  + _as_int(usage.get("cache_creation_input_tokens"))
                                  + _as_int(usage.get("cache_read_input_tokens")))
                    acc["out"] += _as_int(usage.get("output_tokens"))
                    acc["n"] += 1
            for block in _content_blocks(obj):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    info["tool_calls"] += 1
                    payload = _tool_input_summary(block.get("input"))[:200]
                    actions.append(f"[{info['tool_calls']}] {block.get('name', '?')}: {payload}")
                elif block.get("type") == "text":
                    raw_text = block.get("text")
                    text = raw_text.strip() if isinstance(raw_text, str) else ""
                    if text:
                        leaked = count_markup_leaks(text)
                        info["dsml_text_blocks"] += leaked
                        # Tagged in the log too, so a leak is greppable in the trajectory and not
                        # only a count on the row.
                        actions.append(f"    say{'[DSML]' if leaked else ''}: {text[:240]}")
        elif kind == "user":
            for block in _content_blocks(obj):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tag = "ERR" if block.get("is_error") else "ok"
                    body = _flatten_tool_result(block.get("content"))[:200]
                    actions.append(f"      -> [{tag}] {body}")
        elif kind == "rate_limit_event":
            info_block = obj.get("rate_limit_info")
            source = info_block if isinstance(info_block, dict) else obj
            status = str(source.get("status", ""))
            if status and status != "allowed":      # only flag ACTUAL throttling/rejection
                info["rate_limited"] = True
            actions.append(f"[rate-limit] status={status}")
        elif kind == "result":
            info["turns"] = obj.get("num_turns")
            info["cost_usd"] = obj.get("total_cost_usd")
            info["is_error"] = obj.get("is_error")
            info["stop_reason"] = obj.get("stop_reason")
            model_usage = obj.get("modelUsage")
            info["model_usage"] = model_usage if isinstance(model_usage, dict) else {}
            # Prefer modelUsage over the top-level usage block. Measured on four real runs, the
            # two disagree by a CONSTANT +454 input and +11..18 output tokens in modelUsage's
            # favour — the CLI makes an auxiliary call that the aggregate `usage` omits. The gap is
            # ~0.01% of input, so this is about the claim being exact rather than about money; it
            # also gives a per-model split, which is what would expose a second model ever being
            # billed (every run so far: claude-sonnet-5 alone).
            mu, usage = obj.get("modelUsage"), obj.get("usage")
            cands = []
            if isinstance(mu, dict) and mu:
                rows = [m for m in mu.values() if isinstance(m, dict)]
                cands.append((sum(_as_int(m.get("inputTokens"))
                                  + _as_int(m.get("cacheCreationInputTokens")) for m in rows),
                              sum(_as_int(m.get("cacheReadInputTokens")) for m in rows),
                              sum(_as_int(m.get("outputTokens")) for m in rows)))
            if isinstance(usage, dict):
                cands.append((_as_int(usage.get("input_tokens"))
                              + _as_int(usage.get("cache_creation_input_tokens")),
                              _as_int(usage.get("cache_read_input_tokens")),
                              _as_int(usage.get("output_tokens"))))
            if cands:
                # Take whichever block accounts for MORE, never whichever comes first. On real
                # streams modelUsage wins by a constant +454 input (an auxiliary call the aggregate
                # `usage` omits), but modelUsage is not guaranteed to carry the cache fields — and
                # a modelUsage without them would silently drop ~98% of the input on a run like
                # these, where cache reads are nearly all of it. Undercounting must not be the
                # quiet default.
                miss, hit, _ = max(cands, key=lambda c: c[0] + c[1])
                tout = max(c[2] for c in cands)
            else:
                miss = None
            if miss is not None:
                info["input_miss_tokens"], info["cache_read_tokens"] = miss, hit
                info["tokens_in"], info["tokens_out"] = miss + hit, tout
                info["total_tokens"] = miss + hit + tout
    if info["tokens_in"] is None and acc["n"]:
        # No `result` event: capped or walled. Input is fully recoverable — summing per-response
        # usage reproduces the result event's input total exactly (verified on four real runs).
        info["tokens_in"] = acc["in"]
        info["input_miss_tokens"], info["cache_read_tokens"] = acc["miss"], acc["hit"]
        # OUTPUT is not. Measured: per-assistant usage always carries `output_tokens: 0`, and the
        # final `result` event is the ONLY event type in the stream that reports a non-zero one
        # (14,401 system + 208 assistant + 88 user events in a 69-call run, none of them carrying
        # it). So on a truncated run output is UNKNOWN, not zero, and saying zero would understate
        # DeepSeek cost by 25-30% — output is $0.66/1M against input that is ~98% cache reads at
        # $0.007/1M. `usage_partial` is what makes the resulting None self-explaining.
        # acc["out"] is real only when message_delta events were present (--include-partial-
        # messages). Without them it is a sum of zeros, which must stay None rather than read as
        # "the agent produced no output".
        info["tokens_out"] = acc["out"] if acc["deltas"] else None
        info["total_tokens"] = acc["in"] + acc["out"] if acc["deltas"] else None
        info["usage_partial"] = not acc["deltas"]
    info["actions"] = "\n".join(actions)
    return info
