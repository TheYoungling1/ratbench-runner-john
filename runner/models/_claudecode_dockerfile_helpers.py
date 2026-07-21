"""Pure, RAT-tree-free helpers for ClaudeCodeDockerfileModel.

Stdlib-only so they unit-test locally without the RAT tree (unlike the model class,
which subclasses BaseEvalModel and is therefore VM-deferred).
"""
import json
import os
import re

DOCKERFILE_GEN_PATH = "/testbed/Dockerfile.gen"
VERIFY_SOURCE = "claudecode_dockerfile_inbuild"
# Deliberately NON-collect-only so attribution.classify credits a real in-sandbox verify.
VERIFY_CMD = "python3 /run_pytest.py (live /testbed)"

_PROMPT_TEMPLATE = (
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


def build_prompt(full_name: str, base: str) -> str:
    """The agentic setup+emit prompt with the repo and emitted-base injected."""
    return _PROMPT_TEMPLATE.format(gen_path=DOCKERFILE_GEN_PATH, base=base, full_name=full_name)


def _passed_count(results_path: str) -> int:
    """summary.passed from a run_pytest_results.json; -1 if missing/malformed."""
    try:
        with open(results_path) as fh:
            summary = (json.load(fh) or {}).get("summary") or {}
        return int(summary.get("passed") or 0)
    except (OSError, ValueError, TypeError):
        return -1


def _collect_ok(collect_path: str) -> bool:
    """run_pytest_collect_results.json['success'] is True; False if missing/malformed."""
    try:
        with open(collect_path) as fh:
            return (json.load(fh) or {}).get("success") is True
    except (OSError, ValueError, TypeError):
        return False


def parse_inbuild(out_dir: str) -> tuple[bool, bool]:
    """(build_success, test_success) from the live in-build result JSONs.

    build_success = the live env collected tests (inbuild collect 'success' True).
    test_success  = a genuine in-sandbox pass (inbuild pytest summary.passed >= 1).
    """
    build_success = _collect_ok(os.path.join(out_dir, "inbuild_run_pytest_collect_results.json"))
    test_success = _passed_count(os.path.join(out_dir, "inbuild_run_pytest_results.json")) >= 1
    return build_success, test_success


def write_instance_json(out_dir: str, full_name: str, build_success: bool, test_success: bool) -> str:
    """Write {owner}__{repo}.json in the attribution-compatible shape; return its path."""
    path = os.path.join(out_dir, full_name.replace("/", "__") + ".json")
    payload = {
        "build_success": bool(build_success),
        "test_success": bool(test_success),
        "logs": {"verified_test_command": VERIFY_CMD, "verification_source": VERIFY_SOURCE},
    }
    with open(path, "w") as fh:
        json.dump(payload, fh)
    return path


def ensure_pytest(dockerfile_text: str) -> str:
    """Append a pytest install if the Dockerfile never mentions pytest (mirrors DockerAgent)."""
    if re.search(r"\bpytest\b", dockerfile_text):
        return dockerfile_text
    return dockerfile_text.rstrip() + "\nRUN pip install --no-cache-dir pytest\n"
