"""Standalone Repo2Run repair port for the RAT runner.

This module is a faithful verbatim copy of all repair-loop symbols from
run_repo2run_benchmark.py, plus the 4 glue symbols (create_openai_client_from_env,
build_resilient_pip_install_run_instruction, build_resilient_apt_install_run_instruction,
TEST_SIGNAL_DETECTOR).

HARD CONSTRAINT: No imports of src.recipe_repair or src.artifact_verify are allowed here.
Permitted src imports: src.synthesizer, src.verification_bundle.

Stage A: All verbatim symbols copied, TODO stubs for B/C functions (junit_to_pytest_results,
real_test_command, _repair_and_rescore).
"""
from __future__ import annotations

import base64
import fnmatch
import json
import os
import posixpath
import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from openai import OpenAI

# Permitted src imports (NOT recipe_repair, NOT artifact_verify)
from src.synthesizer import Synthesizer as _Synthesizer
from src.verification_bundle import derive_supported_verification_bundle


# ---------------------------------------------------------------------------
# Module-level constants (verbatim from run_repo2run_benchmark.py:37-52)
# ---------------------------------------------------------------------------

DOCKER_TIMEOUT_EXIT_CODE = 124
REPO2RUN_PYTEST_COLLECT_COMMAND = "pytest --collect-only -q --disable-warnings"
REPO2RUN_POETRY_COLLECT_COMMAND = "poetry run pytest --collect-only -q --disable-warnings"
REPO2RUN_UV_COLLECT_COMMAND = f"uv run {REPO2RUN_PYTEST_COLLECT_COMMAND}"
REPO2RUN_PDM_COLLECT_COMMAND = f"pdm run {REPO2RUN_PYTEST_COLLECT_COMMAND}"
OBSERVED_PIP_CONSTRAINTS_PATH = "/tmp/jayint-pip-constraints.txt"
TEST_EXECUTION_SHELL_WRAPPER = (
    "if command -v bash >/dev/null 2>&1; then exec bash -s; else exec sh -s; fi"
)
PYTORCH_CPU_INDEX_URL = "https://download.pytorch.org/whl/cpu"
DOCKERFILE_REPAIR_LOG_LIMIT = 12000
DOCKERFILE_REPAIR_SYSTEM_PROMPT = """You are a bounded Dockerfile repair agent.

You receive a Dockerfile that was generated from a successful sandbox setup trajectory, plus the fresh Docker build/test failure feedback.
Your job is to repair only the Dockerfile so the fresh image can reproduce the sandbox setup and run the provided test command.

Rules:
1. Output JSON only with keys: dockerfile, rationale, confidence.
2. `dockerfile` must be the full replacement Dockerfile text, not a patch.
3. Do not modify target repository source code outside Dockerfile commands.
4. Do not invent a new setup strategy unless the trajectory evidence is insufficient.
5. Prefer restoring omitted successful setup commands from agent_run_summary in the original trajectory order.
6. Preserve command order. Do not merge, sort, hoist, or rewrite successful setup commands for convenience.
7. Fix replay gaps such as missing installs, lost ENV/WORKDIR/SHELL context, build/runtime split mistakes, or Dockerfile syntax errors.
8. Do not remove an existing Dockerfile RUN command unless the logs clearly prove it is wrong or duplicate.
9. Keep the existing base image and repository copy semantics unless the failure directly requires a change.
10. Do not emit raw multi-line RUN commands. Multi-line shell/Python/file-write content must be encoded into a single valid RUN instruction or otherwise rendered with Dockerfile-safe syntax.
11. Treat `agent_run_summary.build_recipe.build_commands` as the authoritative replay order. If a successful command edited files, created symlinks, installed packages, or patched stubs, preserve that exact command text unless Dockerfile syntax alone forces escaping.
12. Do not replace an observed successful file patch or stub with your own equivalent implementation. The goal is reproduction of the sandbox trajectory, not a cleaner independent solution.
13. Do not try to fix a test-command runtime wrapper by adding a final Dockerfile `RUN` test. If the provided test command uses a wrapper such as `xvfb-run`, preserve the test command outside the Dockerfile.

`confidence` must be one of: "high", "medium", "low".
"""

DOCKERFILE_REPAIR_USER_PROMPT = """Repair the Dockerfile using the failure feedback and trajectory evidence.

Input JSON:
```json
{repair_input_json}
```
"""

# run_repo2run_benchmark.py:832-834
_DOCKERFILE_VARIABLE_RE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)

# run_repo2run_benchmark.py:997-1019
_PIP_INSTALL_OPTION_VALUE_FLAGS = {
    "-c",
    "--constraint",
    "-i",
    "--index-url",
    "--extra-index-url",
    "-f",
    "--find-links",
    "--trusted-host",
    "--platform",
    "--python-version",
    "--implementation",
    "--abi",
    "--root",
    "--prefix",
    "--target",
    "--src",
    "--upgrade-strategy",
    "--config-settings",
    "-C",
    "--global-option",
    "--compile-option",
}

# run_repo2run_benchmark.py:1067-1073
_SUCCESSFULLY_INSTALLED_BLOCK_RE = re.compile(
    r"^[ \t]*Successfully installed[ \t]+(?P<packages>[^\r\n]*)",
    flags=re.MULTILINE,
)
_INSTALLED_PACKAGE_TOKEN_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)-(?P<version>[0-9](?:[A-Za-z0-9_.!+~-]*[A-Za-z0-9!+~])?)$"
)

# run_repo2run_benchmark.py:1205
_SHELL_CONTROL_TOKENS = {"&&", "||", ";", "|"}

# run_repo2run_benchmark.py:1585-1593
_CUDA_SKIPPED_LOCAL_SOURCE_INSTALL_RE = re.compile(
    r"(?P<install>"
    r"(?:[A-Za-z_][A-Za-z0-9_]*_)?SKIP_CUDA_BUILD=TRUE\s+"
    r"(?:(?:python(?:2|3)?(?:\.\d+)?\s+-m\s+pip)|pip3?|uv\s+pip)\s+"
    r"install\s+\."
    r"(?:(?!\s(?:&&|\|\||;|\|)\s).)*"
    r")"
    r"(?=$|\s(?:&&|\|\||;|\|)\s)",
)

# run_repo2run_benchmark.py:2155-2167
UNSAFE_COLLECT_COMMAND_SUBSTRINGS = (
    "&&",
    "||",
    ";",
    "|",
    ">",
    "<",
    "`",
    "$(",
    "\n",
    "\r",
)
DISALLOWED_COLLECT_TOKENS = {"tail", "head", "grep"}

# run_repo2run_benchmark.py:2563-2569
_MISSING_PYTHON_MODULE_RE = re.compile(
    r"(?:ModuleNotFoundError|ImportError):\s+No module named ['\"](?P<module>[^'\"]+)['\"]"
)
_KNOWN_MISSING_MODULE_PACKAGE_FALLBACKS = {
    "ppocr": ("paddleocr", "paddleocr==2.7.3"),
    "ppstructure": ("paddleocr", "paddleocr==2.7.3"),
}

# GLUE: module-level Synthesizer singleton (permitted src.synthesizer import)
TEST_SIGNAL_DETECTOR = _Synthesizer()


# ---------------------------------------------------------------------------
# Lowest-level pure helpers (verbatim)
# ---------------------------------------------------------------------------


def _decode_command_stream(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def normalize_command_list(commands: Any) -> list[str]:
    if isinstance(commands, str):
        commands = [commands]
    normalized: list[str] = []
    for command in commands or []:
        text = str(command or "").strip()
        if text:
            normalized.append(text)
    return normalized


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _strip_requirement_line(line: str) -> str:
    stripped = (line or "").strip()
    if not stripped or stripped.startswith("#"):
        return ""
    if " #" in stripped:
        stripped = stripped.split(" #", 1)[0].strip()
    return stripped


def _normalize_pip_constraint_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name or "").strip()).lower()


def _pip_requirement_name(requirement: str) -> str:
    token = str(requirement or "").strip()
    if not token or token.startswith(("-", ".", "/", "git+", "http://", "https://")):
        return ""
    name = re.split(r"\[|==|!=|~=|>=|<=|>|<|@", token, maxsplit=1)[0].strip()
    return name.lower().replace("_", "-")


def _split_pip_install_command(command: str) -> tuple[list[str], list[str], list[str]] | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None

    try:
        install_index = tokens.index("install")
    except ValueError:
        return None

    prefix = tokens[: install_index + 1]
    tail = tokens[install_index + 1 :]
    options: list[str] = []
    requirements: list[str] = []
    index = 0
    while index < len(tail):
        token = tail[index]
        if token == "--":
            requirements.extend(tail[index + 1 :])
            break
        if token.startswith("-"):
            options.append(token)
            option_name = token.split("=", 1)[0]
            if option_name in _PIP_INSTALL_OPTION_VALUE_FLAGS and "=" not in token and index + 1 < len(tail):
                index += 1
                options.append(tail[index])
            index += 1
            continue
        requirements.append(token)
        index += 1

    return prefix, options, requirements


def _pip_installed_requirement_names(command: str) -> set[str]:
    parsed = _split_pip_install_command(command)
    if not parsed:
        return set()
    _, _, requirements = parsed
    return {
        name
        for requirement in requirements
        if (name := _pip_requirement_name(requirement))
    }


def _normalize_dockerfile_path_value(value: str) -> str:
    normalized = (value or "").strip()
    if (
        (normalized.startswith('"') and normalized.endswith('"'))
        or (normalized.startswith("'") and normalized.endswith("'"))
    ):
        normalized = normalized[1:-1]
    normalized = normalized.replace("${HOME}", "/root").replace("$HOME", "/root")
    if normalized.startswith("~/"):
        normalized = "/root/" + normalized[2:]
    normalized = normalized.replace("${PATH}", "$PATH").replace("$PATH", "${PATH}")
    return normalized


def _parse_dockerfile_env_instruction(stripped_line: str) -> dict[str, str]:
    if not stripped_line.upper().startswith("ENV "):
        return {}
    payload = stripped_line.split(None, 1)[1].strip()
    if not payload:
        return {}

    try:
        tokens = shlex.split(payload)
    except ValueError:
        tokens = payload.split()
    if not tokens:
        return {}

    if "=" not in tokens[0]:
        if len(tokens) < 2:
            return {}
        return {tokens[0]: " ".join(tokens[1:])}

    env: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if key:
            env[key] = value
    return env


