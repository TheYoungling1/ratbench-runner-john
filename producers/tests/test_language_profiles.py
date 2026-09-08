# producers/tests/test_language_profiles.py — the produce-side language seam.
#
# All pure: no docker, no API keys, no agent. These tests exist because the two things this seam
# gets wrong are both silent — a prompt that describes a grader which never runs, and an alias
# table that drifts out of step with bench.languages.
from producers._claudecode_helpers import (
    NODE_PROFILE, PYTHON_PROFILE, _PROFILES, build_prompt, get_profile, resolve_base,
)


# ── alias resolution ────────────────────────────────────────────────────────────────────────

def test_get_profile_resolves_python():
    assert get_profile("python") is PYTHON_PROFILE


def test_get_profile_resolves_every_node_alias():
    # These four are the exact keys bench.languages._REGISTRY maps to NodeLanguage.
    for alias in ("nodejs", "node", "javascript", "typescript"):
        assert get_profile(alias) is NODE_PROFILE, alias


def test_get_profile_is_case_insensitive():
    # RepoSpec.language is lowered by runner/benchmark.py, but a dataset read by any other path
    # (or a hand-built RepoSpec) is not, and a "JavaScript" row must not silently become Python.
    assert get_profile("JavaScript") is NODE_PROFILE
    assert get_profile("Python") is PYTHON_PROFILE


def test_get_profile_defaults_to_python_for_unknown_none_and_empty():
    # Mirrors bench.languages.get_language: produce and measure must agree on what an
    # unrecognized language means, or the seam disagrees with itself.
    # "rust" used to be listed here; it now has a real profile, so the stand-in for "a language
    # bench/ can score but the producer has no prompt for" is `go` (see _NO_PROFILE_YET below).
    for value in ("go", "cobol", "", None):
        assert get_profile(value) is PYTHON_PROFILE, value


# ── the Python prompt is FROZEN ─────────────────────────────────────────────────────────────
#
# A paid python50 benchmark run is scored against this exact text. One changed byte makes those
# numbers incomparable, so the golden is a standalone literal rather than a re-render of the
# template — a refactor that "obviously" preserves the prompt has to prove it here.
# The one-turn clause deliberately does NOT appear in this prompt (it is Node-only until Python
# is re-baselined on purpose).
#
# RE-BASELINED 2026-09-04, deliberately, TWICE — the second time is the one that stands:
# the prompt is now a port of sweagent_repo2run_config.yaml's `instance_template`, so the
# two compared arms are given the same task text rather than two paraphrases of it.
# Superseded first pass, for the record: The task changed from "collect and run" to COLLECTION
# ONLY, to match producers/sweagent_repo2run_config.yaml, which targets `pytest --collect-only -q`
# — the two arms are compared, so they must be asked for the same outcome. The quoted command is
# this harness's own gate (rat/libkit/tools/run_pytest_collect.py runs `python -m pytest --co -q`),
# and the agent may now run it to verify. python50 results produced BEFORE this date are scored
# against the previous text and are not comparable with results produced after it.

