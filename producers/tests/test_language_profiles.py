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

_PYTHON_PROMPT_GOLDEN = (
    'You are configuring a Python repository at /testbed so its EXISTING test suite can'
    ' run, and then writing a Dockerfile that reproduces your setup from scratch.\n'
    '\n'
    'First, install ALL Python dependencies and any required system packages so that '
    'pytest can collect and run the tests. Install into the SYSTEM Python using `sudo '
    'pip install ...` and `sudo apt-get install -y ...` for system libraries — do NOT '
    'create a virtualenv (the grader runs the system python3). You may edit '
    'configuration files. DO NOT modify, add, or delete any test files. DO NOT run the '
    'test suite yourself.\n'
    '\n'
    'Then write a self-contained Dockerfile to /testbed/Dockerfile.gen that reproduces '
    'this environment FROM A CLEAN BASE. It MUST:\n'
    '  - start `FROM python:3.11`;\n'
    '  - `RUN git clone https://github.com/o/r /testbed` and `WORKDIR /testbed` (do NOT'
    ' rely on any files from this container — the build starts empty);\n'
    '  - install the SAME system packages and Python dependencies you installed, as RUN'
    ' steps. The build runs as ROOT, so DROP every `sudo` prefix (use `apt-get install '
    '-y ...`, `pip install ...`);\n'
    '  - re-encode any edits you made to repo files as explicit RUN steps (e.g. `RUN '
    'sed -i ...` or a heredoc), since the clone is pristine;\n'
    '  - NOT run pytest or the test suite in the Dockerfile.\n'
    'When the Dockerfile is written, stop.'
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
_NO_PROFILE_YET = {"golang", "go", "java"}


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