def _expand_dockerfile_variables(value: str, env: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("bare") or ""
        return env.get(name, match.group(0))

    return _DOCKERFILE_VARIABLE_RE.sub(replace, value or "")


def _normalize_dockerfile_workdir(value: str, env: dict[str, str], current_workdir: str) -> str:
    workdir = _normalize_dockerfile_path_value(_expand_dockerfile_variables(value, env))
    if not workdir:
        return current_workdir or "/app"
    if workdir.startswith("/"):
        return posixpath.normpath(workdir)
    base = current_workdir if (current_workdir or "").startswith("/") else "/"
    return posixpath.normpath(posixpath.join(base, workdir))


def infer_workdir_from_dockerfile(dockerfile_text: str) -> str:
    env: dict[str, str] = {}
    workdir = "/app"
    for line in dockerfile_text.splitlines():
        stripped = line.strip()
        env_updates = _parse_dockerfile_env_instruction(stripped)
        if env_updates:
            for key, value in env_updates.items():
                env[key] = _expand_dockerfile_variables(value, env)
            continue
        if stripped.upper().startswith("WORKDIR "):
            workdir = _normalize_dockerfile_workdir(
                stripped.split(None, 1)[1].strip(),
                env,
                workdir,
            )
    return workdir


def _pip_install_command_has_constraint(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "--constraint" in command or " -c " in f" {command} "
    return any(token == "-c" or token == "--constraint" or token.startswith("--constraint=") for token in tokens)


def _pip_install_command_needs_observed_constraints(command: str) -> bool:
    if not _is_bare_pip_install_command(command) or _pip_install_command_has_constraint(command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    try:
        install_index = tokens.index("install")
    except ValueError:
        return False

    tail = tokens[install_index + 1 :]
    for index, token in enumerate(tail):
        if token in {"-e", "--editable"} and index + 1 < len(tail):
            target = tail[index + 1]
            return target.startswith((".", "/", "~"))
        if token.startswith("-e.") or token.startswith("--editable=."):
            return True
    return False


def _add_observed_constraints_to_pip_command(command: str, pip_constraints: dict[str, str]) -> str:
    if not pip_constraints or not _pip_install_command_needs_observed_constraints(command):
        return command
    return f"{command} --constraint {shlex.quote(OBSERVED_PIP_CONSTRAINTS_PATH)}"


def _is_bare_pip_install_command(command: str) -> bool:
    normalized = " ".join(str(command or "").split()).strip()
    if not normalized or "JAYINT_PIP_ATTEMPT" in normalized:
        return False
    if any(operator in normalized for operator in ("&&", "||", ";", "|")):
        return False
    return bool(
        re.match(
            r"^(?:python(?:2|3)?(?:\.\d+)?\s+-m\s+pip|pip3?|uv\s+pip)\s+install\b",
            normalized,
        )
    )


def _is_bare_uv_pip_install_command(command: str) -> bool:
    if not _is_bare_pip_install_command(command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = str(command or "").split()
    if not tokens:
        return False

    try:
        install_index = tokens.index("install")
    except ValueError:
        return False

    package_tokens = [
        token
        for token in tokens[install_index + 1 :]
        if token and not token.startswith("-")
    ]
    return any(re.match(r"^uv(?:[<>=!~].*)?$", token) for token in package_tokens)


def _is_uv_shell_installer_command(command: str) -> bool:
    normalized = " ".join(str(command or "").split()).strip()
    return "astral.sh/uv/install.sh" in normalized and re.search(r"\bsh\b", normalized) is not None


def _is_generated_uv_pip_retry_command(command: str) -> bool:
    normalized = " ".join(str(command or "").split()).strip()
    if "JAYINT_PIP_ATTEMPT" not in normalized or "astral.sh/uv/install.sh" in normalized:
        return False
    return bool(re.search(r"\bpip\s+install\s+uv(?:[<>=!~][^'\"\s]*)?", normalized))


def _is_apt_install_replay_command(command: str) -> bool:
    normalized = " ".join(str(command or "").split()).strip()
    if not normalized or "JAYINT_APT_ATTEMPT" in normalized:
        return False
    return bool(
        re.search(
            r"(?:^|&&|\|\||;|\()\s*(?:sudo\s+)?apt(?:-get)?\s+install\b",
            normalized,
        )
    )


def _extract_generated_pip_retry_inner_command(command: str) -> str | None:
    normalized = str(command or "")
    if "JAYINT_PIP_ATTEMPT" not in normalized or "/bin/sh" not in normalized:
        return None
    try:
        tokens = shlex.split(normalized)
    except ValueError:
        return None
    for index, token in enumerate(tokens[:-1]):
        if token == "-lc" and _is_bare_pip_install_command(tokens[index + 1]):
            return tokens[index + 1]
    return None


def _extract_generated_apt_retry_inner_command(command: str) -> str | None:
    normalized = str(command or "")
    if "JAYINT_APT_ATTEMPT" not in normalized or "/bin/sh" not in normalized:
        return None
    try:
        tokens = shlex.split(normalized)
    except ValueError:
        return None
    for index, token in enumerate(tokens[:-1]):
        if token == "-lc" and _is_apt_install_replay_command(tokens[index + 1]):
            return tokens[index + 1]
    return None


def _extract_generated_retry_inner_shell_command(command: str) -> str | None:
    normalized = str(command or "")
    if "JAYINT_PIP_ATTEMPT" not in normalized or "/bin/sh" not in normalized:
        return None
    try:
        tokens = shlex.split(normalized)
    except ValueError:
        return None
    for index, token in enumerate(tokens[:-1]):
        if token == "-lc":
            return tokens[index + 1]
    return None


def _iter_pip_install_segments(command: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    segments: list[str] = []
    index = 0
    while index < len(tokens):
        prefix_end: int | None = None
        if re.match(r"^(?:pip3?|uv)$", tokens[index]):
            if index + 1 < len(tokens) and tokens[index + 1] == "install":
                prefix_end = index + 2
        elif re.match(r"^python(?:2|3)?(?:\.\d+)?$", tokens[index]):
            if (
                index + 3 < len(tokens)
                and tokens[index + 1] == "-m"
                and tokens[index + 2] == "pip"
                and tokens[index + 3] == "install"
            ):
                prefix_end = index + 4

        if prefix_end is None:
            index += 1
            continue

        end = prefix_end
        while end < len(tokens) and tokens[end] not in _SHELL_CONTROL_TOKENS:
            end += 1
        segments.append(shlex.join(tokens[index:end]))
        index = end + 1
    return segments


def _local_pip_install_project_names(command: str) -> set[str]:
    names: set[str] = set()
    for segment in _iter_pip_install_segments(command):
        parsed = _split_pip_install_command(segment)
        if not parsed:
            continue
        _, _, requirements = parsed
        for requirement in requirements:
            token = requirement.rstrip("/")
            if not token.startswith(("./", "../", "/", "~")):
                continue
            name = Path(token).name.strip().lower().replace("_", "-")
            if name:
                names.add(name)
    return names


def _drop_reinstalled_local_projects(command: str, local_project_names: set[str]) -> str | None:
    if not local_project_names or not _is_bare_pip_install_command(command):
        return command
    parsed = _split_pip_install_command(command)
    if not parsed:
        return command
    prefix, options, requirements = parsed
    if not requirements:
        return command

    kept_requirements = [
        requirement
        for requirement in requirements
        if _pip_requirement_name(requirement) not in local_project_names
    ]
    if kept_requirements == requirements:
        return command
    if not kept_requirements:
        return None
    return shlex.join([*prefix, *options, *kept_requirements])


def _is_exact_torch_requirement(requirement: str) -> bool:
    return _pip_requirement_name(requirement) == "torch" and bool(
        re.search(r"(?:^torch(?:\[[^\]]+\])?)\s*(?:===|==)", str(requirement or ""))
    )


def _is_broad_torch_requirement(requirement: str) -> bool:
    if _pip_requirement_name(requirement) != "torch":
        return False
    return not _is_exact_torch_requirement(requirement)


def _is_torch_cpu_split_candidate(requirement: str) -> bool:
    return _is_broad_torch_requirement(requirement)


def _exact_torch_requirements(command: str) -> list[str]:
    requirements: list[str] = []
    for segment in _iter_pip_install_segments(command):
        parsed = _split_pip_install_command(segment)
        if not parsed:
            continue
        _, _, segment_requirements = parsed
        requirements.extend(
            requirement
            for requirement in segment_requirements
            if _is_exact_torch_requirement(requirement)
        )
    return requirements


def _compatible_torchvision_requirement(torch_requirement: str | None) -> str | None:
    token = str(torch_requirement or "")
    match = re.search(r"==\s*([0-9]+)\.([0-9]+)(?:\.[0-9]+)?", token)
    if not match:
        return None
    major_minor = f"{match.group(1)}.{match.group(2)}"
    return {
        "2.6": "torchvision==0.21.0",
        "2.7": "torchvision==0.22.0",
        "2.8": "torchvision==0.23.0",
        "2.9": "torchvision==0.24.0",
        "2.10": "torchvision==0.25.0",
        "2.11": "torchvision==0.26.0",
        "2.12": "torchvision==0.27.0",
    }.get(major_minor)


def _pip_command_installs_torch_replacement(command: str) -> bool:
    for segment in _iter_pip_install_segments(command):
        parsed = _split_pip_install_command(segment)
        if not parsed:
            continue
        _, _, requirements = parsed
        for requirement in requirements:
            requirement_name = _pip_requirement_name(requirement)
            if requirement_name == "mosaicml":
                return True
            if _is_exact_torch_requirement(requirement):
                return True
    return False


def _pip_command_installs_mosaicml_stack(command: str) -> bool:
    for segment in _iter_pip_install_segments(command):
        parsed = _split_pip_install_command(segment)
        if not parsed:
            continue
        _, _, requirements = parsed
        if any(_pip_requirement_name(requirement) in {"mosaicml", "mosaicml-streaming"} for requirement in requirements):
            return True
    return False


def _dockerfile_exact_torch_replacement_requirement(lines: list[str]) -> str | None:
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("RUN "):
            continue
        command = stripped[4:].strip()
        generated_pip_command = _extract_generated_pip_retry_inner_command(command)
        if generated_pip_command:
            exact_requirements = _exact_torch_requirements(generated_pip_command)
            if exact_requirements:
                return exact_requirements[0]
        exact_requirements = _exact_torch_requirements(command)
        if exact_requirements:
            return exact_requirements[0]
    return None


def _dockerfile_contains_torch_replacement(lines: list[str]) -> bool:
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("RUN "):
            continue
        command = stripped[4:].strip()
        generated_pip_command = _extract_generated_pip_retry_inner_command(command)
        if generated_pip_command and _pip_command_installs_torch_replacement(generated_pip_command):
            return True
        if _pip_command_installs_torch_replacement(command):
            return True
    return False


def _dockerfile_contains_mosaicml_stack(lines: list[str]) -> bool:
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("RUN "):
            continue
        command = stripped[4:].strip()
        generated_pip_command = _extract_generated_pip_retry_inner_command(command)
        if generated_pip_command and _pip_command_installs_mosaicml_stack(generated_pip_command):
            return True
        if _pip_command_installs_mosaicml_stack(command):
            return True
    return False


def _drop_redundant_broad_torch_bootstrap(
    command: str,
    *,
    torch_replacement_available: bool,
    exact_torch_replacement_requirement: str | None = None,
    compatible_torchvision_requirement: str | None = None,
) -> str | None:
    if not torch_replacement_available or not _is_bare_pip_install_command(command):
        return command
    parsed = _split_pip_install_command(command)
    if not parsed:
        return command
    prefix, options, requirements = parsed
    if not requirements:
        return command

    kept_requirements: list[str] = []
    inserted_exact_torch = False
    for requirement in requirements:
        if not _is_broad_torch_requirement(requirement):
            kept_requirements.append(requirement)
            continue
        if exact_torch_replacement_requirement and not inserted_exact_torch:
            kept_requirements.append(exact_torch_replacement_requirement)
            if (
                compatible_torchvision_requirement
                and not any(_pip_requirement_name(item) == "torchvision" for item in requirements)
            ):
                kept_requirements.append(compatible_torchvision_requirement)
            inserted_exact_torch = True
    if kept_requirements == requirements:
        return command
    if not kept_requirements:
        return None
    return shlex.join([*prefix, *options, *kept_requirements])


def _add_compatible_torchvision_constraint(
    command: str,
    compatible_torchvision_requirement: str | None,
) -> str:
    if not compatible_torchvision_requirement or not _is_bare_pip_install_command(command):
        return command
    parsed = _split_pip_install_command(command)
    if not parsed:
        return command
    prefix, options, requirements = parsed
    if not requirements:
        return command
    if not any(_is_exact_torch_requirement(requirement) for requirement in requirements):
        return command
    if any(_pip_requirement_name(requirement) == "torchvision" for requirement in requirements):
        return command

    rewritten_requirements: list[str] = []
    inserted = False
    for requirement in requirements:
        rewritten_requirements.append(requirement)
        if _is_exact_torch_requirement(requirement) and not inserted:
            rewritten_requirements.append(compatible_torchvision_requirement)
            inserted = True
    return shlex.join([*prefix, *options, *rewritten_requirements])


def _is_redundant_exact_torch_reinstall(
    command: str,
    installed_exact_torch_requirement: str | None,
) -> bool:
    if not installed_exact_torch_requirement or not _is_bare_pip_install_command(command):
        return False
    parsed = _split_pip_install_command(command)
    if not parsed:
        return False
    _, _, requirements = parsed
    exact_requirements = [
        requirement for requirement in requirements if _is_exact_torch_requirement(requirement)
    ]
    return requirements == exact_requirements and exact_requirements == [installed_exact_torch_requirement]


def _is_cuda_local_installer_scaffolding_command(command: str) -> bool:
    normalized = " ".join(str(command or "").split()).strip()
    if re.fullmatch(r"mkdir\s+-p\s+/tmp/cuda/?", normalized):
        return True
    return (
        "developer.download.nvidia.com/compute/cuda" in normalized
        and ".run" in normalized
        and re.search(r"\b(?:curl|wget)\b", normalized) is not None
    )


def _rewrite_absolute_tests_redirect_to_workdir(command: str, workdir: str) -> str:
    normalized_workdir = (workdir or "/app").rstrip("/") or "/"
    if normalized_workdir == "/":
        return command
    return re.sub(
        r"(?P<redirect>>>?|tee\s+)(?P<space>\s*)/tests/",
        rf"\g<redirect>\g<space>{normalized_workdir}/tests/",
        command,
    )


def _harden_cuda_skipped_local_source_install(command: str) -> str:
    """Prevent CUDA-skipped source installs from re-resolving heavy GPU deps."""

    def add_no_deps(match: re.Match[str]) -> str:
        install_segment = match.group("install")
        if "--no-deps" in install_segment.split():
            return install_segment
        return f"{install_segment} --no-deps"

    return _CUDA_SKIPPED_LOCAL_SOURCE_INSTALL_RE.sub(add_no_deps, command)


def _drop_replay_poetry_lock_command(command: str, *, poetry_lock_available: bool) -> str | None:
    """Avoid re-solving Poetry dependencies when a lockfile is already present."""
    if not poetry_lock_available:
        return command
    normalized = " ".join(str(command or "").split()).strip()
    if not re.search(r"(^|\s)poetry\s+lock\b", normalized):
        return command

    lock_pattern = r"poetry\s+lock(?:\s+--no-update)?(?:\s+2>&1)?"
    if re.fullmatch(lock_pattern, normalized):
        return None
    rewritten = re.sub(rf"^{lock_pattern}\s*&&\s*", "", normalized)
    rewritten = re.sub(rf"\s*&&\s*{lock_pattern}(?=\s*(?:&&|$))", "", rewritten)
    return rewritten.strip() or None


def _dockerfile_may_include_poetry_lock(dockerfile_text: str) -> bool:
    text = str(dockerfile_text or "")
    if "poetry.lock" in text:
        return True
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        try:
            tokens = shlex.split(stripped)
        except ValueError:
            tokens = stripped.split()
        if len(tokens) >= 3 and "." in tokens[1:-1]:
            return True
    return False


def _repair_generated_apt_retry_status_variables(command: str) -> str:
    if "JAYINT_APT_ATTEMPT" not in (command or ""):
        return command
    if "JAYINT_PIP_STATUS" not in command and "JAYINT_PIP_MAX_ATTEMPTS" not in command:
        return command
    return (
        command.replace("JAYINT_PIP_STATUS", "JAYINT_APT_STATUS")
        .replace("JAYINT_PIP_MAX_ATTEMPTS", "JAYINT_APT_MAX_ATTEMPTS")
    )


def _add_no_deps_to_known_force_reinstall(
    command: str,
    installed_package_names: set[str],
) -> str:
    if not installed_package_names or not _is_bare_pip_install_command(command):
        return command
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    if "--force-reinstall" not in tokens or "--no-deps" in tokens:
        return command

    parsed = _split_pip_install_command(command)
    if not parsed:
        return command
    prefix, options, requirements = parsed
    requirement_names = [_pip_requirement_name(requirement) for requirement in requirements]
    if not requirement_names or not all(name in installed_package_names for name in requirement_names):
        return command
    return shlex.join([*prefix, *options, "--no-deps", *requirements])


def _shell_single_quote(value: str) -> str:
    return "'" + (value or "").replace("'", "'\"'\"'") + "'"


def _has_unclosed_shell_quote(value: str) -> bool:
    in_single = False
    in_double = False
    escaped = False
    for char in value or "":
        if escaped:
            escaped = False
            continue
        if char == "\\" and not in_single:
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
    return in_single or in_double


def _format_multiline_run_as_script(command: str, script_index: int) -> str:
    encoded = base64.b64encode((command or "").encode("utf-8")).decode("ascii")
    script_path = f"/tmp/jayint_eval_run_{script_index}.sh"
    return (
        f"RUN printf '%s' {_shell_single_quote(encoded)} "
        f"| base64 -d > {script_path} "
        f"&& chmod +x {script_path} "
        f"&& /bin/sh {script_path}"
    )


def _is_top_level_dockerfile_instruction(line: str) -> bool:
    if not line or line[:1].isspace():
        return False
    return bool(
        re.match(
            r"^(?:ADD|ARG|CMD|COPY|ENTRYPOINT|ENV|EXPOSE|FROM|HEALTHCHECK|LABEL|ONBUILD|RUN|"
            r"SHELL|STOPSIGNAL|USER|VOLUME|WORKDIR)\b",
            line,
            flags=re.IGNORECASE,
        )
    )


def _join_dockerfile_continued_lines(lines: list[str]) -> str:
    segments: list[str] = []
    for line in lines:
        segment = line.rstrip()
        if segment.endswith("\\"):
            segment = segment[:-1].rstrip()
        segment = segment.strip()
        if segment:
            segments.append(segment)
    return " ".join(segments)


def _collect_continued_dockerfile_instruction(
    lines: list[str],
    start_index: int,
) -> Optional[tuple[str, int]]:
    line = lines[start_index]
    if not _is_top_level_dockerfile_instruction(line) or not line.rstrip().endswith("\\"):
        return None

    command_lines = [line]
    index = start_index + 1
    while index < len(lines):
        command_lines.append(lines[index])
        index += 1
        if not command_lines[-1].rstrip().endswith("\\"):
            break

    return _join_dockerfile_continued_lines(command_lines), index


def _collect_raw_multiline_run(lines: list[str], start_index: int) -> Optional[tuple[str, int]]:
    line = lines[start_index]
    match = re.match(r"^\s*RUN\s+(.*)$", line, flags=re.IGNORECASE)
    if not match:
        return None

    first_command_line = match.group(1)
    if line.rstrip().endswith("\\") or not _has_unclosed_shell_quote(first_command_line):
        return None

    command_lines = [first_command_line]
    combined = first_command_line
    index = start_index + 1
    while index < len(lines):
        if len(command_lines) > 1 and _is_top_level_dockerfile_instruction(lines[index]):
            return "\n".join(command_lines), index
        command_lines.append(lines[index])
        combined += "\n" + lines[index]
        index += 1
        if not _has_unclosed_shell_quote(combined):
            break

    if len(command_lines) <= 1:
        return None
    return "\n".join(command_lines), index


def _collect_generated_apt_retry_with_orphan_continuations(
    lines: list[str],
    start_index: int,
) -> Optional[tuple[str, int]]:
    """Repair stale retry wrappers that stranded a multiline apt package list."""

    line = lines[start_index]
    match = re.match(r"^\s*RUN\s+(.*)$", line, flags=re.IGNORECASE)
    if not match:
        return None
    inner_command = _extract_generated_apt_retry_inner_command(match.group(1).strip())
    if not inner_command or not inner_command.rstrip().endswith("\\"):
        return None

    command_lines = [inner_command]
    index = start_index + 1
    while index < len(lines):
        if _is_top_level_dockerfile_instruction(lines[index]):
            break
        command_lines.append(lines[index])
        index += 1
        if not command_lines[-1].rstrip().endswith("\\"):
            break

    if len(command_lines) <= 1:
        return None
    repaired_command = _join_dockerfile_continued_lines(command_lines)
    if not _is_apt_install_replay_command(repaired_command):
        return None
    return build_resilient_apt_install_run_instruction(repaired_command), index


def _render_observed_pip_constraints_instruction(pip_constraints: dict[str, str]) -> str:
    constraint_lines = [
        f"{name}=={version}"
        for name, version in sorted((pip_constraints or {}).items())
        if name and version
    ]
    quoted_lines = " ".join(shlex.quote(line) for line in constraint_lines)
    return f"RUN printf '%s\\n' {quoted_lines} > {OBSERVED_PIP_CONSTRAINTS_PATH}"


# ---------------------------------------------------------------------------
# GLUE-COPY: build_resilient_pip_install_run_instruction
# Copied verbatim from src/synthesizer.py:265-294, inlining _quote_shell_single.
# (identical to _shell_single_quote defined above)
# ---------------------------------------------------------------------------

_DEFAULT_PIP_INSTALL_REPLAY_ATTEMPTS = 3
_DEFAULT_PIP_INSTALL_RETRY_DELAY_SECONDS = 5


def build_resilient_pip_install_run_instruction(
    command: str,
    max_attempts: int = _DEFAULT_PIP_INSTALL_REPLAY_ATTEMPTS,
    retry_delay_seconds: int = _DEFAULT_PIP_INSTALL_RETRY_DELAY_SECONDS,
) -> str:
    if not command or not command.strip():
        raise ValueError("command must be a non-empty pip install invocation")

    attempts = max(1, int(max_attempts or _DEFAULT_PIP_INSTALL_REPLAY_ATTEMPTS))
    delay = max(0, int(retry_delay_seconds or _DEFAULT_PIP_INSTALL_RETRY_DELAY_SECONDS))
    quoted_command = _shell_single_quote(command.strip())

    return (
        "RUN JAYINT_PIP_ATTEMPT=1; "
        f"JAYINT_PIP_MAX_ATTEMPTS={attempts}; "
        "JAYINT_PIP_STATUS=1; "
        "while [ \"$JAYINT_PIP_ATTEMPT\" -le \"$JAYINT_PIP_MAX_ATTEMPTS\" ]; do "
        f"PIP_NO_CACHE_DIR=1 /bin/sh -lc {quoted_command} && JAYINT_PIP_STATUS=0 && break; "
        "JAYINT_PIP_STATUS=$?; "
        "(python -m pip cache purge >/dev/null 2>&1 || "
        "python3 -m pip cache purge >/dev/null 2>&1 || "
        "pip cache purge >/dev/null 2>&1 || true); "
        "if [ \"$JAYINT_PIP_ATTEMPT\" -eq \"$JAYINT_PIP_MAX_ATTEMPTS\" ]; then "
        "exit \"$JAYINT_PIP_STATUS\"; "
        "fi; "
        "JAYINT_PIP_ATTEMPT=$((JAYINT_PIP_ATTEMPT + 1)); "
        f"sleep {delay}; "
        "done; "
        "exit \"$JAYINT_PIP_STATUS\""
    )


# ---------------------------------------------------------------------------
# GLUE-COPY: build_resilient_apt_install_run_instruction
# Copied verbatim from src/synthesizer.py:321-352, inlining helpers.
# ---------------------------------------------------------------------------

_DEFAULT_APT_INSTALL_REPLAY_ATTEMPTS = 3
_DEFAULT_APT_INSTALL_RETRY_DELAY_SECONDS = 5


def _normalize_apt_install_replay_command(command: str) -> str:
    normalized = " ".join(str(command or "").split()).strip()
    normalized = re.sub(r"(^|(?:&&|\|\||;|\()\s*)sudo\s+apt", r"\1apt", normalized)
    has_update = re.search(
        r"(?:^|&&|\|\||;|\()\s*apt(?:-get)?\s+update\b",
        normalized,
    )
    if has_update:
        return normalized
    return f"apt-get update && {normalized}"


def build_resilient_apt_install_run_instruction(
    command: str,
    max_attempts: int = _DEFAULT_APT_INSTALL_REPLAY_ATTEMPTS,
    retry_delay_seconds: int = _DEFAULT_APT_INSTALL_RETRY_DELAY_SECONDS,
) -> str:
    if not command or not command.strip():
        raise ValueError("command must be a non-empty apt install invocation")

    attempts = max(1, int(max_attempts or _DEFAULT_APT_INSTALL_REPLAY_ATTEMPTS))
    delay = max(0, int(retry_delay_seconds or _DEFAULT_APT_INSTALL_RETRY_DELAY_SECONDS))
    replay_command = _normalize_apt_install_replay_command(command)
    quoted_command = _shell_single_quote(replay_command)

    return (
        "RUN JAYINT_APT_ATTEMPT=1; "
        f"JAYINT_APT_MAX_ATTEMPTS={attempts}; "
        "JAYINT_APT_STATUS=1; "
        "while [ \"$JAYINT_APT_ATTEMPT\" -le \"$JAYINT_APT_MAX_ATTEMPTS\" ]; do "
        "rm -rf /var/lib/apt/lists/*; "
        f"DEBIAN_FRONTEND=noninteractive /bin/sh -lc {quoted_command} "
        "&& JAYINT_APT_STATUS=0 && break; "
        "JAYINT_APT_STATUS=$?; "
        "(apt-get clean >/dev/null 2>&1 || true); "
        "rm -rf /var/lib/apt/lists/*; "
        "if [ \"$JAYINT_APT_ATTEMPT\" -eq \"$JAYINT_APT_MAX_ATTEMPTS\" ]; then "
        "exit \"$JAYINT_APT_STATUS\"; "
        "fi; "
        "JAYINT_APT_ATTEMPT=$((JAYINT_APT_ATTEMPT + 1)); "
        f"sleep {delay}; "
        "done; "
        "exit \"$JAYINT_APT_STATUS\""
    )


def build_resilient_uv_install_run_instruction(pip_command: str = "pip install uv") -> str:
    if not _is_bare_uv_pip_install_command(pip_command):
        pip_command = "pip install uv"
    attempts = 3
    delay = 5
    quoted_pip_command = shlex.quote(pip_command.strip())
    return (
        "RUN JAYINT_PIP_ATTEMPT=1; "
        f"JAYINT_PIP_MAX_ATTEMPTS={attempts}; "
        "JAYINT_PIP_STATUS=1; "
        "while [ \"$JAYINT_PIP_ATTEMPT\" -le \"$JAYINT_PIP_MAX_ATTEMPTS\" ]; do "
        f"PIP_NO_CACHE_DIR=1 /bin/sh -lc {quoted_pip_command} && JAYINT_PIP_STATUS=0 && break; "
        "JAYINT_PIP_STATUS=$?; "
        "(python -m pip cache purge >/dev/null 2>&1 || "
        "python3 -m pip cache purge >/dev/null 2>&1 || "
        "pip cache purge >/dev/null 2>&1 || true); "
        "if [ \"$JAYINT_PIP_ATTEMPT\" -eq \"$JAYINT_PIP_MAX_ATTEMPTS\" ]; then "
        "break; "
        "fi; "
        "JAYINT_PIP_ATTEMPT=$((JAYINT_PIP_ATTEMPT + 1)); "
        f"sleep {delay}; "
        "done; "
        "if [ \"$JAYINT_PIP_STATUS\" -ne 0 ]; then "
        "curl -L --retry 5 --retry-delay 2 --retry-connrefused --fail --show-error --silent "
        "-o /tmp/jayint-uv-install.sh https://astral.sh/uv/install.sh "
        "&& sh /tmp/jayint-uv-install.sh; "
        "fi"
    )


def split_heavy_pip_install_replay_commands(command: str) -> list[str]:
    """Split expensive optional ML deps out of replay installs when safe."""
    if not _is_bare_pip_install_command(command):
        return [command]

    parsed = _split_pip_install_command(command)
    if not parsed:
        return [command]
    prefix, options, requirements = parsed
    if "--no-deps" in shlex.split(command):
        return [command]

    if not requirements:
        return [command]

    if not any(option.startswith(("-i", "--index-url")) for option in options):
        torch_requirements = [
            requirement
            for requirement in requirements
            if _is_torch_cpu_split_candidate(requirement)
        ]
        if torch_requirements:
            remaining_requirements = [
                requirement
                for requirement in requirements
                if not _is_torch_cpu_split_candidate(requirement)
            ]
            rewritten = [
                shlex.join(
                    [
                        *prefix,
                        *options,
                        "--index-url",
                        PYTORCH_CPU_INDEX_URL,
                        torch_requirements[0],
                    ]
                )
            ]
            if remaining_requirements:
                rewritten.append(shlex.join([*prefix, *options, *remaining_requirements]))
            return rewritten

    heavy_requirements = [
        requirement
        for requirement in requirements
        if _pip_requirement_name(requirement) == "sentence-transformers"
    ]
    if not heavy_requirements:
        return [command]

    remaining_requirements = [
        requirement
        for requirement in requirements
        if _pip_requirement_name(requirement) != "sentence-transformers"
    ]

    rewritten: list[str] = []
    if remaining_requirements:
        rewritten.append(shlex.join([*prefix, *options, *remaining_requirements]))
    for requirement in heavy_requirements:
        rewritten.append(shlex.join([*prefix, *options, requirement, "--no-deps"]))
    return rewritten


def extract_observed_pip_install_constraints_from_text(text: str) -> dict[str, str]:
    constraints: dict[str, str] = {}
    for match in _SUCCESSFULLY_INSTALLED_BLOCK_RE.finditer(str(text or "")):
        package_text = " ".join(match.group("packages").split())
        for token in package_text.split():
            package_match = _INSTALLED_PACKAGE_TOKEN_RE.fullmatch(token.strip())
            if not package_match:
                continue
            name = _normalize_pip_constraint_name(package_match.group("name"))
            version = package_match.group("version")
            if name.replace("-", "").isdigit():
                continue
            if name and version:
                constraints[name] = version
    return constraints


def collect_observed_pip_install_constraints(
    workplace: Optional[Path],
    run_summary: Optional[dict[str, Any]],
) -> dict[str, str]:
    constraints: dict[str, str] = {}

    def ingest(text: Any) -> None:
        constraints.update(extract_observed_pip_install_constraints_from_text(str(text or "")))

    for action in (run_summary or {}).get("successful_actions") or []:
        if not isinstance(action, dict):
            continue
        ingest(action.get("observation"))
        ingest(action.get("observation_summary"))

    if workplace:
        setup_logs_dir = Path(workplace) / "logs" / "setup_logs"
        if setup_logs_dir.exists():
            for log_path in sorted(setup_logs_dir.glob("*.md")):
                try:
                    ingest(log_path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue

    return constraints


def normalize_eval_dockerfile_for_replay(
    dockerfile_text: str,
    pip_constraints: Optional[dict[str, str]] = None,
) -> str:
    """Apply deterministic replay hardening after synthesis or LLM repair."""
    rendered: list[str] = []
    lines = str(dockerfile_text or "").splitlines()
    workdir = infer_workdir_from_dockerfile(dockerfile_text)
    index = 0
    multiline_script_index = 1
    local_pip_installed_projects: set[str] = set()
    pip_installed_package_names: set[str] = set()
    pip_constraints = {
        _normalize_pip_constraint_name(name): str(version).strip()
        for name, version in (pip_constraints or {}).items()
        if _normalize_pip_constraint_name(name) and str(version).strip()
    }
    pip_constraints_rendered = False

    def ensure_pip_constraints_rendered() -> None:
        nonlocal pip_constraints_rendered
        if pip_constraints_rendered or not pip_constraints:
            return
        rendered.append(_render_observed_pip_constraints_instruction(pip_constraints))
        pip_constraints_rendered = True

    def remember_pip_installs(command: str) -> None:
        pip_installed_package_names.update(_pip_installed_requirement_names(command))

    installed_exact_torch_requirement: str | None = None
    exact_torch_replacement_requirement = _dockerfile_exact_torch_replacement_requirement(lines)
    torch_replacement_available = _dockerfile_contains_torch_replacement(lines)
    compatible_torchvision_requirement = (
        _compatible_torchvision_requirement(exact_torch_replacement_requirement)
        if _dockerfile_contains_mosaicml_stack(lines)
        else None
    )
    cuda_extension_builds_skipped = any("SKIP_CUDA_BUILD=TRUE" in line for line in lines)
    poetry_lock_available = _dockerfile_may_include_poetry_lock(dockerfile_text)
    while index < len(lines):
        generated_apt_retry = _collect_generated_apt_retry_with_orphan_continuations(lines, index)
        if generated_apt_retry:
            repaired_instruction, index = generated_apt_retry
            rendered.append(repaired_instruction)
            continue

        continued_instruction = _collect_continued_dockerfile_instruction(lines, index)
        if continued_instruction:
            line, index = continued_instruction
        else:
            multiline_run = _collect_raw_multiline_run(lines, index)
            if multiline_run:
                command, next_index = multiline_run
                rendered.append(_format_multiline_run_as_script(command, multiline_script_index))
                multiline_script_index += 1
                index = next_index
                continue

            line = lines[index]
            index += 1

        stripped = line.strip()
        run_export_path_match = re.match(r"^RUN\s+export\s+PATH=(.+)$", stripped)
        if run_export_path_match:
            exported_value = run_export_path_match.group(1).strip()
            if not re.search(r"\s(?:&&|\|\||;|\|)\s", exported_value):
                path_value = _normalize_dockerfile_path_value(exported_value)
                rendered.append(f'ENV PATH="{path_value}"')
                continue

        if stripped.startswith("RUN "):
            command = stripped[4:].strip()
            original_command = command
            command = _repair_generated_apt_retry_status_variables(command)
            if command != original_command:
                rendered.append(f"RUN {command}")
                continue
            command = _rewrite_absolute_tests_redirect_to_workdir(command, workdir)
            poetry_lock_rewritten_command = _drop_replay_poetry_lock_command(
                command,
                poetry_lock_available=poetry_lock_available,
            )
            if poetry_lock_rewritten_command is None:
                continue
            if poetry_lock_rewritten_command != command:
                rendered.append(f"RUN {poetry_lock_rewritten_command}")
                continue
            command = poetry_lock_rewritten_command
            if cuda_extension_builds_skipped and _is_cuda_local_installer_scaffolding_command(command):
                continue
            local_pip_installed_projects.update(_local_pip_install_project_names(command))
            generated_shell_command = _extract_generated_retry_inner_shell_command(command)
            if generated_shell_command:
                hardened_generated_shell_command = _harden_cuda_skipped_local_source_install(
                    generated_shell_command
                )
                if hardened_generated_shell_command != generated_shell_command:
                    rendered.append(build_resilient_pip_install_run_instruction(hardened_generated_shell_command))
                    continue
            hardened_source_install_command = _harden_cuda_skipped_local_source_install(command)
            if hardened_source_install_command != command:
                rendered.append(f"RUN {hardened_source_install_command}")
                continue
            generated_pip_command = _extract_generated_pip_retry_inner_command(command)
            if generated_pip_command:
                applied_generated_pip_rewrite = False
                generated_pip_command = _add_no_deps_to_known_force_reinstall(
                    generated_pip_command,
                    pip_installed_package_names,
                )
                if generated_pip_command != _extract_generated_pip_retry_inner_command(command):
                    applied_generated_pip_rewrite = True
                constrained_pip_command = _add_observed_constraints_to_pip_command(
                    generated_pip_command,
                    pip_constraints,
                )
                if constrained_pip_command != generated_pip_command:
                    ensure_pip_constraints_rendered()
                    generated_pip_command = constrained_pip_command
                    applied_generated_pip_rewrite = True
                filtered_pip_command = _drop_reinstalled_local_projects(
                    generated_pip_command,
                    local_pip_installed_projects,
                )
                if filtered_pip_command is None:
                    continue
                filtered_pip_command = _drop_redundant_broad_torch_bootstrap(
                    filtered_pip_command,
                    torch_replacement_available=torch_replacement_available,
                    exact_torch_replacement_requirement=exact_torch_replacement_requirement,
                    compatible_torchvision_requirement=compatible_torchvision_requirement,
                )
                if filtered_pip_command is None:
                    continue
                if _is_redundant_exact_torch_reinstall(
                    filtered_pip_command,
                    installed_exact_torch_requirement,
                ):
                    continue
                filtered_pip_command = _add_compatible_torchvision_constraint(
                    filtered_pip_command,
                    compatible_torchvision_requirement,
                )
                if filtered_pip_command != generated_pip_command:
                    for pip_command in split_heavy_pip_install_replay_commands(filtered_pip_command):
                        rendered.append(build_resilient_pip_install_run_instruction(pip_command))
                        exact_requirements = _exact_torch_requirements(pip_command)
                        if exact_requirements:
                            installed_exact_torch_requirement = exact_requirements[0]
                    continue
                if applied_generated_pip_rewrite:
                    for pip_command in split_heavy_pip_install_replay_commands(generated_pip_command):
                        rendered.append(build_resilient_pip_install_run_instruction(pip_command))
                        remember_pip_installs(pip_command)
                        exact_requirements = _exact_torch_requirements(pip_command)
                        if exact_requirements:
                            installed_exact_torch_requirement = exact_requirements[0]
                    continue
                split_commands = split_heavy_pip_install_replay_commands(generated_pip_command)
                if split_commands != [generated_pip_command]:
                    for pip_command in split_commands:
                        rendered.append(build_resilient_pip_install_run_instruction(pip_command))
                        remember_pip_installs(pip_command)
                        exact_requirements = _exact_torch_requirements(pip_command)
                        if exact_requirements:
                            installed_exact_torch_requirement = exact_requirements[0]
                    continue
                remember_pip_installs(generated_pip_command)
            if _is_generated_uv_pip_retry_command(command):
                rendered.append(build_resilient_uv_install_run_instruction())
                continue
            if _is_uv_shell_installer_command(command):
                rendered.append(build_resilient_uv_install_run_instruction())
                continue
            if _is_bare_uv_pip_install_command(command):
                rendered.append(build_resilient_uv_install_run_instruction(command))
                continue
            if _is_apt_install_replay_command(command):
                rendered.append(build_resilient_apt_install_run_instruction(command))
                continue
            if _is_bare_pip_install_command(command):
                command = _add_no_deps_to_known_force_reinstall(
                    command,
                    pip_installed_package_names,
                )
                command = _add_observed_constraints_to_pip_command(command, pip_constraints)
                if command != original_command:
                    ensure_pip_constraints_rendered()
                filtered_pip_command = _drop_reinstalled_local_projects(
                    command,
                    local_pip_installed_projects,
                )
                if filtered_pip_command is None:
                    continue
                filtered_pip_command = _drop_redundant_broad_torch_bootstrap(
                    filtered_pip_command,
                    torch_replacement_available=torch_replacement_available,
                    exact_torch_replacement_requirement=exact_torch_replacement_requirement,
                    compatible_torchvision_requirement=compatible_torchvision_requirement,
                )
                if filtered_pip_command is None:
                    continue
                if _is_redundant_exact_torch_reinstall(
                    filtered_pip_command,
                    installed_exact_torch_requirement,
                ):
                    continue
                filtered_pip_command = _add_compatible_torchvision_constraint(
                    filtered_pip_command,
                    compatible_torchvision_requirement,
                )
                for pip_command in split_heavy_pip_install_replay_commands(filtered_pip_command):
                    rendered.append(build_resilient_pip_install_run_instruction(pip_command))
                    remember_pip_installs(pip_command)
                    exact_requirements = _exact_torch_requirements(pip_command)
                    if exact_requirements:
                        installed_exact_torch_requirement = exact_requirements[0]
                continue
            if command != original_command:
                rendered.append(f"RUN {command}")
                continue

        rendered.append(line)

    return "\n".join(rendered).rstrip() + "\n"


def build_test_execution_script(workdir: str, runtime_commands: list[str], test_command: str) -> str:
    lines = [
        "set -e",
        f"cd {shlex.quote(workdir)}",
    ]
    lines.extend(runtime_commands)
    lines.extend(
        [
            f"cd {shlex.quote(workdir)}",
            "set +e",
            test_command,
            "TEST_EXIT_CODE=$?",
            "set -e",
            'printf "\\n__REPO2RUN_TEST_EXIT_CODE__=%s\\n" "$TEST_EXIT_CODE"',
            'exit "$TEST_EXIT_CODE"',
        ]
    )
    return "\n".join(lines) + "\n"


def discover_internal_import_prefixes(workspace_root: Path) -> set[str]:
    prefixes = {"src", "tests"}
    for candidate_root in (workspace_root, workspace_root / "src"):
        if not candidate_root.is_dir():
            continue
        for child in candidate_root.iterdir():
            if child.name.startswith(".") or not child.is_dir():
                continue
            if (child / "__init__.py").exists():
                prefixes.add(child.name)
    return prefixes


def output_has_collection_error_signal(observation: str) -> bool:
    normalized = str(observation or "")
    patterns = [
        r"ERROR collecting",
        r"ImportError while importing test module",
    ]
    return any(re.search(pattern, normalized, re.IGNORECASE | re.MULTILINE) for pattern in patterns)


def output_has_invocation_error_signal(observation: str) -> bool:
    normalized = str(observation or "")
    patterns = [
        r"found no collectors for",
        r"pytest: error:",
        r"unrecognized arguments:",
        r"usage: pytest",
    ]
    return any(re.search(pattern, normalized, re.IGNORECASE | re.MULTILINE) for pattern in patterns)


def output_has_internal_repo_import_error_signal(
    observation: str,
    internal_import_prefixes: Optional[set[str]] = None,
) -> bool:
    normalized = str(observation or "")
    prefixes = set(internal_import_prefixes or {"src", "tests"})

    missing_module_match = re.search(
        r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]",
        normalized,
        re.IGNORECASE | re.MULTILINE,
    )
    if missing_module_match:
        missing_module = missing_module_match.group(1)
        if missing_module.split(".", 1)[0] in prefixes:
            return True

    import_from_match = re.search(
        r"ImportError:\s+cannot import name .* from ['\"][^'\"]+['\"] \((/app/[^)]+)\)",
        normalized,
        re.IGNORECASE | re.MULTILINE,
    )
    return bool(import_from_match)


def classify_test_execution(
    command_result: dict[str, Any],
    internal_import_prefixes: Optional[set[str]] = None,
) -> dict[str, Any]:
    output = f"{command_result.get('stdout') or ''}\n{command_result.get('stderr') or ''}".strip()
    effective_signal = TEST_SIGNAL_DETECTOR.observation_has_effective_test_signal(output)
    empty_signal = TEST_SIGNAL_DETECTOR.observation_has_empty_test_run_signal(output)
    help_signal = TEST_SIGNAL_DETECTOR.observation_looks_like_help_text(output)
    failure_signal = TEST_SIGNAL_DETECTOR.observation_has_test_failure_signal(output)
    invocation_error_signal = output_has_invocation_error_signal(output)
    collection_error_signal = output_has_collection_error_signal(output)
    internal_repo_import_error_signal = output_has_internal_repo_import_error_signal(
        output,
        internal_import_prefixes=internal_import_prefixes,
    )

    effective = False
    reason = "tests_did_not_execute"

    if command_result.get("timed_out"):
        reason = "timed_out"
    elif command_result.get("returncode") == 0:
        effective = True
        reason = "tests_collected_successfully"
    elif command_result.get("returncode") == 5:
        effective = True
        reason = "no_tests_collected"
    elif help_signal:
        reason = "help_output"
    elif invocation_error_signal:
        reason = "invocation_error"
    elif collection_error_signal:
        reason = "collection_or_env_error"
    elif empty_signal:
        reason = "empty_test_run"
    elif effective_signal:
        reason = "effective_signal_without_supported_exit_pattern"

    return {
        "effective": effective,
        "reason": reason,
        "effective_signal": effective_signal,
        "failure_signal": failure_signal,
        "empty_signal": empty_signal,
        "help_signal": help_signal,
        "invocation_error_signal": invocation_error_signal,
        "collection_error_signal": collection_error_signal,
        "internal_repo_import_error_signal": internal_repo_import_error_signal,
    }


def extract_missing_python_modules_from_test_execution(
    test_execution: Optional[dict[str, Any]],
) -> list[str]:
    modules: list[str] = []
    seen: set[str] = set()
    for item in (test_execution or {}).get("results") or []:
        execution = item.get("execution") or {}
        combined_output = "\n".join(
            [
                _decode_command_stream(execution.get("stdout")),
                _decode_command_stream(execution.get("stderr")),
            ]
        )
        for match in _MISSING_PYTHON_MODULE_RE.finditer(combined_output):
            module = (match.group("module") or "").split(".", 1)[0].strip()
            if module and module not in seen:
                seen.add(module)
                modules.append(module)
    return modules


def _find_declared_requirement_in_workspace(
    workspace_root: Optional[Path],
    package_name: str,
) -> str | None:
    normalized_name = _normalize_pip_constraint_name(package_name)
    if not workspace_root or not normalized_name or not workspace_root.exists():
        return None

    inspected = 0
    for path in sorted(workspace_root.rglob("*requirements*.txt")):
        inspected += 1
        if inspected > 100:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            requirement = _strip_requirement_line(line)
            if _pip_requirement_name(requirement) == normalized_name:
                return requirement

    lock_inspected = 0
    package_pattern = re.compile(
        r"(?ms)^\[\[package\]\]\s*.*?^name\s*=\s*"
        + re.escape(json.dumps(package_name)[1:-1]).join(['"', '"'])
        + r"\s*$.*?^version\s*=\s*\"(?P<version>[^\"]+)\"",
    )
    for path in sorted(workspace_root.rglob("poetry.lock")):
        lock_inspected += 1
        if lock_inspected > 20:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        match = package_pattern.search(text)
        if match:
            return f"{package_name}=={match.group('version')}"
    return None


def _requirement_for_missing_module(
    module: str,
    workspace_root: Optional[Path],
) -> str | None:
    module_name = _normalize_pip_constraint_name((module or "").split(".", 1)[0])
    if not module_name:
        return None

    candidate_package_names: list[str] = []
    fallback_requirement = module_name
    if module_name in _KNOWN_MISSING_MODULE_PACKAGE_FALLBACKS:
        package_name, fallback_requirement = _KNOWN_MISSING_MODULE_PACKAGE_FALLBACKS[module_name]
        candidate_package_names.append(package_name)
    candidate_package_names.append(module_name)

    for package_name in candidate_package_names:
        declared = _find_declared_requirement_in_workspace(workspace_root, package_name)
        if declared:
            return declared
    return fallback_requirement


def _preferred_pip_invocation_for_dockerfile(dockerfile_text: str) -> str:
    text = dockerfile_text or ""
    if re.search(r"\bpython3\s+-m\s+pip\s+install\b", text):
        return "python3 -m pip"
    if re.search(r"\bpip3\s+install\b", text):
        return "pip3"
    return "pip"


def _dockerfile_already_installs_requirement(dockerfile_text: str, requirement: str) -> bool:
    requirement_name = _pip_requirement_name(requirement)
    if not requirement_name:
        return True
    requires_exact = bool(re.search(r"(?:===|==|~=|!=|>=|<=|>|<)", requirement))
    normalized_requirement = re.sub(r"\s+", "", requirement).lower().replace("_", "-")
    for line in (dockerfile_text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("RUN "):
            continue
        command = stripped[4:].strip()
        candidate_commands = [command]
        generated_pip_command = _extract_generated_pip_retry_inner_command(command)
        if generated_pip_command:
            candidate_commands.append(generated_pip_command)
        for candidate_command in candidate_commands:
            parsed = _split_pip_install_command(candidate_command)
            if not parsed:
                continue
            _, _, requirements = parsed
            for installed_requirement in requirements:
                if _pip_requirement_name(installed_requirement) != requirement_name:
                    continue
                if not requires_exact:
                    return True
                normalized_installed = (
                    re.sub(r"\s+", "", installed_requirement).lower().replace("_", "-")
                )
                if normalized_installed == normalized_requirement:
                    return True
    return False


def _insert_run_instruction_before_final_command(
    dockerfile_text: str,
    instruction: str,
) -> str:
    lines = (dockerfile_text or "").rstrip().splitlines()
    insert_at = len(lines)
    for index, line in enumerate(lines):
        if line.strip().upper().startswith(("CMD ", "ENTRYPOINT ")):
            insert_at = index
            break
    if insert_at > 0 and lines[insert_at - 1].strip():
        lines.insert(insert_at, "")
        insert_at += 1
    lines.insert(insert_at, instruction)
    return "\n".join(lines).rstrip() + "\n"


def repair_dockerfile_for_missing_python_modules(
    dockerfile_text: str,
    test_execution: Optional[dict[str, Any]],
    workspace_root: Optional[Path],
) -> tuple[str, list[str]]:
    modules = extract_missing_python_modules_from_test_execution(test_execution)
    if not modules:
        return dockerfile_text, []

    requirements: list[str] = []
    seen_requirement_names: set[str] = set()
    for module in modules:
        requirement = _requirement_for_missing_module(module, workspace_root)
        if not requirement or _dockerfile_already_installs_requirement(dockerfile_text, requirement):
            continue
        requirement_name = _pip_requirement_name(requirement)
        if requirement_name in seen_requirement_names:
            continue
        seen_requirement_names.add(requirement_name)
        requirements.append(requirement)

    if not requirements:
        return dockerfile_text, []

    pip_invocation = _preferred_pip_invocation_for_dockerfile(dockerfile_text)
    install_command = f"{pip_invocation} install " + " ".join(shlex.quote(item) for item in requirements)
    instruction = build_resilient_pip_install_run_instruction(install_command)
    return _insert_run_instruction_before_final_command(dockerfile_text, instruction), requirements


def should_add_postgres_host_alias(
    workspace_root: Optional[Path],
    runtime_commands: list[str],
    test_commands: list[str],
) -> bool:
    combined_commands = "\n".join([*(runtime_commands or []), *(test_commands or [])]).lower()
    if re.search(r"\b(?:pg_ctlcluster|postgres|psql)\b", combined_commands):
        return True

    if workspace_root is None:
        return False

    test_roots = [workspace_root / "tests", workspace_root / "test"]
    inspected = 0
    for test_root in test_roots:
        if not test_root.exists():
            continue
        for path in test_root.rglob("*.py"):
            inspected += 1
            if inspected > 200:
                return False
            try:
                text = path.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                continue
            if "postgres:5432" in text or "@postgres:" in text:
                return True
    return False


def evaluate_built_image(
    image_tag: str,
    workdir: str,
    runtime_commands: list[str],
    test_commands: list[str],
    cwd: Path,
    timeout_seconds: int,
    workspace_root: Optional[Path] = None,
    docker_platform: Optional[str] = None,
    junit_container_path: Optional[str] = None,
    attempt_index: int = 0,
) -> dict[str, Any]:
    """Run test_commands inside the built image and return result dict.

    When junit_container_path is provided, each container is started with a unique
    --name (to prevent --rm from destroying it before docker cp runs).  After the
    run, the JUnit XML is copied out and stored in execution["_junit_xml_data"],
    then the container is removed with docker rm -f.  This fixes the silent cp
    failure that occurred when --rm was used.
    """
    import tempfile

    command_results: list[dict[str, Any]] = []
    internal_import_prefixes = (
        discover_internal_import_prefixes(workspace_root) if workspace_root else None
    )
    add_postgres_host_alias = should_add_postgres_host_alias(
        workspace_root,
        runtime_commands,
        test_commands,
    )

    for cmd_index, test_command in enumerate(test_commands):
        script = build_test_execution_script(workdir, runtime_commands, test_command)

        # When we need to retrieve JUnit XML after the run, we cannot use --rm because
        # the container is destroyed before docker cp runs.  Use a unique --name instead,
        # copy the XML out, then remove the container explicitly.
        use_named_container = junit_container_path is not None
        if use_named_container:
            safe_tag = re.sub(r"[^a-z0-9-]", "-", image_tag.lower())
            container_name = f"{safe_tag}-a{attempt_index}-c{cmd_index}-{os.getpid()}"
            docker_run_command = ["docker", "run", "--name", container_name, "-i"]
        else:
            container_name = None
            docker_run_command = ["docker", "run", "--rm", "-i"]

        if docker_platform:
            docker_run_command.extend(["--platform", docker_platform])
        if add_postgres_host_alias:
            docker_run_command.extend(["--add-host", "postgres:127.0.0.1"])
        docker_run_command.extend(
            [
                image_tag,
                "sh",
                "-lc",
                TEST_EXECUTION_SHELL_WRAPPER,
            ]
        )
        execution = run_command(
            docker_run_command,
            cwd=cwd,
            input_text=script,
            timeout_seconds=timeout_seconds,
        )

        # If we used a named container, attempt docker cp for JUnit XML, then remove.
        if use_named_container and container_name:
            junit_xml_data: Optional[str] = None
            tmp_path_xml = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
                    tmp_path_xml = tmp.name
                cp_result = subprocess.run(
                    ["docker", "cp", f"{container_name}:{junit_container_path}", tmp_path_xml],
                    capture_output=True,
                    timeout=30,
                )
                if cp_result.returncode == 0:
                    junit_xml_data = Path(tmp_path_xml).read_text(encoding="utf-8", errors="replace")
            except Exception:
                junit_xml_data = None
            finally:
                if tmp_path_xml:
                    try:
                        Path(tmp_path_xml).unlink(missing_ok=True)
                    except Exception:
                        pass
                # Always remove the named container
                try:
                    subprocess.run(
                        ["docker", "rm", "-f", container_name],
                        capture_output=True,
                        timeout=30,
                    )
                except Exception:
                    pass
            if junit_xml_data is not None:
                execution["_junit_xml_data"] = junit_xml_data

        classification = classify_test_execution(
            execution,
            internal_import_prefixes=internal_import_prefixes,
        )
        command_results.append(
            {
                "test_command": test_command,
                "runtime_preparation_commands": runtime_commands,
                "script": script,
                "execution": execution,
                "classification": classification,
            }
        )

    effective_count = sum(
        1 for item in command_results if item["classification"]["effective"]
    )
    all_effective = bool(test_commands) and effective_count == len(test_commands)
    return {
        "workdir": workdir,
        "runtime_preparation_commands": runtime_commands,
        "test_commands": test_commands,
        "results": command_results,
        "effective_test_command_count": effective_count,
        "all_test_commands_effective": all_effective,
    }


def truncate_for_repair_prompt(value: Any, limit: int = DOCKERFILE_REPAIR_LOG_LIMIT) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    head_limit = limit // 2
    tail_limit = limit - head_limit
    return (
        text[:head_limit]
        + "\n\n...[truncated for Dockerfile repair prompt]...\n\n"
        + text[-tail_limit:]
    )


def extract_json_object_candidates(text: str) -> list[str]:
    objects: list[str] = []
    if not text:
        return objects

    search_regions = [
        match.group(1).strip()
        for match in re.finditer(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    ]
    search_regions.append(text.strip())

    for region in search_regions:
        position = 0
        while position < len(region):
            start = region.find("{", position)
            if start == -1:
                break
            depth = 0
            in_string = False
            escape = False
            found = False
            for index in range(start, len(region)):
                char = region[index]
                if in_string:
                    if escape:
                        escape = False
                    elif char == "\\":
                        escape = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        objects.append(region[start:index + 1])
                        position = index + 1
                        found = True
                        break
            if not found:
                break
    return objects


def extract_dockerfile_repair_json(content: str) -> dict[str, Any]:
    for candidate in extract_json_object_candidates(content):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        dockerfile = str(parsed.get("dockerfile") or "").strip()
        if dockerfile and re.search(r"(?im)^FROM\s+\S+", dockerfile):
            confidence = str(parsed.get("confidence") or "medium").strip().lower()
            if confidence not in {"high", "medium", "low"}:
                confidence = "medium"
            return {
                "dockerfile": dockerfile.rstrip() + "\n",
                "rationale": str(parsed.get("rationale") or "").strip(),
                "confidence": confidence,
            }
    raise ValueError("Dockerfile repair response did not contain a valid JSON object with a full Dockerfile")


def build_dockerfile_repair_input(
    *,
    instance: dict[str, Any],
    workdir: str,
    dockerfile_text: str,
    run_summary: Optional[dict[str, Any]],
    runtime_commands: list[str],
    test_commands: list[str],
    docker_build: Optional[dict[str, Any]],
    test_execution: Optional[dict[str, Any]],
) -> dict[str, Any]:
    test_results = []
    for item in (test_execution or {}).get("results") or []:
        execution = item.get("execution") or {}
        test_results.append(
            {
                "test_command": item.get("test_command"),
                "classification": item.get("classification"),
                "returncode": execution.get("returncode"),
                "timed_out": execution.get("timed_out"),
                "stdout": truncate_for_repair_prompt(execution.get("stdout")),
                "stderr": truncate_for_repair_prompt(execution.get("stderr")),
            }
        )

    minimal_run_summary = {
        "repo_url": (run_summary or {}).get("repo_url"),
        "base_commit": (run_summary or {}).get("base_commit"),
        "language": (run_summary or {}).get("language"),
        "verification_bundle": (run_summary or {}).get("verification_bundle"),
        "verified_runtime_preparation_commands": (run_summary or {}).get(
            "verified_runtime_preparation_commands"
        ),
        "verified_test_commands": (run_summary or {}).get("verified_test_commands"),
        "build_recipe": {
            "source": ((run_summary or {}).get("build_recipe") or {}).get("source"),
            "build_commands": ((run_summary or {}).get("build_recipe") or {}).get(
                "build_commands"
            )
            or [],
            "runtime_commands": ((run_summary or {}).get("build_recipe") or {}).get(
                "runtime_commands"
            )
            or [],
        },
        "successful_actions": (run_summary or {}).get("successful_actions") or [],
        "failed_actions": (run_summary or {}).get("failed_actions") or [],
    }

    return {
        "task": {
            "instance_id": instance.get("instance_id"),
            "full_name": instance.get("full_name"),
            "sha": instance.get("sha"),
            "repo_url": instance.get("repo_url"),
            "workdir": workdir,
        },
        "dockerfile": dockerfile_text,
        "runtime_preparation_commands": runtime_commands,
        "test_commands": test_commands,
        "agent_run_summary": minimal_run_summary,
        "docker_build": {
            "returncode": (docker_build or {}).get("returncode"),
            "timed_out": (docker_build or {}).get("timed_out"),
            "stdout": truncate_for_repair_prompt((docker_build or {}).get("stdout")),
            "stderr": truncate_for_repair_prompt((docker_build or {}).get("stderr")),
        },
        "test_execution": test_results,
    }


def repair_dockerfile_with_llm(
    *,
    client: Any,
    model: str,
    repair_input: dict[str, Any],
    artifact_dir: Path,
    round_index: int,
) -> dict[str, Any]:
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    raw_content = ""
    messages = [
        {"role": "system", "content": DOCKERFILE_REPAIR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": DOCKERFILE_REPAIR_USER_PROMPT.format(
                repair_input_json=json.dumps(repair_input, ensure_ascii=False, indent=2)
            ),
        },
    ]
    repair_log_path = artifact_dir / f"dockerfile_repair_round_{round_index}.md"
    write_text(
        repair_log_path,
        "##### LLM INPUT (Dockerfile repair) #####\n"
        "================================ Human Message =================================\n\n"
        + "\n\n".join(f"[{message['role'].upper()}]\n{message['content']}" for message in messages)
        + "\n\n",
    )
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
        )
        usage = {
            "input_tokens": getattr(response.usage, "prompt_tokens", 0),
            "output_tokens": getattr(response.usage, "completion_tokens", 0),
            "total_tokens": getattr(response.usage, "total_tokens", 0),
        }
        raw_content = response.choices[0].message.content or ""
        parsed = extract_dockerfile_repair_json(raw_content)
        result = {
            "round": round_index,
            "source": "llm",
            "error": None,
            "usage": usage,
            "raw_content": raw_content,
            "dockerfile_text": parsed["dockerfile"],
            "rationale": parsed["rationale"],
            "confidence": parsed["confidence"],
            "log_path": str(repair_log_path),
        }
    except Exception as exc:
        result = {
            "round": round_index,
            "source": "llm_error",
            "error": str(exc),
            "usage": usage,
            "raw_content": raw_content,
            "dockerfile_text": None,
            "rationale": "",
            "confidence": "low",
            "log_path": str(repair_log_path),
        }

    with repair_log_path.open("a", encoding="utf-8") as file_obj:
        file_obj.write("================================ AI Message =================================\n\n")
        file_obj.write(f"{raw_content}\n\n")
        file_obj.write("================================ Parsed Repair =================================\n\n")
        file_obj.write(json.dumps({k: v for k, v in result.items() if k != "raw_content"}, ensure_ascii=False, indent=2))
        file_obj.write("\n")
    return result


def docker_build_failed_due_to_unavailable_daemon(docker_build: Optional[dict[str, Any]]) -> bool:
    if not docker_build or docker_build.get("returncode") == 0:
        return False
    combined_output = "\n".join(
        [
            _decode_command_stream(docker_build.get("stdout")),
            _decode_command_stream(docker_build.get("stderr")),
        ]
    ).lower()
    return (
        "cannot connect to the docker daemon" in combined_output
        or "is the docker daemon running" in combined_output
    )


def run_command(
    command: list[str],
    cwd: Path,
    env: Optional[dict[str, str]] = None,
    input_text: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
) -> dict[str, Any]:
    started_at = datetime.now().astimezone()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        returncode = DOCKER_TIMEOUT_EXIT_CODE
        stdout = _decode_command_stream(exc.stdout)
        stderr = _decode_command_stream(exc.stderr)
        timed_out = True

    finished_at = datetime.now().astimezone()
    return {
        "command": command,
        "command_shell": shlex.join(command),
        "cwd": str(cwd),
        "returncode": returncode,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "timeout_seconds": timeout_seconds,
    }


# ---------------------------------------------------------------------------
# GLUE-COPY: create_openai_client_from_env
# Copied verbatim from src/workplace_replay.py:71-79
# ---------------------------------------------------------------------------


def create_openai_client_from_env() -> OpenAI:
    api_key = (os.getenv("OPENROUTER_API_KEY")
               or os.getenv("MINIMAX_API_KEY") or os.getenv("OPENAI_API_KEY"))
    base_url = (os.getenv("OPENROUTER_API_BASE")
                or os.getenv("MINIMAX_API_BASE") or os.getenv("OPENAI_API_BASE"))
    if not api_key:
        raise ValueError("No LLM API key found. Set OPENROUTER_API_KEY, MINIMAX_API_KEY, "
                         "or OPENAI_API_KEY in environment variables (.env).")
    return OpenAI(api_key=api_key, base_url=base_url if base_url else None)


# ---------------------------------------------------------------------------
# Verbatim collect-command helpers (run_repo2run_benchmark.py:2112-2418)
# ---------------------------------------------------------------------------


def workspace_uses_poetry(workspace_root: Path) -> bool:
    if (workspace_root / "poetry.lock").exists():
        return True
    pyproject_path = workspace_root / "pyproject.toml"
    if not pyproject_path.exists():
        return False
    try:
        pyproject_text = pyproject_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "[tool.poetry]" in pyproject_text


def _normalize_python_module_pytest_prefix(command: str) -> str:
    normalized = " ".join(str(command or "").split())
    for python_prefix in ("python -m ", "python3 -m "):
        if normalized.startswith(f"{python_prefix}pytest "):
            return "pytest " + normalized[len(f"{python_prefix}pytest ") :]
    return normalized


def _normalize_collect_cd_workdir(workdir: str) -> Optional[str]:
    normalized = str(workdir or "").strip().rstrip("/")
    if normalized in {"", ".", "/app", "/app/."}:
        return ""
    if normalized.startswith("/app/"):
        normalized = normalized[len("/app/") :]
    elif normalized.startswith(("/", "~")):
        return None

    if not re.match(r"^[A-Za-z0-9_./-]+$", normalized):
        return None
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _split_safe_leading_cd_collect_command(command: str) -> Optional[tuple[Optional[str], str]]:
    normalized = " ".join(str(command or "").split())
    try:
        tokens = shlex.split(normalized)
    except ValueError:
        return None
    if len(tokens) < 4 or tokens[0] != "cd" or tokens[2] != "&&":
        return None

    cd_workdir = _normalize_collect_cd_workdir(tokens[1])
    if cd_workdir is None:
        return None
    inner_command = " ".join(shlex.quote(token) for token in tokens[3:]).strip()
    if not inner_command:
        return None
    return cd_workdir or None, inner_command


def _repo2run_collect_command_has_unsafe_shell_syntax(command: str) -> bool:
    return any(fragment in command for fragment in UNSAFE_COLLECT_COMMAND_SUBSTRINGS)


def _repo2run_token_has_shell_control(token: str) -> bool:
    return any(character in token for character in (";", "&", "|", ">", "<", "`"))


def _is_pytest_executable_token(token: str) -> bool:
    return token == "pytest" or token.endswith("/pytest") or token.endswith("\\pytest")


def _is_env_assignment_token(token: str) -> bool:
    if _repo2run_token_has_shell_control(token):
        return False
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.+$", token))


def _strip_leading_env_assignment_tokens(command_tokens: list[str]) -> list[str]:
    index = 0
    while index < len(command_tokens) and _is_env_assignment_token(command_tokens[index]):
        index += 1
    return command_tokens[index:]


def _strip_leading_env_command_tokens(command_tokens: list[str]) -> Optional[list[str]]:
    command_tokens = _strip_leading_env_assignment_tokens(command_tokens)
    if not command_tokens or command_tokens[0] != "env":
        return command_tokens

    index = 1
    while index < len(command_tokens) and _is_env_assignment_token(command_tokens[index]):
        index += 1
    if index >= len(command_tokens):
        return None
    if command_tokens[index].startswith("-"):
        return None
    return command_tokens[index:]


def _repo2run_collect_source_from_tokens(command_tokens: list[str]) -> Optional[str]:
    command_tokens = _strip_leading_env_command_tokens(command_tokens)
    if not command_tokens:
        return None
    runner_end_index = 0
    source: Optional[str] = None

    if command_tokens[:3] == ["poetry", "run", "pytest"]:
        runner_end_index = 3
        source = "repo2run_poetry_collect_only_agent_verified"
    elif command_tokens[:3] == ["uv", "run", "pytest"]:
        runner_end_index = 3
        source = "repo2run_uv_collect_only_agent_verified"
    elif command_tokens[:3] == ["pdm", "run", "pytest"]:
        runner_end_index = 3
        source = "repo2run_pdm_collect_only_agent_verified"
    elif command_tokens[:3] in (["python", "-m", "pytest"], ["python3", "-m", "pytest"]):
        runner_end_index = 3
        source = "repo2run_pytest_collect_only_agent_verified"
    elif command_tokens and _is_pytest_executable_token(command_tokens[0]):
        runner_end_index = 1
        source = "repo2run_pytest_collect_only_agent_verified"

    if not source:
        return None

    collect_args = command_tokens[runner_end_index:]
    if not any(token == "--collect-only" or token.startswith("--collect-only=") for token in collect_args):
        return None
    for token in collect_args:
        if token in DISALLOWED_COLLECT_TOKENS:
            return None
        if _repo2run_token_has_shell_control(token):
            return None
    return source


def _split_xvfb_run_collect_command(command: str) -> Optional[tuple[list[str], list[str]]]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None

    if not tokens or tokens[0] != "xvfb-run":
        return None

    value_options = {
        "-e",
        "-f",
        "-n",
        "-s",
        "--error-file",
        "--auth-file",
        "--server-num",
        "--server-args",
    }
    flag_options = {
        "-a",
        "-l",
        "--auto-servernum",
        "--listen-tcp",
    }
    value_option_prefixes = tuple(f"{option}=" for option in value_options if option.startswith("--"))

    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in flag_options:
            index += 1
            continue
        if token in value_options:
            index += 2
            continue
        if token.startswith(value_option_prefixes):
            index += 1
            continue
        if token.startswith("-"):
            return None
        break

    if index >= len(tokens):
        return None
    return tokens[:index], tokens[index:]


def _repo2run_wrapped_collect_source_for_command(command: str) -> Optional[str]:
    xvfb_tokens = _split_xvfb_run_collect_command(command)
    if not xvfb_tokens:
        return None

    _, inner_tokens = xvfb_tokens
    if _repo2run_collect_source_from_tokens(inner_tokens):
        return "repo2run_xvfb_collect_only_agent_verified"
    return None


def repo2run_collect_source_for_command(command: str) -> Optional[str]:
    normalized = normalize_repo2run_collect_candidate(command)
    cd_split = _split_safe_leading_cd_collect_command(normalized)
    if cd_split:
        _, normalized = cd_split

    if _repo2run_collect_command_has_unsafe_shell_syntax(normalized):
        return None

    wrapped_source = _repo2run_wrapped_collect_source_for_command(normalized)
    if wrapped_source:
        return wrapped_source

    try:
        command_tokens = shlex.split(normalized)
    except ValueError:
        return None
    return _repo2run_collect_source_from_tokens(command_tokens)


def normalize_repo2run_collect_candidate(command: str) -> str:
    normalized = " ".join(str(command or "").split())
    cd_split = _split_safe_leading_cd_collect_command(normalized)
    cd_workdir: Optional[str] = None
    if cd_split:
        cd_workdir, normalized = cd_split
    normalized = _normalize_python_module_pytest_prefix(normalized)

    xvfb_tokens = _split_xvfb_run_collect_command(normalized)
    if xvfb_tokens:
        wrapper_tokens, inner_tokens = xvfb_tokens
        inner_command = _normalize_python_module_pytest_prefix(
            " ".join(shlex.quote(token) for token in inner_tokens)
        )
        normalized = " ".join(
            [*(shlex.quote(token) for token in wrapper_tokens), inner_command]
        )
    if cd_workdir:
        normalized = f"cd {shlex.quote(cd_workdir)} && {normalized}"
    return normalized


def select_repo2run_collect_commands_from_run_summary(
    run_summary: Optional[dict[str, Any]],
) -> Optional[tuple[list[str], str]]:
    supported_bundle = derive_supported_verification_bundle(run_summary)
    candidate_commands = normalize_command_list(supported_bundle.get("test_commands"))
    if not candidate_commands:
        return None

    selected_commands: list[str] = []
    sources: list[str] = []
    for command in candidate_commands:
        normalized = normalize_repo2run_collect_candidate(command)
        source = repo2run_collect_source_for_command(normalized)
        if not source:
            return None
        selected_commands.append(normalized)
        sources.append(source)

    source = sources[0] if len(set(sources)) == 1 else "repo2run_agent_verified_collect_commands"
    return selected_commands, source


def filter_runtime_preparation_commands(commands: list[str]) -> list[str]:
    filtered: list[str] = []
    for command in commands or []:
        normalized = normalize_repo2run_collect_candidate(command)
        if repo2run_collect_source_for_command(normalized):
            continue
        filtered.append(command)
    return filtered


def derive_repo2run_collect_commands(
    workspace_root: Path,
    run_summary: Optional[dict[str, Any]] = None,
) -> tuple[list[str], list[str], str]:
    supported_bundle = derive_supported_verification_bundle(run_summary)
    runtime_commands = filter_runtime_preparation_commands(
        normalize_command_list(supported_bundle.get("runtime_preparation_commands"))
    )
    agent_verified_choice = select_repo2run_collect_commands_from_run_summary(run_summary)
    if agent_verified_choice is not None:
        commands, source = agent_verified_choice
        return runtime_commands, commands, source

    if workspace_uses_poetry(workspace_root):
        return [], [REPO2RUN_POETRY_COLLECT_COMMAND], "repo2run_poetry_collect_only"
    return [], [REPO2RUN_PYTEST_COLLECT_COMMAND], "repo2run_pytest_collect_only"


# ---------------------------------------------------------------------------
# Verbatim dockerignore helpers (run_repo2run_benchmark.py:359-519)
# ---------------------------------------------------------------------------


def _infer_existing_eval_test_artifact_paths(
    build_context: Path,
    test_commands: Optional[list[str]] = None,
    run_summary: Optional[dict[str, Any]] = None,
) -> list[str]:
    """Infer test artifacts that must survive .dockerignore for fresh eval."""
    candidates: list[str] = ["tests", "test", "testing"]

    for command in test_commands or []:
        normalized = normalize_repo2run_collect_candidate(command)
        try:
            tokens = shlex.split(normalized)
        except ValueError:
            continue
        for token in tokens:
            if not token or token.startswith("-"):
                continue
            if token in {"pytest", "poetry", "run", "uv", "pdm", "xvfb-run"}:
                continue
            if token.endswith("/pytest") or token.endswith("\\pytest"):
                continue
            if token in {"python", "python3", "-m"}:
                continue
            path_token = token.strip().lstrip("./").rstrip("/")
            if path_token and (
                "/" in path_token
                or path_token.startswith(("test", "tests", "testing"))
                or path_token.endswith((".py", ".rst"))
            ):
                candidates.append(path_token.split("/", 1)[0])

    for action in (run_summary or {}).get("successful_actions") or []:
        command = normalize_repo2run_collect_candidate(str(action.get("command") or ""))
        if not repo2run_collect_source_for_command(command):
            continue
        observation = str(
            action.get("observation_summary") or action.get("observation") or ""
        )
        if not re.search(r"\b(?:tests?|items?) collected\b", observation):
            continue
        for line in observation.splitlines():
            stripped = line.strip()
            if "::" not in stripped:
                continue
            node_path = stripped.split("::", 1)[0].lstrip("./")
            if "/" in node_path:
                candidates.append(node_path.split("/", 1)[0])
            elif node_path:
                candidates.append(node_path)

    seen: set[str] = set()
    existing: list[str] = []
    for candidate in candidates:
        cleaned = candidate.strip().strip("/").replace("\\", "/")
        if not cleaned or cleaned in seen:
            continue
        if (build_context / cleaned).exists():
            seen.add(cleaned)
            existing.append(cleaned)
    return existing


def _dockerignore_pattern_mentions_artifact(pattern: str, artifact_path: str) -> bool:
    stripped = pattern.strip().replace("\\", "/")
    if not stripped or stripped.startswith("#") or stripped.startswith("!"):
        return False

    normalized_pattern = stripped.lstrip("/").rstrip("/")
    artifact = artifact_path.strip("/").replace("\\", "/")
    if not normalized_pattern or not artifact:
        return False

    artifact_root = artifact.split("/", 1)[0]
    if normalized_pattern in {"*", "**", "**/*"}:
        return False
    if normalized_pattern == artifact or normalized_pattern == artifact_root:
        return True
    if normalized_pattern.startswith(f"{artifact}/") or normalized_pattern.startswith(
        f"{artifact_root}/"
    ):
        return True
    if normalized_pattern.endswith(f"/{artifact}") or normalized_pattern.endswith(
        f"/{artifact_root}"
    ):
        return True
    if f"/{artifact}/" in normalized_pattern or f"/{artifact_root}/" in normalized_pattern:
        return True
    return bool(
        fnmatch.fnmatch(artifact, normalized_pattern)
        or fnmatch.fnmatch(f"{artifact}/__jayint_keep__", normalized_pattern)
        or fnmatch.fnmatch(artifact_root, normalized_pattern)
        or fnmatch.fnmatch(f"{artifact_root}/__jayint_keep__", normalized_pattern)
    )


def ensure_eval_dockerignore_includes_test_artifacts(
    build_context: Path,
    *,
    test_commands: Optional[list[str]] = None,
    run_summary: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Prevent target .dockerignore files from hiding tests during eval replay."""
    dockerignore_path = build_context / ".dockerignore"
    artifact_paths = _infer_existing_eval_test_artifact_paths(
        build_context,
        test_commands=test_commands,
        run_summary=run_summary,
    )
    result: dict[str, Any] = {
        "path": str(dockerignore_path),
        "test_artifact_paths": artifact_paths,
        "changed": False,
        "removed_patterns": [],
        "appended_exceptions": [],
    }
    if not dockerignore_path.exists():
        result["reason"] = "no_dockerignore"
        return result
    if not artifact_paths:
        result["reason"] = "no_existing_test_artifacts_detected"
        return result

    original_lines = dockerignore_path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines()
    kept_lines: list[str] = []
    removed_patterns: list[str] = []
    for line in original_lines:
        if any(_dockerignore_pattern_mentions_artifact(line, path) for path in artifact_paths):
            removed_patterns.append(line)
            continue
        kept_lines.append(line)

    exceptions: list[str] = []
    for path in artifact_paths:
        if (build_context / path).is_dir():
            exceptions.extend([f"!{path}/", f"!{path}/**"])
        else:
            exceptions.append(f"!{path}")

    existing_lines = {line.strip() for line in kept_lines}
    appended_exceptions = [line for line in exceptions if line not in existing_lines]
    changed = bool(removed_patterns or appended_exceptions)
    if changed:
        rendered_lines = kept_lines[:]
        if appended_exceptions:
            if rendered_lines and rendered_lines[-1].strip():
                rendered_lines.append("")
            rendered_lines.append("# Repo2Run eval: keep test artifacts available inside the image.")
            rendered_lines.extend(appended_exceptions)
        dockerignore_path.write_text("\n".join(rendered_lines).rstrip() + "\n", encoding="utf-8")

    result.update(
        {
            "changed": changed,
            "removed_patterns": removed_patterns,
            "appended_exceptions": appended_exceptions,
            "reason": "updated" if changed else "already_includes_test_artifacts",
        }
    )
    return result


def derive_verification_commands(run_summary: Optional[dict[str, Any]]) -> tuple[list[str], list[str], str]:
    supported_bundle = derive_supported_verification_bundle(run_summary)

    runtime_commands = normalize_command_list(supported_bundle.get("runtime_preparation_commands"))
    test_commands = normalize_command_list(supported_bundle.get("test_commands"))
    source = "supported_verification_bundle"
    if not test_commands:
        test_commands = ["pytest"]
        source = "default_pytest"

    return runtime_commands, test_commands, source


# ---------------------------------------------------------------------------
# Stage B — Glue: error categorization (mirrors run_pytest.py:110-150)
# ---------------------------------------------------------------------------

# 21-bucket vocabulary in first-match order (run_pytest.py:110-150).
# AssertionError has no trailing colon; all others match "ClassName:".
_ERROR_BUCKET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ModuleNotFoundError:"), "ModuleNotFoundError"),
    (re.compile(r"ImportError:"), "ImportError"),
    (re.compile(r"AttributeError:"), "AttributeError"),
    (re.compile(r"AssertionError"), "AssertionError"),        # no colon
    (re.compile(r"TypeError:"), "TypeError"),
    (re.compile(r"ValueError:"), "ValueError"),
    (re.compile(r"KeyError:"), "KeyError"),
    (re.compile(r"IndexError:"), "IndexError"),
    (re.compile(r"NameError:"), "NameError"),
    (re.compile(r"FileNotFoundError:"), "FileNotFoundError"),
    (re.compile(r"RuntimeError:"), "RuntimeError"),
    (re.compile(r"OSError:"), "OSError"),
    (re.compile(r"IOError:"), "IOError"),
    (re.compile(r"ZeroDivisionError:"), "ZeroDivisionError"),
    (re.compile(r"SyntaxError:"), "SyntaxError"),
    (re.compile(r"IndentationError:"), "IndentationError"),
    (re.compile(r"MemoryError:"), "MemoryError"),
    (re.compile(r"RecursionError:"), "RecursionError"),
    (re.compile(r"TimeoutError:"), "TimeoutError"),
    (re.compile(r"ConnectionError:"), "ConnectionError"),
    (re.compile(r"PermissionError:"), "PermissionError"),
]


def _categorize_error(text: str) -> str:
    """First-match regex scan on combined failure text → bucket name.

    Mirrors run_pytest.py:110-150 exactly. Returns 'OtherError' if no match.
    """
    for pattern, bucket in _ERROR_BUCKET_PATTERNS:
        if pattern.search(text):
            return bucket
    return "OtherError"


def _parse_junit_xml(xml_text: str) -> dict[str, Any]:
    """Parse a JUnit XML string into the run_pytest_results.json schema structure.

    Returns a partial dict with summary, error_breakdown, failed_tests, error_tests.
    """
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_text)
    # Handle both <testsuite> at root and <testsuites><testsuite>
    suites = root.findall(".//testsuite") if root.tag != "testsuite" else [root]
    if not suites:
        suites = [root]

    total_tests = 0
    passed = 0
    failed = 0
    errors = 0
    skipped = 0
    xfailed = 0
    xpassed = 0

    error_breakdown: dict[str, int] = {}
    failed_tests: list[dict[str, Any]] = []
    error_tests: list[dict[str, Any]] = []

    for suite in suites:
        for tc in suite.findall("testcase"):
            classname = tc.get("classname", "")
            name = tc.get("name", "")
            test_id = f"{classname}::{name}" if classname else name

            failure_el = tc.find("failure")
            error_el = tc.find("error")
            skipped_el = tc.find("skipped")

            if skipped_el is not None:
                skipped += 1
                total_tests += 1
            elif failure_el is not None:
                total_tests += 1
                failed += 1
                msg = (failure_el.get("message") or "") + "\n" + (failure_el.text or "")
                bucket = _categorize_error(msg)
                error_breakdown[bucket] = error_breakdown.get(bucket, 0) + 1
                error_message = msg.strip()[:200]
                failed_tests.append(
                    {
                        "test_id": test_id,
                        "error_type": bucket,
                        "error_message": error_message,
                    }
                )
            elif error_el is not None:
                total_tests += 1
                errors += 1
                msg = (error_el.get("message") or "") + "\n" + (error_el.text or "")
                bucket = _categorize_error(msg)
                error_breakdown[bucket] = error_breakdown.get(bucket, 0) + 1
                error_message = msg.strip()[:200]
                error_tests.append(
                    {
                        "test_id": test_id,
                        "error_type": bucket,
                        "error_message": error_message,
                    }
                )
            else:
                total_tests += 1
                passed += 1

    return {
        "summary": {
            "total_tests": total_tests,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
            "xfailed": xfailed,
            "xpassed": xpassed,
        },
        "error_breakdown": error_breakdown,
        "failed_tests": failed_tests,
        "error_tests": error_tests,
    }


def _parse_pytest_stdout_regex(stdout: str, stderr: str) -> dict[str, Any]:
    """Regex fallback parser over pytest stdout/stderr.

    Extracts counts from pytest's summary line:
        "N passed, M failed, K error(s), J skipped in X.Xs"

    Returns the same partial dict structure as _parse_junit_xml.
    """
    combined = (stdout or "") + "\n" + (stderr or "")

    passed = 0
    failed = 0
    errors = 0
    skipped = 0

    # Match typical pytest summary: "1 passed, 2 failed, 3 error in 0.5s"
    # Various orderings are possible; parse each count independently
    m_passed = re.search(r"(\d+)\s+passed", combined)
    m_failed = re.search(r"(\d+)\s+failed", combined)
    m_errors = re.search(r"(\d+)\s+error", combined)
    m_skipped = re.search(r"(\d+)\s+skipped", combined)

    if m_passed:
        passed = int(m_passed.group(1))
    if m_failed:
        failed = int(m_failed.group(1))
    if m_errors:
        errors = int(m_errors.group(1))
    if m_skipped:
        skipped = int(m_skipped.group(1))

    total_tests = passed + failed + errors + skipped

    # Build a minimal error_breakdown from FAILED lines in stdout
    error_breakdown: dict[str, int] = {}
    failed_tests: list[dict[str, Any]] = []
    error_tests: list[dict[str, Any]] = []

    # Try to extract error types from FAILED lines like:
    # "FAILED tests/foo.py::test_bar - SomeError: msg"
    for line in combined.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED ") or stripped.startswith("ERROR "):
            is_error = stripped.startswith("ERROR ")
            parts = stripped.split(" - ", 1)
            test_id = parts[0].replace("FAILED ", "").replace("ERROR ", "").strip()
            failure_text = parts[1] if len(parts) > 1 else stripped
            bucket = _categorize_error(failure_text)
            error_breakdown[bucket] = error_breakdown.get(bucket, 0) + 1
            entry = {
                "test_id": test_id,
                "error_type": bucket,
                "error_message": failure_text[:200],
            }
            if is_error:
                error_tests.append(entry)
            else:
                failed_tests.append(entry)

    return {
        "summary": {
            "total_tests": total_tests,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
            "xfailed": 0,
            "xpassed": 0,
        },
        "error_breakdown": error_breakdown,
        "failed_tests": failed_tests,
        "error_tests": error_tests,
    }


def junit_to_pytest_results(
    execution: dict[str, Any],
    image_tag: str,
    junit_container_path: str = "/tmp/repair_junit.xml",
) -> dict[str, Any]:
    """Convert a live pytest run (inside the repaired Docker container) into the exact JSON
    schema that scorers.py reads.

    SPEC: IMPLEMENTATION_SPEC.md §B.1

    Acquisition strategy:
    1. If execution["timed_out"] is True → emit TimeoutError directly (run_pytest.py:517).
    2. If execution["_junit_xml_override"] is set (for testing) → use that XML directly.
    3. Otherwise, try docker cp <image_tag>:<junit_container_path> to retrieve XML.
    4. If XML unavailable or unparseable → regex fallback over stdout/stderr.
    """
    raw_output = (execution.get("stdout") or "") + "\n" + (execution.get("stderr") or "")
    returncode = execution.get("returncode", 1)

    # Fast path: timeout
    if execution.get("timed_out"):
        return {
            "summary": {
                "total_tests": 0,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "xfailed": 0,
                "xpassed": 0,
            },
            "error_breakdown": {"TimeoutError": 1},
            "failed_tests": [],
            "error_tests": [],
            "raw_output": raw_output.strip(),
            "returncode": returncode,
            "parse_method": "junit_xml",
        }

    # Try to get JUnit XML
    xml_text: Optional[str] = None
    parse_method = "regex_fallback"

    # Priority 1: test injection point (unit tests supply XML directly)
    if "_junit_xml_override" in execution:
        xml_text = execution["_junit_xml_override"]
        parse_method = "junit_xml"
    elif "_junit_xml_data" in execution:
        # Priority 2: pre-fetched by evaluate_built_image via named container + docker cp
        xml_text = execution["_junit_xml_data"]
        parse_method = "junit_xml"
    else:
        # Priority 3 (fallback): attempt docker cp from image_tag — only works when
        # the container was NOT started with --rm (i.e. legacy callers outside repair loop).
        import tempfile
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
                tmp_path = tmp.name
            cp_result = subprocess.run(
                ["docker", "cp", f"{image_tag}:{junit_container_path}", tmp_path],
                capture_output=True,
                timeout=30,
            )
            if cp_result.returncode == 0:
                xml_text = Path(tmp_path).read_text(encoding="utf-8", errors="replace")
                parse_method = "junit_xml"
        except Exception:
            xml_text = None
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass

    # Parse XML if available
    if xml_text:
        try:
            parsed = _parse_junit_xml(xml_text)
            return {
                **parsed,
                "raw_output": raw_output.strip(),
                "returncode": returncode,
                "parse_method": "junit_xml",
            }
        except Exception:
            # XML malformed → fall through to regex fallback
            pass

    # Regex fallback
    parsed = _parse_pytest_stdout_regex(
        execution.get("stdout") or "",
        execution.get("stderr") or "",
    )
    return {
        **parsed,
        "raw_output": raw_output.strip(),
        "returncode": returncode,
        "parse_method": "regex_fallback",
    }


# ---------------------------------------------------------------------------
# Stage B — Glue: real_test_command
# ---------------------------------------------------------------------------

_JUNIT_CONTAINER_PATH = "/tmp/repair_junit.xml"

# Accepted pytest invocation prefixes (in priority order)
_PYTEST_PREFIXES = (
    "poetry run pytest",
    "uv run pytest",
    "python -m pytest",
    "pytest",
)

# Flags to strip before checking if a command is runnable
_FLAGS_TO_STRIP = {"--collect-only", "-q", "--quiet"}


def _strip_collect_flags(cmd: str) -> str:
    """Strip --collect-only, -q, --quiet from a pytest command string.

    Preserves all other flags and arguments.
    """
    tokens = shlex.split(cmd)
    filtered = [t for t in tokens if t not in _FLAGS_TO_STRIP]
    return " ".join(filtered)


def _cmd_starts_with_pytest(cmd: str) -> bool:
    """Return True if cmd starts with a recognized pytest invocation prefix."""
    stripped = cmd.strip()
    return any(stripped.startswith(pfx) for pfx in _PYTEST_PREFIXES)


def _extract_pytest_from_compound_command(cmd: str) -> Optional[str]:
    """Extract a pytest invocation from a compound command like 'cd dir && pytest ...'.

    When the command contains '&&', find the last segment that contains a recognized
    pytest prefix and return the full original command (minus --collect-only flags)
    so that the 'cd dir' prefix is preserved.

    Returns None if no pytest invocation is found anywhere in the command.
    """
    if "&&" not in cmd:
        return None

    segments = [s.strip() for s in cmd.split("&&")]
    # Find the last segment that starts with a pytest prefix
    pytest_segment_index: Optional[int] = None
    for i, seg in enumerate(segments):
        if _cmd_starts_with_pytest(seg):
            pytest_segment_index = i

    if pytest_segment_index is None:
        return None

    # Strip --collect-only flags from the pytest segment only
    pytest_seg = _strip_collect_flags(segments[pytest_segment_index])
    if not pytest_seg:
        return None

    # Rebuild: keep all segments up to and including the (stripped) pytest segment
    rebuilt_segments = segments[:pytest_segment_index] + [pytest_seg]
    return " && ".join(rebuilt_segments)


def real_test_command(recipe: dict[str, Any]) -> tuple[str, str]:
    """Derive a runnable test command from the recipe.

    SPEC: IMPLEMENTATION_SPEC.md §B.2

    Steps (in order):
    1. Read recipe.get("logs", {}).get("verified_test_commands") or [].
    2. Strip each command of --collect-only and any -q/--quiet flags.
    3. If stripped command is non-empty and starts with a recognized pytest prefix → accept.
       If command contains '&&' and pytest appears after the last '&&', preserve the full
       compound command (e.g. 'cd dir && pytest ...') minus --collect-only flags.
    4. If nothing passes → fallback 'pytest -q --disable-warnings'.
    5. Append --junitxml=/tmp/repair_junit.xml (unless already present).
    6. Return (command_with_junitxml, "/tmp/repair_junit.xml").
    """
    junit_path = _JUNIT_CONTAINER_PATH

    verified_cmds: list[str] = (
        (recipe.get("logs") or {}).get("verified_test_commands") or []
    )

    accepted: Optional[str] = None
    for cmd in verified_cmds:
        stripped = _strip_collect_flags(cmd)
        if stripped and _cmd_starts_with_pytest(stripped):
            accepted = stripped
            break
        # Handle compound commands like "cd subdir && pytest tests/"
        compound = _extract_pytest_from_compound_command(cmd)
        if compound is not None:
            accepted = compound
            break

    if accepted is None:
        accepted = "pytest -q --disable-warnings"

    # Append --junitxml= only if not already present
    if "--junitxml=" not in accepted:
        accepted = f"{accepted} --junitxml={junit_path}"

    return (accepted, junit_path)


# ---------------------------------------------------------------------------
# Stage C — _repair_and_rescore: verbatim Repo2Run repair loop
# ---------------------------------------------------------------------------


def _repair_and_rescore(
    out: dict[str, Any],
    root_path: str,
    full_name: str,
    llm: str,
    max_rounds: int = 2,
) -> dict[str, Any]:
    """Runner-side verbatim Repo2Run repair loop.

    Signature: IMPLEMENTATION_SPEC.md §C.1
    Loop body: verbatim from run_repo2run_benchmark.py:3398-3530
    Glue (path derivation, I/O, junit write): new; annotated inline.

    NEVER raises — degrades to original results on any error.
    Writes run_pytest_results.json UNCONDITIONALLY after each test attempt.
    """
    # ── GLUE: path derivation ───────────────────────────────────────────────
    out_dir = os.path.join(root_path, "output", full_name)
    slug = full_name.replace("/", "__")
    recipe_path = os.path.join(out_dir, f"{slug}.json")
    dockerfile_path = os.path.join(out_dir, "eval_build", "Dockerfile")
    pytest_json_path = os.path.join(out_dir, "run_pytest_results.json")
    repair_dir = os.path.join(out_dir, "repair_artifacts")
    os.makedirs(repair_dir, exist_ok=True)

    # ── GLUE: early-exit if already passing ─────────────────────────────────
    try:
        existing = json.loads(Path(pytest_json_path).read_text())
        summary = existing.get("summary", {})
        effective_total = summary.get("total_tests", 0) - summary.get("skipped", 0)
        passed = summary.get("passed", 0)
        if effective_total > 0 and passed == effective_total:
            return out  # already passing — do not touch
    except Exception:
        pass  # missing or malformed JSON → proceed

    # ── GLUE FIX (audit #2): preserve the framework's result; only overwrite if
    # STRICTLY better. The RAT framework already wrote run_pytest_results.json from
    # its own build+test. The repair must IMPROVE on it, never destroy it — e.g. a
    # repair-build failure must NOT clobber a real framework result with a build_failed
    # stub (this was wiping ~23 real results, mcp-atlassian 2578/2739 -> 0/0).
    try:
        _baseline_pr = json.loads(Path(pytest_json_path).read_text())
    except Exception:
        _baseline_pr = None

    def _pr_score(_pr):
        """Comparison key: (passed, total_tests). A build_failed stub = (0,0); absent = (-1,-1)."""
        if not _pr:
            return (-1, -1)
        _s = _pr.get("summary", {}) or {}
        return (_s.get("passed", 0), _s.get("total_tests", 0))

    _best_score = _pr_score(_baseline_pr)

    # ── GLUE: load recipe ───────────────────────────────────────────────────
    try:
        recipe = json.loads(Path(recipe_path).read_text())
    except Exception:
        return out  # no recipe → cannot repair

    # ── GLUE: load agent_run_summary from the agent-written workplace path ────
    # TRAJECTORY (plan property #1): the adapter writes the summary to
    #   ./workplace/multi_docker_eval_{slug}/agent_run_summary.json
    # relative to the repo root (DOCKERAGENT_ROOT / cwd) — NOT under root_path
    # (multi_docker_eval_adapter.py:758 uses "./workplace"; agent.py abspaths it).
    _workplace_root = os.environ.get("DOCKERAGENT_ROOT") or os.getcwd()
    _summary_candidates = [
        os.path.join(_workplace_root, "workplace", f"multi_docker_eval_{slug}", "agent_run_summary.json"),
        os.path.join("workplace", f"multi_docker_eval_{slug}", "agent_run_summary.json"),
    ]
    run_summary = None
    agent_summary_path = _summary_candidates[0]
    for _cand in _summary_candidates:
        try:
            run_summary = json.loads(Path(_cand).read_text())
            agent_summary_path = _cand
            break
        except Exception:
            continue
    if run_summary is None:
        # Defensive fallback: use recipe["logs"] which has verified_test_commands / build_recipe
        run_summary = recipe.get("logs") or {}
        if not run_summary.get("successful_actions"):
            print(
                f"[repair] WARNING {full_name}: agent_run_summary.json not found at"
                f" {agent_summary_path}; successful_actions will be empty."
                " Trajectory-aware LLM repair will have reduced fidelity.",
                flush=True,
            )
    else:
        _n_actions = len(run_summary.get("successful_actions") or [])
        print(
            f"[repair] {full_name}: loaded trajectory ({_n_actions} successful_actions)"
            f" from {agent_summary_path}",
            flush=True,
        )

    # ── GLUE: load current Dockerfile ────────────────────────────────────────
    try:
        current_eval_dockerfile_text = Path(dockerfile_path).read_text(encoding="utf-8")
    except Exception:
        return out  # no Dockerfile → cannot repair

    # ── GLUE: collect pip constraints from run_summary (verbatim helper) ─────
    pip_constraints = collect_observed_pip_install_constraints(None, run_summary)

    # ── GLUE: paths for docker build context ─────────────────────────────────
    eval_build_context_path = Path(out_dir) / "eval_build"
    repo_root = Path(out_dir)

    # ── GLUE: derive collect commands (mirrors run_repo2run_benchmark.py:3386-3393) ──
    # Use derive_repo2run_collect_commands (not derive_verification_commands) so that
    # filter_runtime_preparation_commands strips collect-only commands from runtime_commands.
    runtime_commands, _test_commands_collect, _source = derive_repo2run_collect_commands(
        eval_build_context_path, run_summary
    )
    # Override collect commands with the real (non-collect-only) command + junitxml appended
    real_cmd, junit_path = real_test_command(recipe)
    test_commands = [real_cmd]
    # Rewrite .dockerignore so test artifacts are not excluded from the image build.
    ensure_eval_dockerignore_includes_test_artifacts(
        eval_build_context_path,
        test_commands=_test_commands_collect,
        run_summary=run_summary,
    )

    # ── GLUE: synthetic instance dict for build_dockerfile_repair_input ──────
    instance = {
        "instance_id": slug,
        "full_name": full_name,
        "sha": out.get("head_sha", ""),
        "repo_url": (run_summary or {}).get("repo_url", ""),
    }

    # ── GLUE: unique image tag per repo+pid to avoid concurrency collisions ──
    safe_slug = re.sub(r"[^a-z0-9-]", "-", slug.lower())
    image_tag = f"dockeragent-repair-{safe_slug}-{os.getpid()}"

    # ── GLUE: docker_platform — read from recipe then fall back to env (fix (d)) ──
    docker_platform: Optional[str] = (
        (recipe or {}).get("docker_platform")
        or os.environ.get("DOCKER_DEFAULT_PLATFORM")
    ) or None

    # ── GLUE: shared accumulators + lazy OpenAI client ───────────────────────
    dockerfile_validation_attempts: list[dict] = []
    dockerfile_repair_rounds: list[dict] = []
    repair_client = None
    eval_dockerfile_path = eval_build_context_path / "Dockerfile"
    _pytest_json_written = False  # tracks whether run_pytest_results.json was written

    # ===== VERBATIM loop body from run_repo2run_benchmark.py:3398-3530 =====
    # (only I/O bindings are new glue; repair decision logic is byte-for-byte)
    try:
        for attempt_index in range(max_rounds + 1):                  # VERBATIM
            workdir = infer_workdir_from_dockerfile(current_eval_dockerfile_text)  # VERBATIM
            eval_dockerfile_path.write_text(current_eval_dockerfile_text, encoding="utf-8")  # VERBATIM

            docker_build_command = ["docker", "build"]               # VERBATIM
            if docker_platform:                                      # GLUE: fix (d)
                docker_build_command.extend(["--platform", docker_platform])
            docker_build_command.extend([                            # VERBATIM pattern
                "-f", str(eval_dockerfile_path),
                "-t", image_tag,
                str(eval_build_context_path),
            ])
            docker_build = run_command(                              # VERBATIM
                docker_build_command,
                cwd=repo_root,
                env=os.environ.copy(),
                timeout_seconds=1800,
            )

            test_execution = None
            if docker_build["returncode"] == 0 and not docker_build.get("timed_out"):  # VERBATIM
                test_execution = evaluate_built_image(               # VERBATIM
                    image_tag=image_tag,
                    workdir=workdir,
                    runtime_commands=runtime_commands,
                    test_commands=test_commands,
                    cwd=repo_root,
                    timeout_seconds=600,
                    workspace_root=eval_build_context_path,
                    docker_platform=docker_platform,               # GLUE: fix (d)
                    junit_container_path=junit_path,               # GLUE: fix (a)
                    attempt_index=attempt_index,                   # GLUE: fix (a)
                )

            attempt_success = bool(                                  # VERBATIM
                docker_build
                and docker_build["returncode"] == 0
                and not docker_build.get("timed_out")
                and test_execution
                and test_execution["all_test_commands_effective"]
            )
            dockerfile_validation_attempts.append({                  # VERBATIM
                "attempt": attempt_index,
                "dockerfile_path": str(eval_dockerfile_path),
                "docker_build": docker_build,
                "test_execution": test_execution,
                "success": attempt_success,
            })

            # GLUE: unconditional write of results after each test attempt
            # (mirrors Repo2Run "score the last attempt regardless")
            if test_execution is not None:
                _exec_dict = (
                    test_execution["results"][0]["execution"]
                    if test_execution.get("results")
                    else {}
                )
                pr = junit_to_pytest_results(
                    execution=_exec_dict,
                    image_tag=image_tag,
                    junit_container_path=junit_path,
                )
                # GLUE FIX (audit #2): only replace the on-disk result if STRICTLY
                # better than the best so far (framework baseline or a prior attempt).
                if _pr_score(pr) > _best_score:
                    Path(pytest_json_path).write_text(
                        json.dumps(pr, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    _best_score = _pr_score(pr)
                    _pytest_json_written = True
                # GLUE: write sidecar for audit trail
                try:
                    _sidecar_path = pytest_json_path.replace(
                        ".json", f"_repair_attempt{attempt_index}.json"
                    )
                    Path(_sidecar_path).write_text(
                        json.dumps(pr, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except Exception:
                    pass

            if attempt_success:                                      # VERBATIM
                break
            if attempt_index < max_rounds and test_execution:       # VERBATIM
                repaired_text, installed_requirements = repair_dockerfile_for_missing_python_modules(  # VERBATIM
                    current_eval_dockerfile_text,
                    test_execution,
                    eval_build_context_path,
                )
                if repaired_text != current_eval_dockerfile_text:    # VERBATIM
                    dockerfile_repair_rounds.append({                # VERBATIM
                        "round": attempt_index + 1,
                        "source": "deterministic_missing_python_modules",
                        "error": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                        "raw_content": "",
                        "dockerfile_text": repaired_text,
                        "rationale": (
                            "Installed missing Python modules reported by pytest collection: "
                            + ", ".join(installed_requirements)
                        ),
                        "confidence": "high",
                        "log_path": None,
                    })
                    current_eval_dockerfile_text = normalize_eval_dockerfile_for_replay(  # VERBATIM
                        repaired_text,
                        pip_constraints=pip_constraints,
                    )
                    continue                                          # VERBATIM
            if attempt_index >= max_rounds:                          # VERBATIM
                break
            if docker_build_failed_due_to_unavailable_daemon(docker_build):  # VERBATIM
                break

            repair_input = build_dockerfile_repair_input(            # VERBATIM
                instance=instance,
                workdir=workdir,
                dockerfile_text=current_eval_dockerfile_text,
                run_summary=run_summary,
                runtime_commands=runtime_commands,
                test_commands=test_commands,
                docker_build=docker_build,
                test_execution=test_execution,
            )
            try:
                if repair_client is None:                            # VERBATIM
                    repair_client = create_openai_client_from_env()  # VERBATIM
                repair_result = repair_dockerfile_with_llm(          # VERBATIM
                    client=repair_client,
                    model=llm,                                       # GLUE: model.llm passed as param
                    repair_input=repair_input,
                    artifact_dir=Path(repair_dir),
                    round_index=attempt_index + 1,
                )
            except Exception as exc:                                 # VERBATIM
                repair_result = {
                    "round": attempt_index + 1,
                    "source": "llm_error",
                    "error": str(exc),
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    "raw_content": "",
                    "dockerfile_text": None,
                    "rationale": "",
                    "confidence": "low",
                    "log_path": None,
                }
            dockerfile_repair_rounds.append(repair_result)          # VERBATIM
            repaired_text = repair_result.get("dockerfile_text")    # VERBATIM
            if not repaired_text:                                    # VERBATIM
                break
            current_eval_dockerfile_text = normalize_eval_dockerfile_for_replay(  # VERBATIM
                repaired_text,
                pip_constraints=pip_constraints,
            )
        # ===== END VERBATIM LOOP =====

        # GLUE fix (b): if all build attempts failed AND there was NO framework result
        # to preserve, write a build_failed stub so scorers don't read stale data.
        # CRITICAL: only stub when _baseline_pr is None — never clobber a real framework
        # result with a 0-tests stub just because the repair's own rebuild failed.
        if not _pytest_json_written and _baseline_pr is None:
            _build_failed_stub = {
                "summary": {
                    "total_tests": 0,
                    "passed": 0,
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                    "xfailed": 0,
                    "xpassed": 0,
                },
                "error_breakdown": {},
                "failed_tests": [],
                "returncode": 1,
                "parse_method": "build_failed",
            }
            try:
                Path(pytest_json_path).write_text(
                    json.dumps(_build_failed_stub, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                pass

    except Exception as _exc:
        # GLUE: never-raise contract; degrade to original results
        print(f"[repair] {full_name} — repair loop exception (non-fatal): {_exc}", flush=True)

    finally:
        # GLUE: always attempt to remove the repair image to free disk
        try:
            subprocess.run(["docker", "rmi", "-f", image_tag],
                           capture_output=True, timeout=30)
        except Exception:
            pass

    # GLUE: write repair metadata sidecar
    try:
        sidecar = {
            "full_name": full_name,
            "repair_rounds": len(dockerfile_repair_rounds),
            "validation_attempts": len(dockerfile_validation_attempts),
            "repair_history": dockerfile_repair_rounds,
        }
        Path(os.path.join(repair_dir, "repair_meta.json")).write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass

    return out
