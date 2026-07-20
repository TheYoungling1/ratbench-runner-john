#!/usr/bin/env python3
"""Run the standalone Repo2Run benchmark against this project without Multi-Docker-Eval."""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from src.constants import DEFAULT_LLM_MODEL, DEFAULT_MEMORY_EMBEDDING_MODEL
from src.repo2run_dataset import load_repo2run_dataset
from src.synthesizer import (
    Synthesizer,
    build_resilient_apt_install_run_instruction,
    build_resilient_pip_install_run_instruction,
)
from src.verification_bundle import derive_supported_verification_bundle
from src.workplace_replay import (
    create_openai_client_from_env,
    load_platform_override_from_workplace,
    resynthesize_dockerfile_from_existing_workplace,
)


DOCKER_TIMEOUT_EXIT_CODE = 124
TEST_SIGNAL_DETECTOR = Synthesizer()
TEST_EXECUTION_SHELL_WRAPPER = (
    "if command -v bash >/dev/null 2>&1; then exec bash -s; else exec sh -s; fi"
)
REPO2RUN_PYTEST_COLLECT_COMMAND = "pytest --collect-only -q --disable-warnings"
REPO2RUN_POETRY_COLLECT_COMMAND = "poetry run pytest --collect-only -q --disable-warnings"
REPO2RUN_UV_COLLECT_COMMAND = f"uv run {REPO2RUN_PYTEST_COLLECT_COMMAND}"
REPO2RUN_PDM_COLLECT_COMMAND = f"pdm run {REPO2RUN_PYTEST_COLLECT_COMMAND}"
REPO2RUN_EVAL_TOOL_INSTALL = (
    "RUN (python -m pip install pytest pytest-xdist poetry || "
    "python3 -m pip install pytest pytest-xdist poetry || "
    "pip install pytest pytest-xdist poetry)"
)
OBSERVED_PIP_CONSTRAINTS_PATH = "/tmp/jayint-pip-constraints.txt"
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


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted(_json_safe(item) for item in value)
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(payload), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _decode_command_stream(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def sanitize_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)


def load_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_executable_path(raw_value: str) -> str:
    candidate = Path(raw_value)
    if candidate.is_absolute() or "/" in raw_value:
        return str(candidate.resolve())
    resolved = shutil.which(raw_value)
    return resolved or raw_value


def normalize_command_list(commands: Any) -> list[str]:
    if isinstance(commands, str):
        commands = [commands]
    normalized: list[str] = []
    for command in commands or []:
        text = str(command or "").strip()
        if text:
            normalized.append(text)
    return normalized