_PYTHON_PROMPT_GOLDEN = (
    'We\'re currently setting up the environment for the following task. Here are the details:\n'
    '\n'
    'TASK:\n'
    'Set up the environment for the Python repository o/r (https://github.com/o/r) so that its test suite can be collected, and record the working setup as a Dockerfile at /testbed/Dockerfile.gen.\n'
    '\n'
    'INSTRUCTIONS:\n'
    'Now, you\'ll carry out this task on your own. Your session has begun in the repository\'s root directory. Use any bash commands you need. Edit and check files as needed.\n'
    '\n'
    'The goal is to generate a Dockerfile that can successfully build and run the tests in the repository using the command "pytest --collect-only -q".\n'
    '\n'
    'NOTE:\n'
    '1. The repository is cloned into /testbed.\n'
    '2. The Dockerfile should start with the following lines (if the base image is python:3.11 and the repository is adamobeng/wddbfs):\n'
    '```\n'
    'FROM python:3.11\n'
    'RUN pip install pytest\n'
    'RUN git clone https://github.com/adamobeng/wddbfs.git /testbed\n'
    'WORKDIR /testbed\n'
    '```\n'
    '3. The build runs as ROOT from an EMPTY image: drop every `sudo` prefix from the commands you write into the Dockerfile, and do not rely on any file from this container.\n'
    '4. Install into the system Python — do NOT create a virtualenv, since the grader runs the system python3.\n'
    'Your task includes:\n'
    '0. **Generate Dockerfile**: Create the Dockerfile at /testbed/Dockerfile.gen.\n'
    '1. **Read Directory Structure**: Check the folder structure in the root directory.\n'
    '2. **Check the Configuration Files**: Inspect files like "requirements.txt", "setup.py", "setup.cfg", "Pipfile*", etc.\n'
    '3. **Determine Package Dependencies**: Handle dependencies and manage conflicting dependency versions.\n'
    '4. **Testing**: Ensure "python -m pytest /testbed --co -q" runs without errors.\n'
    '5. **Generate Dockerfile**: Write necessary installation or setup steps determined from the inspection. If you finish run testing successfully, you should modify the Dockerfile in /testbed/Dockerfile.gen.\n'
    '\n'
    'IMPORTANT TIPS:\n'
    '* Check the directory and files carefully.\n'
    '* Make sure Dockerfile commands are correct.\n'
    '* Use proper Dockerfile syntax and indentation.\n'
    '* Test the final Dockerfile by running "python -m pytest /testbed --co -q".'
)


def test_python_prompt_is_byte_identical_to_the_measured_baseline():
    assert build_prompt("o/r", "python:3.11", PYTHON_PROFILE) == _PYTHON_PROMPT_GOLDEN


# ── the Node prompt mirrors NodeLanguage ────────────────────────────────────────────────────

def _node_prompt(full_name: str = "expressjs/express") -> str:
    return build_prompt(full_name, NODE_PROFILE.default_base, NODE_PROFILE)


def test_node_prompt_states_the_actual_gate():
    # NodeLanguage.short_circuit_gate is True: a failed gate means ZERO tests run. The prompt has
    # to name the gate it must pass, or an agent failure is indistinguishable from a mismatch.
    prompt = _node_prompt()
    assert "npm ci" in prompt
    assert "scripts.test" in prompt and "`test` script" in prompt
    assert "npx --no-install" in prompt          # why devDependencies are non-negotiable


def test_node_prompt_asks_for_the_node_base_and_the_repo_clone():
    prompt = _node_prompt("expressjs/express")
    assert "FROM node:22" in prompt
    assert "git clone https://github.com/expressjs/express /testbed" in prompt


def test_node_prompt_states_the_one_turn_constraint():
    # `claude -p` is single-shot. Runs have been lost to agents backgrounding an install or
    # scheduling a wakeup and ending the turn to wait for a re-invocation that never comes.
    prompt = _node_prompt()
    assert "EXACTLY ONE TURN" in prompt
    assert "FOREGROUND" in prompt
    assert "re-invoke" in prompt and "wakeup" in prompt


def test_node_prompt_never_mentions_python_tooling():
    # A stray `pip`/`pytest` reference would invite the agent to write a line that cannot run on
    # a node base — the exact failure _ensure_test_runner was fixed to stop producing.
    prompt = _node_prompt()
    assert "pip" not in prompt
    assert "pytest" not in prompt


# ── base resolution ─────────────────────────────────────────────────────────────────────────

def test_env_base_overrides_the_language_default():
    # CLAUDE_DOCKERFILE_BASE is a GLOBAL pin (one base for the whole run), so it wins everywhere.
    assert resolve_base(NODE_PROFILE, "node:20-bookworm") == "node:20-bookworm"
    assert resolve_base(PYTHON_PROFILE, "python:3.13") == "python:3.13"


def test_unset_or_empty_env_base_falls_to_the_language_default():
    # None is "unset"; "" is an exported-but-empty var, which must not become `FROM `.
    for env_base in (None, ""):
        assert resolve_base(PYTHON_PROFILE, env_base) == "python:3.11"
        assert resolve_base(NODE_PROFILE, env_base) == "node:22"


def test_language_default_base_reaches_the_prompt():
    assert "FROM node:22" in build_prompt("o/r", resolve_base(NODE_PROFILE, None), NODE_PROFILE)
    assert "FROM python:3.11" in build_prompt("o/r", resolve_base(PYTHON_PROFILE, None),
                                              PYTHON_PROFILE)


