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
    for value in ("rust", "cobol", "", None):
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
    assert "FROM node:20" in prompt
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
    assert resolve_base(NODE_PROFILE, "node:22-bookworm") == "node:22-bookworm"
    assert resolve_base(PYTHON_PROFILE, "python:3.13") == "python:3.13"


def test_unset_or_empty_env_base_falls_to_the_language_default():
    # None is "unset"; "" is an exported-but-empty var, which must not become `FROM `.
    for env_base in (None, ""):
        assert resolve_base(PYTHON_PROFILE, env_base) == "python:3.11"
        assert resolve_base(NODE_PROFILE, env_base) == "node:20"


def test_language_default_base_reaches_the_prompt():
    assert "FROM node:20" in build_prompt("o/r", resolve_base(NODE_PROFILE, None), NODE_PROFILE)
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
_NO_PROFILE_YET = {"golang", "go", "rust", "java"}


def test_profile_aliases_match_the_measure_registry():
    from bench.languages import _REGISTRY

    assert set(_PROFILES) | _NO_PROFILE_YET == set(_REGISTRY)
    assert set(_PROFILES) & _NO_PROFILE_YET == set()


def test_profile_alias_groups_match_the_measure_registry():
    # Key-set parity is not enough: `javascript` must resolve to the SAME language on both sides.
    from bench.languages import _REGISTRY

    for alias, profile in _PROFILES.items():
        assert _REGISTRY[alias].name == profile.key, alias