def resolve_benchmark_platform(
    workplace: Path,
    run_summary: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    summary = run_summary or {}
    platform_override = str(summary.get("platform_override") or "").strip()
    if platform_override:
        return platform_override
    return load_platform_override_from_workplace(workplace)


def build_agent_command(
    *,
    python_executable: str,
    repo_root: Path,
    instance: dict[str, Any],
    workplace: Path,
    args: argparse.Namespace,
) -> list[str]:
    command = [
        python_executable,
        str(repo_root / "agent.py"),
        instance["repo_url"],
        "--base-commit",
        instance["base_commit"],
        "--image",
        args.base_image,
        "--model",
        args.model,
        "--steps",
        str(args.max_steps),
        "--workplace",
        str(workplace),
        "--command-timeout",
        str(args.agent_command_timeout),
    ]

    if args.enable_observation_compression:
        command.append("--enable-observation-compression")
    if args.enable_long_term_memory:
        command.append("--enable-long-term-memory")
        command.extend(["--memory-embedding-model", args.memory_embedding_model])
        if args.memory_path:
            command.extend(["--memory-path", args.memory_path])
    if args.keep_container:
        command.append("--keep-container")

    # EnvState / ablation-arm flags (§9.1 — forwarded only when set).
    if getattr(args, "enable_supervisor", False):
        command.append("--enable-supervisor")
    if getattr(args, "enable_fullstate_worker", False):
        command.append("--enable-fullstate-worker")
    if getattr(args, "fullstate_worker_prompt", False):
        command.append("--fullstate-worker-prompt")
    if getattr(args, "enable_envstate", False):
        command.append("--enable-envstate")
    if getattr(args, "enable_cleanroom", False):
        command.append("--enable-cleanroom")
    if getattr(args, "enable_v1", False):
        command.append("--enable-v1")
    if getattr(args, "enable_contract_graph", False):
        command.append("--enable-contract-graph")
    if getattr(args, "enable_dep_graph", False):
        command.append("--enable-dep-graph")
    if getattr(args, "enable_dep_emit", False):
        command.append("--enable-dep-emit")
    if getattr(args, "enable_runtime_feedback", False):
        command.append("--enable-runtime-feedback")
    if getattr(args, "enable_graph_scheduler", False):
        command.append("--enable-graph-scheduler")
    if getattr(args, "enable_runtime_pin", False):
        command.append("--enable-runtime-pin")
    # enable_service_provision: agent.py reads DOCKERAGENT_ENABLE_SERVICE_PROVISION
    # from os.environ only (~:1154); there is NO --enable-service-provision CLI flag,
    # so we inject the env var instead of appending a flag.
    # The else-branch is mandatory: without it the env var leaks into subsequent
    # calls (e.g. a v3-arm run followed by a v0-arm run in the same process).
    if getattr(args, "enable_service_provision", False):
        os.environ["DOCKERAGENT_ENABLE_SERVICE_PROVISION"] = "1"
    else:
        os.environ.pop("DOCKERAGENT_ENABLE_SERVICE_PROVISION", None)

    return command


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


def agent_run_completed_successfully(agent_run: Optional[dict[str, Any]]) -> bool:
    return bool(
        agent_run
        and agent_run.get("returncode") == 0
        and not agent_run.get("timed_out")
    )


def should_use_agent_dockerfile(
    agent_run: Optional[dict[str, Any]],
    *,
    reused_existing_workplace: bool,
    run_summary: Optional[dict[str, Any]] = None,
) -> tuple[bool, Optional[str]]:
    if reused_existing_workplace:
        return True, None
    if agent_run_completed_successfully(agent_run):
        if _run_summary_reports_unusable_agent_configuration(run_summary):
            return False, "agent_configuration_unsuccessful"
        return True, None
    return False, "agent_run_failed_or_timed_out"


def _run_summary_reports_unusable_agent_configuration(run_summary: Optional[dict[str, Any]]) -> bool:
    if not isinstance(run_summary, dict):
        return False
    if run_summary.get("configuration_success") is not False:
        return False

    verification_bundle = run_summary.get("verification_bundle") or {}
    has_verified_tests = bool(
        normalize_command_list(verification_bundle.get("test_commands"))
        or normalize_command_list(run_summary.get("verified_test_commands"))
        or normalize_command_list(run_summary.get("verified_test_command"))
        or normalize_command_list(run_summary.get("successful_test_commands"))
    )
    return not has_verified_tests


def prepare_eval_build_context(
    source_workplace: Path,
    destination: Path,
    *,
    base_commit: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> dict[str, Any]:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    git_dir = source_workplace / ".git"
    if git_dir.exists():
        clone_result = run_command(
            ["git", "clone", "--no-hardlinks", str(source_workplace), str(destination)],
            cwd=cwd or source_workplace.parent,
        )
        checkout_result = None
        clean_result = None
        if clone_result["returncode"] == 0 and not clone_result.get("timed_out"):
            if base_commit:
                checkout_result = run_command(
                    ["git", "checkout", "--force", str(base_commit)],
                    cwd=destination,
                )
            clean_result = run_command(
                ["git", "clean", "-fdx"],
                cwd=destination,
            )
            success = (
                (checkout_result is None or checkout_result["returncode"] == 0)
                and clean_result["returncode"] == 0
            )
        else:
            success = False

        return {
            "method": "local_git_clone",
            "source": str(source_workplace),
            "destination": str(destination),
            "base_commit": base_commit,
            "success": success,
            "clone": clone_result,
            "checkout": checkout_result,
            "clean": clean_result,
            "path": str(destination if success else source_workplace),
        }

    shutil.copytree(source_workplace, destination)
    return {
        "method": "workspace_copy_fallback",
        "source": str(source_workplace),
        "destination": str(destination),
        "base_commit": base_commit,
        "success": True,
        "warning": "source workspace has no .git directory; copied workspace state may already include agent mutations",
        "path": str(destination),
    }


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


def _format_scalar_for_markdown(value: Any) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _write_command_stream_logs(log_dir: Path, prefix: str, command_result: Optional[dict[str, Any]]) -> dict[str, str]:
    if not command_result:
        return {}

    stdout_path = log_dir / f"{prefix}.stdout.log"
    stderr_path = log_dir / f"{prefix}.stderr.log"
    write_text(stdout_path, str(command_result.get("stdout") or ""))
    write_text(stderr_path, str(command_result.get("stderr") or ""))
    return {
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }


def _render_command_result_section(
    title: str,
    command_result: Optional[dict[str, Any]],
    stream_logs: Optional[dict[str, str]] = None,
) -> list[str]:
    lines = [f"## {title}"]
    if not command_result:
        lines.append("(not run)")
        lines.append("")
        return lines

    lines.extend(
        [
            f"- Command: `{command_result.get('command_shell', '')}`",
            f"- Return Code: `{_format_scalar_for_markdown(command_result.get('returncode'))}`",
            f"- Timed Out: `{_format_scalar_for_markdown(command_result.get('timed_out'))}`",
            f"- Duration Seconds: `{_format_scalar_for_markdown(command_result.get('duration_seconds'))}`",
            f"- Started At: `{_format_scalar_for_markdown(command_result.get('started_at'))}`",
            f"- Finished At: `{_format_scalar_for_markdown(command_result.get('finished_at'))}`",
            f"- CWD: `{_format_scalar_for_markdown(command_result.get('cwd'))}`",
        ]
    )
    if stream_logs:
        lines.append(f"- Stdout Log: `{stream_logs.get('stdout_log', '')}`")
        lines.append(f"- Stderr Log: `{stream_logs.get('stderr_log', '')}`")
    lines.append("")
    return lines


def write_instance_debug_artifacts(
    artifact_dir: Path,
    instance: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    terminal_logs_dir = artifact_dir / "terminal_logs"
    terminal_logs_dir.mkdir(parents=True, exist_ok=True)

    agent_logs = _write_command_stream_logs(
        terminal_logs_dir,
        "agent_run",
        payload.get("agent_run"),
    )
    docker_build_logs = _write_command_stream_logs(
        terminal_logs_dir,
        "docker_build",
        payload.get("docker_build"),
    )
    docker_cleanup_logs = _write_command_stream_logs(
        terminal_logs_dir,
        "docker_cleanup",
        payload.get("docker_cleanup"),
    )
    validation_attempt_logs: list[dict[str, Any]] = []
    for attempt in payload.get("dockerfile_validation_attempts") or []:
        attempt_index = attempt.get("attempt")
        attempt_build_logs = _write_command_stream_logs(
            terminal_logs_dir,
            f"docker_build_attempt_{attempt_index}",
            attempt.get("docker_build"),
        )
        attempt_test_logs = []
        attempt_test_execution = attempt.get("test_execution") or {}
        for test_index, item in enumerate(attempt_test_execution.get("results") or [], start=1):
            execution_logs = _write_command_stream_logs(
                terminal_logs_dir,
                f"test_execution_attempt_{attempt_index}_{test_index}",
                item.get("execution"),
            )
            attempt_test_logs.append(
                {
                    "index": test_index,
                    "test_command": item.get("test_command"),
                    "stdout_log": execution_logs.get("stdout_log"),
                    "stderr_log": execution_logs.get("stderr_log"),
                }
            )
        validation_attempt_logs.append(
            {
                "attempt": attempt_index,
                "success": attempt.get("success"),
                "docker_build": attempt_build_logs,
                "test_execution": attempt_test_logs,
            }
        )

    test_execution_logs: list[dict[str, Any]] = []
    test_execution = payload.get("test_execution") or {}
    for index, item in enumerate(test_execution.get("results") or [], start=1):
        execution_logs = _write_command_stream_logs(
            terminal_logs_dir,
            f"test_execution_{index}",
            item.get("execution"),
        )
        test_execution_logs.append(
            {
                "index": index,
                "test_command": item.get("test_command"),
                "stdout_log": execution_logs.get("stdout_log"),
                "stderr_log": execution_logs.get("stderr_log"),
            }
        )

    log_lines = [
        "# Repo2Run Benchmark Run Log",
        "",
        "## Instance",
        f"- Instance ID: `{instance.get('instance_id', '')}`",
        f"- Full Name: `{instance.get('full_name', '')}`",
        f"- SHA: `{instance.get('sha', '')}`",
        f"- Repo URL: `{instance.get('repo_url', '')}`",
        "",
        "## Outcome",
        f"- Execution Status: `{payload.get('execution_status')}`",
        f"- Dockerfile Generation Success: `{_format_scalar_for_markdown(payload.get('dockerfile_generation_success'))}`",
        f"- Environment Build Success: `{_format_scalar_for_markdown(payload.get('environment_build_success'))}`",
        f"- Paper Build Success: `{_format_scalar_for_markdown(payload.get('paper_build_success'))}`",
        f"- Paper Alignment: `{payload.get('paper_alignment')}`",
        f"- Docker Platform: `{_format_scalar_for_markdown(payload.get('docker_platform'))}`",
        f"- Verification Command Source: `{_format_scalar_for_markdown(payload.get('verification_command_source'))}`",
        f"- Agent Dockerfile Present: `{_format_scalar_for_markdown(payload.get('agent_dockerfile_present'))}`",
        f"- Agent Dockerfile Usable: `{_format_scalar_for_markdown(payload.get('agent_dockerfile_usable'))}`",
        f"- Agent Dockerfile Ignored Reason: `{_format_scalar_for_markdown(payload.get('agent_dockerfile_ignored_reason'))}`",
        "",
        "## Paths",
        f"- Agent Run Summary: `{_format_scalar_for_markdown(payload.get('run_summary_path'))}`",
        f"- Agent Dockerfile: `{_format_scalar_for_markdown(payload.get('agent_dockerfile_path'))}`",
        f"- Eval Dockerfile: `{_format_scalar_for_markdown(payload.get('eval_dockerfile_path'))}`",
        f"- Eval Build Context: `{_format_scalar_for_markdown(payload.get('eval_build_context_path'))}`",
        f"- Result JSON: `{_format_scalar_for_markdown(payload.get('result_json_path'))}`",
        "",
    ]

    log_lines.extend(_render_command_result_section("Agent Run", payload.get("agent_run"), agent_logs))

    eval_context_preparation = payload.get("eval_context_preparation") or {}
    eval_dockerignore_test_artifacts = (
        payload.get("eval_dockerignore_test_artifacts") or {}
    )
    log_lines.append("## Eval Build Context")
    if eval_context_preparation:
        log_lines.extend(
            [
                f"- Method: `{_format_scalar_for_markdown(eval_context_preparation.get('method'))}`",
                f"- Success: `{_format_scalar_for_markdown(eval_context_preparation.get('success'))}`",
                f"- Source: `{_format_scalar_for_markdown(eval_context_preparation.get('source'))}`",
                f"- Destination: `{_format_scalar_for_markdown(eval_context_preparation.get('destination'))}`",
                f"- Base Commit: `{_format_scalar_for_markdown(eval_context_preparation.get('base_commit'))}`",
                f"- Warning: `{_format_scalar_for_markdown(eval_context_preparation.get('warning'))}`",
                f"- Dockerignore Test Artifact Fix: `{_format_scalar_for_markdown(eval_dockerignore_test_artifacts.get('reason'))}`",
                f"- Dockerignore Changed: `{_format_scalar_for_markdown(eval_dockerignore_test_artifacts.get('changed'))}`",
                f"- Test Artifact Paths: `{_format_scalar_for_markdown(', '.join(eval_dockerignore_test_artifacts.get('test_artifact_paths') or []))}`",
                f"- Removed Dockerignore Patterns: `{_format_scalar_for_markdown(', '.join(eval_dockerignore_test_artifacts.get('removed_patterns') or []))}`",
                "",
            ]
        )
    else:
        log_lines.append("(not run)")
        log_lines.append("")

    resynthesis = payload.get("resynthesis")
    log_lines.append("## Resynthesis")
    if resynthesis:
        for key in sorted(resynthesis.keys()):
            log_lines.append(f"- {key}: `{_format_scalar_for_markdown(resynthesis.get(key))}`")
    else:
        log_lines.append("(not run)")
    log_lines.append("")

    log_lines.extend(_render_command_result_section("Docker Build", payload.get("docker_build"), docker_build_logs))

    log_lines.append("## Dockerfile Repair")
    repair_rounds = payload.get("dockerfile_repair_rounds") or []
    if repair_rounds:
        for repair in repair_rounds:
            log_lines.extend(
                [
                    f"### Repair Round {repair.get('round')}",
                    f"- Source: `{_format_scalar_for_markdown(repair.get('source'))}`",
                    f"- Error: `{_format_scalar_for_markdown(repair.get('error'))}`",
                    f"- Confidence: `{_format_scalar_for_markdown(repair.get('confidence'))}`",
                    f"- Log Path: `{_format_scalar_for_markdown(repair.get('log_path'))}`",
                    f"- Rationale: `{_format_scalar_for_markdown(repair.get('rationale'))}`",
                    "",
                ]
            )
    else:
        log_lines.append("(not run)")
        log_lines.append("")

    log_lines.append("## Dockerfile Validation Attempts")
    if validation_attempt_logs:
        for attempt_logs in validation_attempt_logs:
            attempt = attempt_logs.get("attempt")
            log_lines.extend(
                [
                    f"### Attempt {attempt}",
                    f"- Success: `{_format_scalar_for_markdown(attempt_logs.get('success'))}`",
                    f"- Docker Build Stdout Log: `{_format_scalar_for_markdown((attempt_logs.get('docker_build') or {}).get('stdout_log'))}`",
                    f"- Docker Build Stderr Log: `{_format_scalar_for_markdown((attempt_logs.get('docker_build') or {}).get('stderr_log'))}`",
                ]
            )
            for item in attempt_logs.get("test_execution") or []:
                log_lines.extend(
                    [
                        f"- Test {item.get('index')} Command: `{_format_scalar_for_markdown(item.get('test_command'))}`",
                        f"- Test {item.get('index')} Stdout Log: `{_format_scalar_for_markdown(item.get('stdout_log'))}`",
                        f"- Test {item.get('index')} Stderr Log: `{_format_scalar_for_markdown(item.get('stderr_log'))}`",
                    ]
                )
            log_lines.append("")
    else:
        log_lines.append("(not run)")
        log_lines.append("")

    log_lines.extend(
        [
            "## Verification Commands",
            "### Runtime Preparation Commands",
        ]
    )
    runtime_commands = payload.get("runtime_preparation_commands") or []
    if runtime_commands:
        log_lines.extend(f"- `{command}`" for command in runtime_commands)
    else:
        log_lines.append("- `(none)`")
    log_lines.extend(
        [
            "",
            "### Test Commands",
        ]
    )
    test_commands = payload.get("test_commands") or []
    if test_commands:
        log_lines.extend(f"- `{command}`" for command in test_commands)
    else:
        log_lines.append("- `(none)`")
    log_lines.append("")

    log_lines.append("## Test Execution")
    if test_execution:
        log_lines.extend(
            [
                f"- Workdir: `{_format_scalar_for_markdown(test_execution.get('workdir'))}`",
                f"- Effective Test Command Count: `{_format_scalar_for_markdown(test_execution.get('effective_test_command_count'))}`",
                f"- All Test Commands Effective: `{_format_scalar_for_markdown(test_execution.get('all_test_commands_effective'))}`",
                "",
            ]
        )
        for item, item_logs in zip(test_execution.get("results") or [], test_execution_logs):
            execution = item.get("execution") or {}
            classification = item.get("classification") or {}
            log_lines.extend(
                [
                    f"### Test Command {item_logs['index']}",
                    f"- Command: `{item.get('test_command', '')}`",
                    f"- Effective: `{_format_scalar_for_markdown(classification.get('effective'))}`",
                    f"- Reason: `{_format_scalar_for_markdown(classification.get('reason'))}`",
                    f"- Return Code: `{_format_scalar_for_markdown(execution.get('returncode'))}`",
                    f"- Stdout Log: `{_format_scalar_for_markdown(item_logs.get('stdout_log'))}`",
                    f"- Stderr Log: `{_format_scalar_for_markdown(item_logs.get('stderr_log'))}`",
                    "",
                    "#### Script",
                    "```sh",
                    str(item.get("script") or "").rstrip(),
                    "```",
                    "",
                ]
            )
    else:
        log_lines.append("(not run)")
        log_lines.append("")

    log_lines.extend(_render_command_result_section("Docker Cleanup", payload.get("docker_cleanup"), docker_cleanup_logs))

    benchmark_log_path = artifact_dir / "benchmark_run.md"
    write_text(benchmark_log_path, "\n".join(log_lines).rstrip() + "\n")

    return {
        "benchmark_log_path": str(benchmark_log_path),
        "terminal_logs_dir": str(terminal_logs_dir),
        "agent_run": agent_logs,
        "docker_build": docker_build_logs,
        "dockerfile_validation_attempts": validation_attempt_logs,
        "test_execution": test_execution_logs,
        "docker_cleanup": docker_cleanup_logs,
    }


_DOCKERFILE_VARIABLE_RE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)


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


def render_eval_dockerfile(agent_dockerfile_text: str) -> str:
    lines = agent_dockerfile_text.splitlines()
    rendered: list[str] = []
    inserted_copy = False
    workdir = infer_workdir_from_dockerfile(agent_dockerfile_text)
    last_from_index = -1
    for index, line in enumerate(lines):
        if line.strip().upper().startswith("FROM "):
            last_from_index = index
    already_copies_context = any(
        line.strip().upper().startswith("COPY ") and "." in line.split()
        for line in lines
    )

    for index, line in enumerate(lines):
        rendered.append(line)
        stripped = line.strip()
        if index == last_from_index:
            rendered.append(REPO2RUN_EVAL_TOOL_INSTALL)
        if (
            not inserted_copy
            and not already_copies_context
            and stripped.upper().startswith("WORKDIR ")
        ):
            rendered.append(f"COPY . {workdir}")
            next_line_is_blank = index + 1 < len(lines) and lines[index + 1].strip() == ""
            if not next_line_is_blank:
                rendered.append("")
            inserted_copy = True

    if not inserted_copy and not already_copies_context:
        if rendered and rendered[-1] != "":
            rendered.append("")
        rendered.extend(
            [
                f"WORKDIR {workdir}",
                f"COPY . {workdir}",
            ]
        )

    return "\n".join(rendered).rstrip() + "\n"


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


_SUCCESSFULLY_INSTALLED_BLOCK_RE = re.compile(
    r"^[ \t]*Successfully installed[ \t]+(?P<packages>[^\r\n]*)",
    flags=re.MULTILINE,
)
_INSTALLED_PACKAGE_TOKEN_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)-(?P<version>[0-9](?:[A-Za-z0-9_.!+~-]*[A-Za-z0-9!+~])?)$"
)