# ── parity with the MEASURE registry ────────────────────────────────────────────────────────
#
# producers/ must not import bench.languages in production code (produce and measure share only
# bench.schema), which is exactly why the alias table is duplicated — and why it can drift. The
# import below is test-only, and this is the assertion that keeps the two honest.

# Languages bench/ can score but the producer has no prompt for yet. get_profile() sends these to
# the Python profile (same fallback as get_language()), which would emit a PYTHON prompt for a Go
# repo — harmless while no dataset uses them, wrong the day one does. Listing them explicitly makes
# the parity check total in BOTH directions: a new measure alias or a new producer profile fails
# this test until the other side is updated.
_NO_PROFILE_YET = {"golang", "go"}


def test_profile_aliases_match_the_measure_registry():
    from bench.languages import _REGISTRY

    assert set(_PROFILES) | _NO_PROFILE_YET == set(_REGISTRY)
    assert set(_PROFILES) & _NO_PROFILE_YET == set()


def test_profile_alias_groups_match_the_measure_registry():
    # Key-set parity is not enough: `javascript` must resolve to the SAME language on both sides.
    from bench.languages import _REGISTRY

    for alias, profile in _PROFILES.items():
        assert _REGISTRY[alias].name == profile.key, alias


# ── workbench image ─────────────────────────────────────────────────────────────────────────

def test_python_and_node_share_the_original_workbench():
    # Task 1 is a pure refactor: whatever these two resolved to before must be byte-identical
    # after, or the completed python50 / node-full50 runs stop being comparable.
    assert PYTHON_PROFILE.workbench == "claude-runner:latest"
    assert NODE_PROFILE.workbench == "claude-runner:latest"


def test_workbench_env_override_wins_for_every_profile():
    # CLAUDE_RUNNER_IMAGE is a GLOBAL pin, mirroring CLAUDE_DOCKERFILE_BASE.
    from producers._claudecode_helpers import resolve_workbench
    for profile in (PYTHON_PROFILE, NODE_PROFILE):
        assert resolve_workbench(profile, "custom:tag") == "custom:tag"


def test_unset_or_empty_workbench_env_falls_to_the_profile():
    from producers._claudecode_helpers import resolve_workbench
    for env in (None, ""):
        assert resolve_workbench(PYTHON_PROFILE, env) == "claude-runner:latest"


# ── rust ────────────────────────────────────────────────────────────────────────────────────

def _rust_prompt(full_name="o/r"):
    from producers._claudecode_helpers import RUST_PROFILE
    return build_prompt(full_name, resolve_base(RUST_PROFILE, None), RUST_PROFILE)


def test_rust_profile_shape():
    from producers._claudecode_helpers import RUST_PROFILE
    assert RUST_PROFILE.key == "rust"
    assert RUST_PROFILE.default_base == "rust:1"
    assert RUST_PROFILE.workbench == "claude-runner-rust:latest"
    # None, NOT a cargo-nextest install: RustLanguage.ensure_cmd curls a prebuilt nextest at
    # measure time, so an install line in the emitted Dockerfile would be dead weight.
    assert RUST_PROFILE.test_runner is None


def test_rust_prompt_names_the_gate_and_its_consequence():
    # RustLanguage.short_circuit_gate is True: if the test target does not compile, nextest never
    # runs and the repo scores zero. The agent must know which command it is being scored on.
    prompt = _rust_prompt()
    assert "cargo test --no-run" in prompt
    assert "ZERO" in prompt
    assert "cargo nextest run" in prompt          # what runs after the gate passes


def test_rust_prompt_pins_the_cargo_location():
    # rust.py hardcodes `export PATH=/usr/local/cargo/bin:$PATH` with no env indirection, and the
    # grader's `bash -l` resets PATH from /etc/profile — so an agent that relocates CARGO_HOME and
    # sets ENV PATH gets that PATH silently discarded and the gate cannot find cargo.
    prompt = _rust_prompt()
    assert "/usr/local/cargo/bin" in prompt
    assert "ENV PATH" in prompt                   # names the trap explicitly


