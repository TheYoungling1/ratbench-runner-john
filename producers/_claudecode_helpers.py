"""Pure, RAT-tree-free helpers for the claudecode-dockerfile PRODUCER.

Folded out of the runner's claudecode model + dockerfile helpers so producers/ never
imports the runner package or RAT model modules (the produce -> measure seam only
crosses via bench.schema). Stdlib-only, so producers stay importable without the RAT tree.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

W = "/testbed"
AUTH_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
DOCKERFILE_GEN_PATH = "/testbed/Dockerfile.gen"

# The Claude Code CLI accepts model aliases (sonnet/opus/haiku) or full IDs.
_CLAUDE_PREFIXES = ("claude", "sonnet", "opus", "haiku")


def _as_text(v) -> str:
    """Decode subprocess output that may be str (text mode), bytes, or None."""
    if v is None:
        return ""
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else v


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


_PYTHON_PROMPT = (
    "You are configuring a Python repository at /testbed so its EXISTING test suite can "
    "run, and then writing a Dockerfile that reproduces your setup from scratch.\n\n"
    "First, install ALL Python dependencies and any required system packages so that "
    "pytest can collect and run the tests. Install into the SYSTEM Python using "
    "`sudo pip install ...` and `sudo apt-get install -y ...` for system libraries — do "
    "NOT create a virtualenv (the grader runs the system python3). You may edit "
    "configuration files. DO NOT modify, add, or delete any test files. DO NOT run the "
    "test suite yourself.\n\n"
    "Then write a self-contained Dockerfile to {gen_path} that reproduces this environment "
    "FROM A CLEAN BASE. It MUST:\n"
    "  - start `FROM {base}`;\n"
    "  - `RUN git clone https://github.com/{full_name} /testbed` and `WORKDIR /testbed` "
    "(do NOT rely on any files from this container — the build starts empty);\n"
    "  - install the SAME system packages and Python dependencies you installed, as RUN "
    "steps. The build runs as ROOT, so DROP every `sudo` prefix (use "
    "`apt-get install -y ...`, `pip install ...`);\n"
    "  - re-encode any edits you made to repo files as explicit RUN steps (e.g. "
    "`RUN sed -i ...` or a heredoc), since the clone is pristine;\n"
    "  - NOT run pytest or the test suite in the Dockerfile.\n"
    "When the Dockerfile is written, stop."
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
                  "llm_calls": 0, "tool_calls": 0, "model_usage": {}, "rate_limited": False}
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
        if kind == "system" and obj.get("subtype") == "init":
            actions.append(f"[init] session={str(obj.get('session_id', '?'))[:8]} "
                           f"cwd={obj.get('cwd', '?')}")
        elif kind == "assistant":
            info["llm_calls"] += 1
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
                        actions.append(f"    say: {text[:240]}")
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
            usage = obj.get("usage")
            if isinstance(usage, dict):
                tin = (_as_int(usage.get("input_tokens"))
                       + _as_int(usage.get("cache_creation_input_tokens"))
                       + _as_int(usage.get("cache_read_input_tokens")))
                tout = _as_int(usage.get("output_tokens"))
                info["tokens_in"], info["tokens_out"] = tin, tout
                info["total_tokens"] = tin + tout
    info["actions"] = "\n".join(actions)
    return info