def _normalize_pip_constraint_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name or "").strip()).lower()


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


def _render_observed_pip_constraints_instruction(pip_constraints: dict[str, str]) -> str:
    constraint_lines = [
        f"{name}=={version}"
        for name, version in sorted((pip_constraints or {}).items())
        if name and version
    ]
    quoted_lines = " ".join(shlex.quote(line) for line in constraint_lines)
    return f"RUN printf '%s\\n' {quoted_lines} > {OBSERVED_PIP_CONSTRAINTS_PATH}"


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


_SHELL_CONTROL_TOKENS = {"&&", "||", ";", "|"}


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


def split_heavy_pip_install_replay_commands(command: str) -> list[str]:
    """Split expensive optional ML deps out of replay installs when safe.

    `sentence-transformers` can pull GPU/CUDA torch wheels on Linux. Keeping the
    package marker with `--no-deps` lets import-light collection runs proceed,
    and any real missing dependency will surface as a precise test failure
    instead of a Docker build timeout.
    """
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


_CUDA_SKIPPED_LOCAL_SOURCE_INSTALL_RE = re.compile(
    r"(?P<install>"
    r"(?:[A-Za-z_][A-Za-z0-9_]*_)?SKIP_CUDA_BUILD=TRUE\s+"
    r"(?:(?:python(?:2|3)?(?:\.\d+)?\s+-m\s+pip)|pip3?|uv\s+pip)\s+"
    r"install\s+\."
    r"(?:(?!\s(?:&&|\|\||;|\|)\s).)*"
    r")"
    r"(?=$|\s(?:&&|\|\||;|\|)\s)",
)