def test_rust_prompt_asks_for_the_compile_and_gives_the_real_reason():
    # The gate re-compiles from scratch in a fresh container. Nothing in the harness INSPECTS the
    # Dockerfile for a compile step, so the prompt must not claim the grader requires one — the
    # actual reason is the exec timeout (docker_client.exec returns rc 124 on expiry, and the
    # scheduler applies a per-child hard wall). Overstating it as a grader rule teaches the agent
    # a false model of the contract, which is how prompts start lying about everything else.
    prompt = _rust_prompt()
    assert "RUN cargo test --no-run" in prompt
    assert "time limit" in prompt
    assert "COMPILES the tests without executing" in prompt
    assert "REQUIRED work your Dockerfile must do" not in prompt


def test_rust_prompt_warns_about_a_missing_root_manifest():
    # `cargo test --no-run` runs at the ROOT. A repo whose crates all live in subdirectories with
    # no root Cargo.toml cannot be gated at all — and that is a property of the repo, not something
    # the agent caused by moving files.
    prompt = _rust_prompt()
    assert "no Cargo.toml at the" in prompt


def test_rust_prompt_states_the_one_turn_constraint():
    prompt = _rust_prompt()
    assert "EXACTLY ONE TURN" in prompt
    assert "FOREGROUND" in prompt


def test_rust_prompt_never_mentions_python_or_node_tooling():
    prompt = _rust_prompt()
    for token in ("pip", "pytest", "npm ci", "package.json"):
        assert token not in prompt


# ── java ────────────────────────────────────────────────────────────────────────────────────

def _java_prompt(full_name="o/r"):
    from producers._claudecode_helpers import JAVA_PROFILE
    return build_prompt(full_name, resolve_base(JAVA_PROFILE, None), JAVA_PROFILE)


def test_java_profile_shape():
    from producers._claudecode_helpers import JAVA_PROFILE
    assert JAVA_PROFILE.key == "java"
    # JAVA_HOME in this image is /opt/java/openjdk, which is exactly java.py's hardcoded default.
    assert JAVA_PROFILE.default_base == "maven:3-eclipse-temurin-17"
    assert JAVA_PROFILE.workbench == "claude-runner-java:latest"
    assert JAVA_PROFILE.test_runner is None      # Surefire/Gradle emit JUnit natively


def test_java_prompt_names_both_gate_branches_and_the_consequence():
    prompt = _java_prompt()
    assert "test-compile" in prompt               # Maven branch
    assert "testClasses" in prompt                # Gradle branch
    assert "ZERO" in prompt


def test_java_prompt_reproduces_the_gate_verbatim_including_the_x_tests():
    # The prompt says "EXACTLY this", so it must actually match java.py: the branch is `[ -f
    # pom.xml ]` and the wrapper choice is `[ -x ./mvnw ]` / `[ -x ./gradlew ]` — EXECUTABLE, not
    # merely present. A wrapper that lost its exec bit falls through to a different-versioned
    # system tool, which is a silent-wrong-answer failure, not a loud one.
    prompt = _java_prompt()
    assert "[ -f pom.xml ]" in prompt
    assert "[ -x ./mvnw ]" in prompt
    assert "[ -x ./gradlew ]" in prompt
    assert "EXECUTABLE, not merely" in prompt


def test_java_prompt_states_the_post_gate_runner_prefers_the_wrapper():
    # java.py run_cmd prefers ./mvnw -B test / ./gradlew test. Saying plain `mvn -B test` would
    # mislead an agent whose repo pins a different Maven via the wrapper.
    prompt = _java_prompt()
    assert "./mvnw -B test" in prompt
    assert "./gradlew test" in prompt


def test_java_prompt_warns_about_a_natively_nested_pom():
    # Not only "do not MOVE the pom" — a repo that already keeps its pom below the root hits the
    # same gate failure with the agent having done nothing wrong.
    prompt = _java_prompt()
    assert "subdirectory" in prompt and "aggregator pom.xml" in prompt


def test_java_prompt_tells_the_agent_env_java_home_is_honored():
    # java.py reads ${JAVA_HOME:-/opt/java/openjdk}. `bash -l` resets PATH but NOT JAVA_HOME, so a
    # Dockerfile ENV JAVA_HOME is respected by the grader — the only escape hatch for a repo that
    # cannot build on JDK 17. No agent would infer this.
    prompt = _java_prompt()
    assert "ENV JAVA_HOME" in prompt


def test_java_prompt_forbids_moving_the_root_build_file():
    # The gate branches on `[ -f pom.xml ]` at the ROOT: a Maven project relocated into a
    # subdirectory falls through to the Gradle branch and fails on a perfectly healthy repo.
    # Case-sensitive on ROOT: the prompt shouts it, and a lowercase assertion would pass on
    # unrelated prose ("the root cause") while the actual warning went missing.
    prompt = _java_prompt()
    assert "pom.xml" in prompt and "ROOT" in prompt


def test_java_prompt_asks_for_the_compile_and_gives_the_real_reason():
    # Same rule as the Rust twin: the grader does not inspect the Dockerfile for a compile step, so
    # the justification is the exec timeout, not an invented requirement.
    prompt = _java_prompt()
    assert "time limit" in prompt
    assert "COMPILES the tests without executing" in prompt
    assert "REQUIRED work your Dockerfile must do" not in prompt


def test_java_prompt_handles_a_wrapperless_gradle_repo():
    # RAT's own java image apt-installs gradle; measured, that yields Gradle 4.4.1 (2017), which
    # cannot build a modern project. So the prompt tells the agent to fetch a real one instead —
    # strictly more capable than the reference environment, not merely at parity with it. And it
    # must say SYMLINK: unpacking a distribution under /usr/local/bin/gradle-8.5/ leaves `gradle`
    # itself unresolvable, which is the obvious way to follow this instruction and still fail.
    prompt = _java_prompt()
    assert "ships no EXECUTABLE ./gradlew" in prompt
    assert "apt-get install gradle" in prompt          # named as the thing NOT to do
    assert "symlink its `bin/gradle`" in prompt


def test_neither_new_prompt_claims_the_grader_passes_no_daemon():
    # java.py passes neither --no-daemon nor any daemon flag. Telling the agent to add one while
    # also saying the gate runs "EXACTLY" as quoted is self-contradictory.
    from producers._claudecode_helpers import JAVA_PROFILE, RUST_PROFILE
    for profile in (JAVA_PROFILE, RUST_PROFILE):
        assert "--no-daemon" not in profile.prompt_template


def test_java_prompt_states_the_one_turn_constraint():
    prompt = _java_prompt()
    assert "EXACTLY ONE TURN" in prompt
    assert "FOREGROUND" in prompt


def test_java_prompt_never_mentions_python_or_node_tooling():
    prompt = _java_prompt()
    for token in ("pip", "pytest", "npm ci", "package.json"):
        assert token not in prompt


# ── the ported prompt must keep the baseline's task, not a paraphrase ────────────────────────
# Ported from sweagent_repo2run_config.yaml's instance_template. These assert the parts that make
# it the SAME TASK as that arm, and the parts that had to change because the grader differs.

def test_python_prompt_carries_the_baselines_task_text():
    p = build_prompt("o/r", "python:3.11", PYTHON_PROFILE)
    for phrase in [
        "We're currently setting up the environment for the following task",
        'The goal is to generate a Dockerfile that can successfully build and run the tests in '
        'the repository using the command "pytest --collect-only -q"',
        "**Read Directory Structure**", "**Check the Configuration Files**",
        "**Determine Package Dependencies**", "**Testing**", "**Generate Dockerfile**",
        "IMPORTANT TIPS:", "Use proper Dockerfile syntax and indentation.",
    ]:
        assert phrase in p, phrase


def test_python_prompt_drops_sweagents_interface_scaffolding():
    # system_template teaches SWE-agent's own REPL; copying it would describe a machine Claude
    # Code does not have.
    p = build_prompt("o/r", "python:3.11", PYTHON_PROFILE)
    for scaffold in ["bash-$", "DISCUSSION", "(Current task:", "{{WINDOW}}", "command_docs"]:
        assert scaffold not in p, scaffold


def test_python_prompt_uses_this_lanes_paths_and_gate():
    p = build_prompt("o/r", "python:3.11", PYTHON_PROFILE)
    assert "/repo" not in p.replace("/repo2run", "")      # repo2run re-homes; this lane does not
    assert "/testbed/Dockerfile.gen" in p
    assert "python -m pytest /testbed --co -q" in p       # what run_pytest_collect.py runs