def _harden_cuda_skipped_local_source_install(command: str) -> str:
    """Prevent CUDA-skipped source installs from re-resolving heavy GPU deps.

    The setup agent often installs dependencies first, then installs a local
    CUDA project with `*_SKIP_CUDA_BUILD=TRUE pip install .`. During replay,
    letting pip resolve that local project's dependencies can pull a newer
    torch/CUDA stack than the verified sandbox trajectory. `--no-deps` keeps
    the source install focused on the local package; any truly missing
    dependency will surface in the final collect/test step.
    """

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


def _repair_generated_apt_retry_status_variables(command: str) -> str:
    if "JAYINT_APT_ATTEMPT" not in (command or ""):
        return command
    if "JAYINT_PIP_STATUS" not in command and "JAYINT_PIP_MAX_ATTEMPTS" not in command:
        return command
    return (
        command.replace("JAYINT_PIP_STATUS", "JAYINT_APT_STATUS")
        .replace("JAYINT_PIP_MAX_ATTEMPTS", "JAYINT_APT_MAX_ATTEMPTS")
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
    """Repair stale retry wrappers that stranded a multiline apt package list.

    Older normalization could wrap only the first line of a continued apt
    install and leave indented package lines behind, which Docker then parsed as
    top-level instructions such as `gcc` or `build-essential`.
    """

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


def _normalize_python_module_pytest_prefix(command: str) -> str:
    normalized = " ".join(str(command or "").split())
    for python_prefix in ("python -m ", "python3 -m "):
        if normalized.startswith(f"{python_prefix}pytest "):
            return "pytest " + normalized[len(f"{python_prefix}pytest ") :]
    return normalized


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


def select_repo2run_collect_command_from_run_summary(
    run_summary: Optional[dict[str, Any]],
) -> Optional[tuple[str, str]]:
    selected = select_repo2run_collect_commands_from_run_summary(run_summary)
    if not selected:
        return None
    commands, source = selected
    return commands[0], source


def filter_runtime_preparation_commands(commands: list[str]) -> list[str]:
    filtered: list[str] = []
    for command in commands or []:
        normalized = normalize_repo2run_collect_candidate(command)
        if repo2run_collect_source_for_command(normalized):
            continue
        filtered.append(command)
    return filtered


def compose_in_image_service_commands(run_summary: Optional[dict[str, Any]]) -> list[str]:
    """Root-wrapped start/wait/createdb(fatal) lines for confirmed in-image
    services, read from the agent's handoff field. Prepended to runtime_commands
    so they run in the same shell as the tests (design §8.3). Empty when absent."""
    if not isinstance(run_summary, dict):
        return []
    services = run_summary.get("confirmed_in_image_services") or []
    out: list[str] = []
    for svc in services:
        if not isinstance(svc, dict):
            continue
        if svc.get("start"):
            out.append(svc["start"])
        if svc.get("wait"):
            out.append(svc["wait"])
        if svc.get("createdb"):  # FATAL — never `|| true`
            out.append(svc["createdb"])
        if svc.get("var") and svc.get("url"):  # AFTER createdb — DB up first
            out.append(f"export {svc['var']}={shlex.quote(svc['url'])}")
    return out


def derive_repo2run_collect_commands(
    workspace_root: Path,
    run_summary: Optional[dict[str, Any]] = None,
) -> tuple[list[str], list[str], str]:
    supported_bundle = derive_supported_verification_bundle(run_summary)
    runtime_commands = filter_runtime_preparation_commands(
        normalize_command_list(supported_bundle.get("runtime_preparation_commands"))
    )
    runtime_commands = compose_in_image_service_commands(run_summary) + runtime_commands
    agent_verified_choice = select_repo2run_collect_commands_from_run_summary(run_summary)
    if agent_verified_choice is not None:
        commands, source = agent_verified_choice
        return runtime_commands, commands, source

    if workspace_uses_poetry(workspace_root):
        return [], [REPO2RUN_POETRY_COLLECT_COMMAND], "repo2run_poetry_collect_only"
    return [], [REPO2RUN_PYTEST_COLLECT_COMMAND], "repo2run_pytest_collect_only"


def derive_verification_commands(run_summary: Optional[dict[str, Any]]) -> tuple[list[str], list[str], str]:
    supported_bundle = derive_supported_verification_bundle(run_summary)

    runtime_commands = normalize_command_list(supported_bundle.get("runtime_preparation_commands"))
    runtime_commands = compose_in_image_service_commands(run_summary) + runtime_commands
    test_commands = normalize_command_list(supported_bundle.get("test_commands"))
    source = "supported_verification_bundle"
    if not test_commands:
        test_commands = ["pytest"]
        source = "default_pytest"

    return runtime_commands, test_commands, source


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


_MISSING_PYTHON_MODULE_RE = re.compile(
    r"(?:ModuleNotFoundError|ImportError):\s+No module named ['\"](?P<module>[^'\"]+)['\"]"
)
_KNOWN_MISSING_MODULE_PACKAGE_FALLBACKS = {
    "ppocr": ("paddleocr", "paddleocr==2.7.3"),
    "ppstructure": ("paddleocr", "paddleocr==2.7.3"),
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


def _strip_requirement_line(line: str) -> str:
    stripped = (line or "").strip()
    if not stripped or stripped.startswith("#"):
        return ""
    if " #" in stripped:
        stripped = stripped.split(" #", 1)[0].strip()
    return stripped


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


def compute_paper_alignment(expected_success: bool, observed_success: bool) -> str:
    if observed_success and expected_success:
        return "matched_success"
    if (not observed_success) and (not expected_success):
        return "matched_failure"
    if observed_success and not expected_success:
        return "unexpected_success"
    return "unexpected_failure"


def compute_execution_status(
    agent_run: dict[str, Any],
    dockerfile_present: bool,
    docker_build_success: bool,
    environment_build_success: bool,
) -> str:
    if environment_build_success:
        return "environment_built"
    if not dockerfile_present:
        return "dockerfile_missing"
    if not docker_build_success:
        return "docker_build_failed"
    if agent_run.get("returncode") != 0:
        return "agent_command_failed"
    return "test_execution_failed"


def build_docker_image_tag(instance_id: str) -> str:
    return f"jayint-repo2run-{sanitize_name(instance_id).lower()}"


def remove_docker_image(image_tag: str, cwd: Path) -> dict[str, Any]:
    return run_command(
        ["docker", "image", "rm", "-f", image_tag],
        cwd=cwd,
    )


def should_add_postgres_host_alias(
    workspace_root: Optional[Path],
    runtime_commands: list[str],
    test_commands: list[str],
    run_summary: Optional[dict[str, Any]] = None,
) -> bool:
    if isinstance(run_summary, dict) and run_summary.get("confirmed_in_image_services"):
        return True
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
    run_summary: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    command_results: list[dict[str, Any]] = []
    internal_import_prefixes = (
        discover_internal_import_prefixes(workspace_root) if workspace_root else None
    )
    add_postgres_host_alias = should_add_postgres_host_alias(
        workspace_root,
        runtime_commands,
        test_commands,
        run_summary,
    )

    for test_command in test_commands:
        script = build_test_execution_script(workdir, runtime_commands, test_command)
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


# ---------------------------------------------------------------------------
# Ablation-experiment arm presets (§3.1, §9.1 of the experiment design spec)
# Exposed at module level so tests can import _ARM_PRESETS directly.
# ---------------------------------------------------------------------------
_ARM_PRESETS: dict[str, dict] = {
    "0": {
        "enable_supervisor": False,
        "enable_fullstate_worker": False,
        "fullstate_worker_prompt": False,
        "enable_envstate": False,
        "enable_v1": False,
        "enable_cleanroom": False,
        "max_steps": 180,
        "_label": "arm0_bare_react",
    },
    "v1": {
        "enable_supervisor": False,
        "enable_fullstate_worker": False,
        "fullstate_worker_prompt": False,
        "enable_envstate": False,
        "enable_v1": True,
        "enable_cleanroom": True,
        "max_steps": 12,
        "_label": "armV1_three_role",
    },
    "v3": {
        "enable_supervisor": False, "enable_fullstate_worker": False, "fullstate_worker_prompt": False,
        "enable_envstate": False, "enable_v1": True, "enable_contract_graph": False,
        "enable_dep_graph": True, "enable_dep_emit": True, "enable_graph_scheduler": True,
        "enable_runtime_feedback": True, "enable_runtime_pin": True,
        "enable_service_provision": True,
        "enable_cleanroom": True,
        "max_steps": 12, "_label": "armV3_graph_scheduled",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Repo2Run Table 15 benchmark with this project's native agent."
    )
    parser.add_argument(
        "--dataset",
        default="datasets/repo2run_table15.json",
        help="Standalone Repo2Run dataset JSON path. Defaults to datasets/repo2run_table15.json.",
    )
    parser.add_argument(
        "--output-root",
        default="outputs/repo2run_benchmark",
        help="Directory where per-instance results and summary JSON will be written.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable used to invoke agent.py. Defaults to the current interpreter.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_LLM_MODEL,
        help="Model forwarded to agent.py.",
    )
    parser.add_argument(
        "--base-image",
        default="auto",
        help="Base image forwarded to agent.py.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=100,
        help="Maximum agent steps per repository. Defaults to 100.",
    )
    parser.add_argument(
        "--agent-command-timeout",
        type=int,
        default=1800,
        help="Per-command timeout forwarded to agent.py inside the sandbox. Defaults to 1800.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only run the first N instances after filtering.",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Skip the first N instances after filtering.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help=(
            "Number of instances to evaluate in parallel. Each instance runs as "
            "an independent agent subprocess isolated by its own workplace dir "
            "and a unique docker image tag, so this only bounds the orchestrator. "
            "Defaults to 1 (sequential). Tune to host cores / docker disk / LLM "
            "rate limits."
        ),
    )
    parser.add_argument(
        "--instance-regex",
        default=None,
        help="Only run instances whose instance_id or full_name matches this regex.",
    )
    parser.add_argument(
        "--only-paper-success",
        action="store_true",
        help="Only run instances marked Yes in Table 15.",
    )
    parser.add_argument(
        "--docker-build-timeout",
        type=int,
        default=1800,
        help="Timeout in seconds for a single docker build. Defaults to 1800.",
    )
    parser.add_argument(
        "--test-timeout",
        type=int,
        default=1800,
        help="Timeout in seconds for a single test command execution. Defaults to 1800.",
    )
    parser.add_argument(
        "--dockerfile-repair-rounds",
        type=int,
        default=2,
        help=(
            "Maximum LLM Dockerfile repair rounds after a fresh build/test failure. "
            "Use 0 to disable. Defaults to 2."
        ),
    )
    parser.add_argument(
        "--keep-docker-artifacts",
        action="store_true",
        help="Keep built docker images for inspection instead of removing them after evaluation.",
    )
    parser.set_defaults(enable_observation_compression=True)
    parser.add_argument(
        "--enable-observation-compression",
        action="store_true",
        dest="enable_observation_compression",
        help="Enable AgentDiet-style observation compression during benchmark runs (default: enabled).",
    )
    parser.add_argument(
        "--disable-observation-compression",
        action="store_false",
        dest="enable_observation_compression",
        help="Disable AgentDiet-style observation compression during benchmark runs.",
    )
    parser.add_argument(
        "--enable-long-term-memory",
        action="store_true",
        help="Forward --enable-long-term-memory to agent.py.",
    )
    parser.add_argument(
        "--memory-path",
        default=None,
        help="Optional JSONL long-term memory path forwarded to agent.py.",
    )
    parser.add_argument(
        "--memory-embedding-model",
        default=DEFAULT_MEMORY_EMBEDDING_MODEL,
        help="Embedding model forwarded to agent.py when long-term memory is enabled.",
    )
    parser.add_argument(
        "--keep-container",
        action="store_true",
        help="Forward --keep-container to agent.py.",
    )
    parser.add_argument(
        "--reuse-existing-workplace",
        action="store_true",
        help="If a workplace already has agent_run_summary.json, skip agent setup and reuse that run.",
    )
    parser.add_argument(
        "--force-resynthesize",
        action="store_true",
        help="When reusing an existing workplace, rerun setup-log summary and recipe synthesis even if Dockerfile already exists.",
    )

    # ---------------------------------------------------------------------------
    # Ablation-experiment arm selector (§3.1, §9.1 of the experiment design spec)
    # --arm maps to a canonical flag set + --steps override so both arms can be
    # launched without memorising per-arm flag combinations.
    #   Arm 0: bare ReAct (no EnvState flags), --steps 180
    #   Arm v1: three-role orchestrator (Planner/BuildAgent/Maintainer), --steps 12
    # Arms A/B/C are retired.  When --arm is absent the individual flags below
    # are used directly (back-compat).
    # ---------------------------------------------------------------------------
    parser.add_argument(
        "--arm",
        choices=["0", "v1", "v3"],
        default=None,
        help=(
            "Ablation arm shorthand. "
            "0=bare ReAct (no EnvState flags, --steps 180); "
            "v1=three-role orchestrator Planner/BuildAgent/Maintainer "
            "(--enable-v1 --enable-cleanroom --steps 12); "
            "v3=full graph-scheduled stack (--enable-graph-scheduler --enable-runtime-pin "
            "--enable-service-provision; --steps 12). "
            "Overrides the individual --enable-* flags and --max-steps when set. "
            "Outputs land under <output-root>/arm{0,v1,v3}_<label>/."
        ),
    )

    # Individual pass-through flags (used directly when --arm is absent, or
    # set implicitly by --arm).
    parser.add_argument(
        "--enable-supervisor",
        action="store_true",
        help="[DEPRECATED — Arms B/C retired] Forward --enable-supervisor to agent.py.",
    )
    parser.add_argument(
        "--enable-fullstate-worker",
        action="store_true",
        help="[DEPRECATED — Arm A retired] Forward --enable-fullstate-worker to agent.py.",
    )
    parser.add_argument(
        "--fullstate-worker-prompt",
        action="store_true",
        help="[DEPRECATED — Arm C retired] Forward --fullstate-worker-prompt to agent.py.",
    )
    parser.add_argument(
        "--enable-envstate",
        action="store_true",
        help="Forward --enable-envstate to agent.py.",
    )
    parser.add_argument(
        "--enable-cleanroom",
        action="store_true",
        help="Forward --enable-cleanroom to agent.py.",
    )
    parser.add_argument(
        "--enable-v1",
        action="store_true",
        help="Use the v1 three-role orchestrator (Planner/BuildAgent/Maintainer).",
    )

    args = parser.parse_args()

    # Apply --arm presets (overrides individual flags + max_steps).
    if args.arm is not None:
        preset = _ARM_PRESETS[args.arm]
        for key, value in preset.items():
            if not key.startswith("_"):
                setattr(args, key, value)
        # Embed arm label in output_root so arms never collide.
        args.output_root = str(Path(args.output_root) / preset["_label"])
        args._arm_label = preset["_label"]
    else:
        args._arm_label = None

    return args


def run_instances(
    instances: list[dict[str, Any]],
    *,
    concurrency: int,
    worker: Callable[[int, dict[str, Any]], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Run ``worker(position, instance)`` for every instance and return the
    payloads in submit (dataset) order.

    At most ``concurrency`` instances are in flight at once. Each instance is
    fully isolated on disk (its own workplace / result / artifact dirs) and uses
    a unique docker image tag, so they parallelise safely. The work is
    subprocess/IO-bound — ``agent.py`` runs as a child process and docker
    build/test calls block on IO — so threads are the right tool (the GIL is
    released across subprocess and file IO), mirroring the RAT runner's
    ThreadPoolExecutor scheduler.

    A worker exception propagates (same fail behaviour as the sequential path).
    """
    total = len(instances)
    if concurrency <= 1 or total <= 1:
        return [
            worker(position, instance)
            for position, instance in enumerate(instances, start=1)
        ]

    results: list[Optional[dict[str, Any]]] = [None] * total
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        future_to_index = {
            pool.submit(worker, position, instance): position - 1
            for position, instance in enumerate(instances, start=1)
        }
        for future in as_completed(future_to_index):
            results[future_to_index[future]] = future.result()
    return [item for item in results if item is not None]


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parent
    dataset_path = (repo_root / args.dataset).resolve()
    output_root = (repo_root / args.output_root).resolve()
    results_dir = output_root / "results"
    workplaces_dir = output_root / "workplaces"
    eval_artifacts_dir = output_root / "eval_artifacts"

    dataset = load_repo2run_dataset(dataset_path)
    instances = list(dataset["instances"])

    if args.only_paper_success:
        instances = [instance for instance in instances if instance.get("paper_build_success")]

    if args.instance_regex:
        matcher = re.compile(args.instance_regex)
        instances = [
            instance
            for instance in instances
            if matcher.search(instance.get("instance_id", "")) or matcher.search(instance.get("full_name", ""))
        ]

    if args.offset:
        instances = instances[args.offset :]
    if args.limit is not None:
        instances = instances[: args.limit]

    output_root.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    workplaces_dir.mkdir(parents=True, exist_ok=True)
    eval_artifacts_dir.mkdir(parents=True, exist_ok=True)

    print(f"Running Repo2Run dataset: {dataset_path}")
    print(f"Selected instances: {len(instances)}")

    python_executable = resolve_executable_path(args.python)

    def _process(position: int, instance: dict[str, Any]) -> dict[str, Any]:
        instance_id = instance["instance_id"]
        safe_instance_id = sanitize_name(instance_id)
        workplace = workplaces_dir / safe_instance_id
        result_path = results_dir / f"{safe_instance_id}.json"
        artifact_dir = eval_artifacts_dir / safe_instance_id
        artifact_dir.mkdir(parents=True, exist_ok=True)

        print(f"[{position}/{len(instances)}] {instance['full_name']} @ {instance['sha']}")

        agent_command = build_agent_command(
            python_executable=python_executable,
            repo_root=repo_root,
            instance=instance,
            workplace=workplace,
            args=args,
        )

        run_summary_path = workplace / "agent_run_summary.json"
        agent_dockerfile_path = workplace / "Dockerfile"
        reused_existing_workplace = bool(
            args.reuse_existing_workplace and run_summary_path.exists()
        )
        resynthesis = None

        if reused_existing_workplace:
            print(f"[Reuse] Skipping agent setup and reusing workplace: {workplace}")
            agent_run = {
                "command": agent_command,
                "command_shell": shlex.join(agent_command),
                "cwd": str(repo_root),
                "returncode": 0,
                "started_at": None,
                "finished_at": None,
                "duration_seconds": 0.0,
                "stdout": "[reused existing workplace]",
                "stderr": "",
                "timed_out": False,
                "timeout_seconds": None,
            }
            if args.force_resynthesize or not agent_dockerfile_path.exists():
                resynthesis = resynthesize_dockerfile_from_existing_workplace(
                    workplace=workplace,
                    model=args.model,
                )
        else:
            agent_run = run_command(agent_command, cwd=repo_root, env=os.environ.copy())

        run_summary = load_json(run_summary_path)
        raw_agent_dockerfile_text = (
            agent_dockerfile_path.read_text(encoding="utf-8")
            if agent_dockerfile_path.exists()
            else None
        )
        agent_dockerfile_usable, agent_dockerfile_ignored_reason = should_use_agent_dockerfile(
            agent_run,
            reused_existing_workplace=reused_existing_workplace,
            run_summary=run_summary,
        )
        agent_dockerfile_text = raw_agent_dockerfile_text if agent_dockerfile_usable else None

        eval_dockerfile_path = artifact_dir / "Dockerfile.eval"
        docker_build = None
        test_execution = None
        docker_cleanup = None
        eval_context_preparation = None
        eval_dockerignore_test_artifacts = None
        eval_build_context_path = None
        dockerfile_validation_attempts: list[dict[str, Any]] = []
        dockerfile_repair_rounds: list[dict[str, Any]] = []
        workdir = "/app"
        verification_source = None
        runtime_commands: list[str] = []
        test_commands: list[str] = []
        image_tag = build_docker_image_tag(instance_id)
        docker_platform = resolve_benchmark_platform(workplace, run_summary)
        pip_constraints = collect_observed_pip_install_constraints(workplace, run_summary)

        if agent_dockerfile_text:
            eval_context_preparation = prepare_eval_build_context(
                workplace,
                artifact_dir / "build_context",
                base_commit=instance.get("base_commit") or instance.get("sha"),
                cwd=repo_root,
            )
            eval_build_context_path = Path(
                eval_context_preparation.get("path") or str(workplace)
            )
            workdir = infer_workdir_from_dockerfile(agent_dockerfile_text)
            eval_dockerfile_text = normalize_eval_dockerfile_for_replay(
                render_eval_dockerfile(agent_dockerfile_text),
                pip_constraints=pip_constraints,
            )
            runtime_commands, test_commands, verification_source = (
                derive_repo2run_collect_commands(workplace, run_summary)
            )
            eval_dockerignore_test_artifacts = ensure_eval_dockerignore_includes_test_artifacts(
                eval_build_context_path,
                test_commands=test_commands,
                run_summary=run_summary,
            )
            current_eval_dockerfile_text = eval_dockerfile_text
            repair_client = None
            max_repair_rounds = max(0, args.dockerfile_repair_rounds)

            for attempt_index in range(max_repair_rounds + 1):
                workdir = infer_workdir_from_dockerfile(current_eval_dockerfile_text)
                eval_dockerfile_path.write_text(current_eval_dockerfile_text, encoding="utf-8")

                docker_build_command = ["docker", "build"]
                if docker_platform:
                    docker_build_command.extend(["--platform", docker_platform])
                docker_build_command.extend(
                    [
                        "-f",
                        str(eval_dockerfile_path),
                        "-t",
                        image_tag,
                        str(eval_build_context_path),
                    ]
                )
                docker_build = run_command(
                    docker_build_command,
                    cwd=repo_root,
                    env=os.environ.copy(),
                    timeout_seconds=args.docker_build_timeout,
                )

                test_execution = None
                if docker_build["returncode"] == 0 and not docker_build.get("timed_out"):
                    test_execution = evaluate_built_image(
                        image_tag=image_tag,
                        workdir=workdir,
                        runtime_commands=runtime_commands,
                        test_commands=test_commands,
                        cwd=repo_root,
                        timeout_seconds=args.test_timeout,
                        workspace_root=eval_build_context_path,
                        docker_platform=docker_platform,
                        run_summary=run_summary,
                    )

                attempt_success = bool(
                    docker_build
                    and docker_build["returncode"] == 0
                    and not docker_build.get("timed_out")
                    and test_execution
                    and test_execution["all_test_commands_effective"]
                )
                dockerfile_validation_attempts.append(
                    {
                        "attempt": attempt_index,
                        "dockerfile_path": str(eval_dockerfile_path),
                        "docker_build": docker_build,
                        "test_execution": test_execution,
                        "success": attempt_success,
                    }
                )

                if attempt_success:
                    break
                if attempt_index < max_repair_rounds and test_execution:
                    repaired_text, installed_requirements = repair_dockerfile_for_missing_python_modules(
                        current_eval_dockerfile_text,
                        test_execution,
                        eval_build_context_path,
                    )
                    if repaired_text != current_eval_dockerfile_text:
                        dockerfile_repair_rounds.append(
                            {
                                "round": attempt_index + 1,
                                "source": "deterministic_missing_python_modules",
                                "error": None,
                                "usage": {
                                    "input_tokens": 0,
                                    "output_tokens": 0,
                                    "total_tokens": 0,
                                },
                                "raw_content": "",
                                "dockerfile_text": repaired_text,
                                "rationale": (
                                    "Installed missing Python modules reported by pytest collection: "
                                    + ", ".join(installed_requirements)
                                ),
                                "confidence": "high",
                                "log_path": None,
                            }
                        )
                        current_eval_dockerfile_text = normalize_eval_dockerfile_for_replay(
                            repaired_text,
                            pip_constraints=pip_constraints,
                        )
                        continue
                if attempt_index >= max_repair_rounds:
                    break
                if docker_build_failed_due_to_unavailable_daemon(docker_build):
                    break

                repair_input = build_dockerfile_repair_input(
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
                    if repair_client is None:
                        repair_client = create_openai_client_from_env()
                    repair_result = repair_dockerfile_with_llm(
                        client=repair_client,
                        model=args.model,
                        repair_input=repair_input,
                        artifact_dir=artifact_dir,
                        round_index=attempt_index + 1,
                    )
                except Exception as exc:
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
                dockerfile_repair_rounds.append(repair_result)
                repaired_text = repair_result.get("dockerfile_text")
                if not repaired_text:
                    break
                current_eval_dockerfile_text = normalize_eval_dockerfile_for_replay(
                    repaired_text,
                    pip_constraints=pip_constraints,
                )

            if not args.keep_docker_artifacts:
                docker_cleanup = remove_docker_image(image_tag, cwd=repo_root)

        dockerfile_generation_success = bool(
            docker_build and docker_build["returncode"] == 0 and not docker_build.get("timed_out")
        )
        environment_build_success = bool(
            dockerfile_generation_success
            and test_execution
            and test_execution["all_test_commands_effective"]
        )
        paper_alignment = compute_paper_alignment(
            expected_success=bool(instance.get("paper_build_success")),
            observed_success=environment_build_success,
        )
        goal_status = "success" if environment_build_success else "needs_repair"
        execution_status = compute_execution_status(
            agent_run=agent_run,
            dockerfile_present=agent_dockerfile_text is not None,
            docker_build_success=dockerfile_generation_success,
            environment_build_success=environment_build_success,
        )
        payload = {
            "dataset_entry": instance,
            "agent_run": agent_run,
            "reused_existing_workplace": reused_existing_workplace,
            "resynthesis": resynthesis,
            "result_json_path": str(result_path),
            "run_summary_path": str(run_summary_path),
            "run_summary": run_summary,
            "agent_claimed_success": bool((run_summary or {}).get("configuration_success")),
            "agent_dockerfile_path": str(agent_dockerfile_path),
            "agent_dockerfile_present": raw_agent_dockerfile_text is not None,
            "agent_dockerfile_usable": agent_dockerfile_text is not None,
            "agent_dockerfile_ignored_reason": agent_dockerfile_ignored_reason,
            "eval_dockerfile_path": str(eval_dockerfile_path),
            "eval_build_context_path": str(eval_build_context_path) if eval_build_context_path else None,
            "eval_context_preparation": eval_context_preparation,
            "eval_dockerignore_test_artifacts": eval_dockerignore_test_artifacts,
            "eval_workdir": workdir,
            "docker_platform": docker_platform,
            "verification_command_source": verification_source,
            "runtime_preparation_commands": runtime_commands,
            "test_commands": test_commands,
            "docker_build": docker_build,
            "test_execution": test_execution,
            "dockerfile_validation_attempts": dockerfile_validation_attempts,
            "dockerfile_repair_rounds": dockerfile_repair_rounds,
            "docker_cleanup": docker_cleanup,
            "dockerfile_generation_success": dockerfile_generation_success,
            "environment_build_success": environment_build_success,
            "paper_build_success": bool(instance.get("paper_build_success")),
            "paper_alignment": paper_alignment,
            "goal_status": goal_status,
            "needs_repair": not environment_build_success,
            "execution_status": execution_status,
        }
        payload["debug_artifacts"] = write_instance_debug_artifacts(
            artifact_dir=artifact_dir,
            instance=instance,
            payload=payload,
        )
        write_json(result_path, payload)
        return payload

    worker_count = max(1, int(getattr(args, "concurrency", 1) or 1))
    per_instance_results = run_instances(
        instances, concurrency=worker_count, worker=_process
    )
    execution_status_counter = Counter(
        item["execution_status"] for item in per_instance_results
    )
    paper_alignment_counter = Counter(
        item["paper_alignment"] for item in per_instance_results
    )

    dgsr_successes = sum(
        1 for item in per_instance_results if item["dockerfile_generation_success"]
    )
    ebsr_successes = sum(
        1 for item in per_instance_results if item["environment_build_success"]
    )

    summary = {
        "benchmark_name": dataset.get("benchmark_name", "Repo2Run Table 15"),
        "dataset_path": str(dataset_path),
        "output_root": str(output_root),
        "selected_instances": len(instances),
        "metrics": {
            "DGSR": {
                "success_count": dgsr_successes,
                "total": len(instances),
                "rate": round(dgsr_successes / len(instances), 4) if instances else 0.0,
            },
            "EBSR": {
                "success_count": ebsr_successes,
                "total": len(instances),
                "rate": round(ebsr_successes / len(instances), 4) if instances else 0.0,
            },
        },
        "execution_status_counts": dict(sorted(execution_status_counter.items())),
        "paper_alignment_counts": dict(sorted(paper_alignment_counter.items())),
        "goal_status_counts": {
            "success": sum(1 for item in per_instance_results if item["environment_build_success"]),
            "needs_repair": sum(1 for item in per_instance_results if not item["environment_build_success"]),
        },
        "matched_against_paper": sum(
            1 for item in per_instance_results if item["paper_alignment"] in {"matched_success", "matched_failure"}
        ),
        "paper_success_count": sum(
            1 for instance in instances if instance.get("paper_build_success")
        ),
        "paper_failure_count": sum(
            1 for instance in instances if not instance.get("paper_build_success")
        ),
        "results": [
            {
                "instance_id": item["dataset_entry"]["instance_id"],
                "full_name": item["dataset_entry"]["full_name"],
                "sha": item["dataset_entry"]["sha"],
                "execution_status": item["execution_status"],
                "paper_alignment": item["paper_alignment"],
                "goal_status": item["goal_status"],
                "needs_repair": item["needs_repair"],
                "dockerfile_generation_success": item["dockerfile_generation_success"],
                "environment_build_success": item["environment_build_success"],
                "paper_build_success": item["paper_build_success"],
                "benchmark_log_path": item.get("debug_artifacts", {}).get("benchmark_log_path"),
                "result_json": str(results_dir / f"{sanitize_name(item['dataset_entry']['instance_id'])}.json"),
            }
            for item in per_instance_results
        ],
    }
    write_json(output_root / "summary.json", summary)

    print(json.dumps(summary["metrics"], ensure_ascii=False, indent=2))
    print(json.dumps(summary["paper_alignment_counts"], ensure_ascii=False, indent=2))
    print(f"Summary written to {output_root / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
