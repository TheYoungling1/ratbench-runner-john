"""
Multi-Docker-Eval Benchmark Adapter
将 DockerAgent 适配到 Multi-Docker-Eval 评估标准

输入格式 (JSONL):
{
    "instance_id": "repo_name__issue_number",
    "repo_url": "https://github.com/user/repo.git", 
    "base_commit": "commit_hash",
    "problem_statement": "Issue description...",
    "patch": "diff content...",
    "test_patch": "test diff content...",
    "language": "python"
}

输出格式 (docker_res JSON):
{
    "instance_id": "repo_name__issue_number",
    "dockerfile": "FROM python:3.10\nRUN...",
    "test_script": "#!/bin/bash\npython -m pytest...",
    "build_success": true,
    "test_success": true,
    "logs": {...}
}
"""

import os
import json
import argparse
import base64
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Any, List, Optional, Tuple
from agent import DockerAgent
from src.constants import DEFAULT_LLM_MODEL, DEFAULT_MEMORY_EMBEDDING_MODEL
from src.synthesizer import (
    build_dockerfile_apt_bootstrap_run_instructions,
    is_generated_apt_bootstrap_run_instruction,
    quote_shell_sensitive_package_specs,
)


RECIPE_REPAIR_SYSTEM_PROMPT = """You are a build-artifact repair module for an environment setup agent.

The setup agent already found a working interactive container state. Your job is to repair the structured build recipe so a fresh Docker image can reproduce that state and pass the benchmark evaluation.

Return JSON only. Do not write Markdown.

Rules:
1. Repair the recipe fields, not the source patch or benchmark test patch.
2. Keep commands reproducible from a clean Docker build after the repository is cloned and checked out.
3. Put persistent setup in `build_commands`.
4. Put commands that must run after the evaluator's test patch is baked into the image in `post_test_patch_commands`.
5. Put daemon starts, database/service initialization, and commands that must run immediately before tests in `runtime_preparation_commands`.
6. Put final benchmark-facing commands in `test_commands`.
7. Do not include read-only diagnostics, failed commands, or broad fallback test commands that are not needed once a target-covering command exists.
8. If the failure is caused by an incomplete recipe, add the missing setup command rather than weakening the test.
9. If the failure is caused by stale/wrong test selection, adjust `test_commands` to the benchmark changed test target or native wrapper that definitely runs it.
10. Preserve commands from the current recipe that are still necessary.
11. Use `project_config_context` as the highest-priority dependency and test-runner evidence. Prefer pinned versions and commands from tox/CI/lock/config files over broad latest-version installs.
12. If logs show framework/plugin/runtime incompatibility, repair dependency pins first. Do not diagnose a path mismatch unless the logs or config prove that different source trees are being used.
13. The artifact renderer normalizes `/app` to `/testbed`; do not change paths as the main repair unless that is the only concrete failure.
14. If tests must exercise source-patched project code, install or load the local project. Do not install the same project from PyPI in a way that shadows the patched local source.
15. Treat exact dependency pins from project config as higher-priority evidence than broad ranges or latest-version installs. Do not mix an old framework pin with unpinned companion packages that can upgrade it; keep compatible tox/CI dependency groups together.

Required JSON keys:
`build_commands`, `post_test_patch_commands`, `runtime_preparation_commands`, `test_commands`, `excluded_commands`, `rationale`, `confidence`.

`confidence` must be one of: "high", "medium", "low".
"""


class MultiDockerEvalAdapter:
    """适配器：将 DockerAgent 输出转换为 Multi-Docker-Eval 评估格式"""
    _AGENT_ROOT_WORKDIR_PATTERN = re.compile(r"(?<![A-Za-z0-9_.-])/app(?=$|[^A-Za-z0-9_.-])")
    _DOCKERFILE_INSTRUCTION_PATTERN = re.compile(
        r"^\s*(FROM|RUN|CMD|LABEL|MAINTAINER|EXPOSE|ENV|ADD|COPY|ENTRYPOINT|"
        r"VOLUME|USER|WORKDIR|ARG|ONBUILD|STOPSIGNAL|HEALTHCHECK|SHELL)\b",
        re.IGNORECASE,
    )
    _HEREDOC_DELIMITER_PATTERN = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
    _REPAIR_CONTEXT_FILE_CANDIDATES = (
        "tox.ini",
        "setup.cfg",
        "setup.py",
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "dev-requirements.txt",
        "test-requirements.txt",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "composer.json",
        "composer.lock",
        "Gemfile",
        "Gemfile.lock",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "gradle.properties",
        "go.mod",
        "go.sum",
        "Cargo.toml",
        "Cargo.lock",
        "Makefile",
        ".travis.yml",
        ".circleci/config.yml",
    )
    _REPAIR_CONTEXT_GLOB_CANDIDATES = (
        ".github/workflows/*.yml",
        ".github/workflows/*.yaml",
    )
    _REPAIR_CONTEXT_MAX_FILE_CHARS = 6000
    _REPAIR_CONTEXT_MAX_TOTAL_CHARS = 24000
    
    def __init__(self, output_dir: str = "./multi_docker_eval_output"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.repo_root = Path(__file__).resolve().parent
        self._last_test_command_source = None
        self._last_runtime_preparation_source = None
        self._last_post_test_patch_source = None
        self._last_source_patch_rebuild_commands = []
        self._last_deferred_post_test_patch_commands = []
        self._last_filtered_runtime_preparation_commands = []
        self._last_filtered_test_commands = []
        self._last_refined_test_commands = []
        self._last_dropped_broad_test_commands = []
        self._last_eval_runtime_preparation_commands = []
        self._last_project_dependency_pin_rewrites = []

    def build_benchmark_evaluation_target(self, test_patch: str) -> Dict[str, Any]:
        """Extract non-solution benchmark hints that help the agent choose final tests."""
        changed_test_files = self._extract_changed_test_files(test_patch)
        framework_clues = self._extract_test_framework_clues(test_patch)
        changed_test_targets = self._extract_changed_test_targets(test_patch)
        if not changed_test_files and not framework_clues and not changed_test_targets:
            return {}

        return {
            "changed_test_files": changed_test_files,
            "changed_test_targets": changed_test_targets,
            "test_framework_clues": framework_clues,
        }

    def _extract_changed_test_files(self, test_patch: str) -> List[str]:
        changed_files = []
        seen = set()

        for raw_path in re.findall(r"^diff --git a/(.*?) b/(.*?)$", test_patch or "", re.MULTILINE):
            for path in raw_path:
                normalized = self._normalize_patch_path(path)
                if self._looks_like_test_file(normalized) and normalized not in seen:
                    seen.add(normalized)
                    changed_files.append(normalized)

        for marker in re.findall(r"^(?:---|\+\+\+) [ab]/([^\t\n\r]+)", test_patch or "", re.MULTILINE):
            normalized = self._normalize_patch_path(marker)
            if self._looks_like_test_file(normalized) and normalized not in seen:
                seen.add(normalized)
                changed_files.append(normalized)

        return changed_files[:20]

    def _normalize_patch_path(self, path: str) -> str:
        path = (path or "").strip()
        if path in {"/dev/null", "dev/null"}:
            return ""
        return path.lstrip("./")

    def _looks_like_test_file(self, path: str) -> bool:
        if not path:
            return False
        lowered = path.lower()
        path_parts = lowered.split("/")
        filename = path_parts[-1]

        if any(part in {"test", "tests", "spec", "specs", "__tests__"} for part in path_parts[:-1]):
            return True

        test_name_markers = (
            "test_",
            "_test.",
            ".test.",
            ".spec.",
            "_spec.",
            "spec_",
        )
        if any(marker in filename for marker in test_name_markers):
            return True

        test_extensions = (
            ".t",
            ".t.py",
            ".t.js",
            ".t.ts",
            ".feature",
        )
        return filename.endswith(test_extensions)

    def _extract_test_framework_clues(self, test_patch: str) -> List[str]:
        patch = test_patch or ""
        clues = []
        framework_patterns = [
            (r"\bpytest\b|def test_|@pytest\.", "pytest"),
            (r"\bunittest\b|TestCase|assert[A-Z]\w*\(", "python unittest"),
            (r"\bsimpletap\b|TAPTestRunner|1\.\.\d+|assertRegexpMatches", "TAP/simpletap"),
            (r"\bJest\b|\bdescribe\(|\bit\(|\btest\(|expect\(", "Jest/Vitest-style JS tests"),
            (r"\bmocha\b|\bchai\b", "Mocha/Chai"),
            (r"\bJUnit\b|@Test\b|assertEquals\(", "JUnit"),
            (r"\bTestNG\b", "TestNG"),
            (r"\brspec\b|RSpec\.describe|describe ['\"]", "RSpec"),
            (r"\bphpunit\b|PHPUnit|extends TestCase", "PHPUnit"),
            (r"\bgo test\b|func Test[A-Z]", "Go testing"),
            (r"\bcargo test\b|#\[test\]", "Rust cargo test"),
            (r"\bctest\b|add_test\(|add_custom_target\s*\(\s*test", "CMake/CTest"),
            (r"\bbats\b|@test\s+['\"]", "Bats"),
            (r"\bTAP\b|Test Anything Protocol", "TAP"),
        ]
        for pattern, label in framework_patterns:
            if re.search(pattern, patch, flags=re.IGNORECASE):
                clues.append(label)

        return list(dict.fromkeys(clues))[:12]

    def _extract_changed_test_targets(self, test_patch: str, source_root: Optional[str] = None) -> List[str]:
        """Extract precise test nodes introduced by the benchmark test patch."""
        targets = []
        seen = set()
        current_file = ""
        current_class = ""
        current_class_indent = None
        old_line = None
        new_line = None
        python_triple_string = None
        pending_changed_decorator = False
        active_added_test_indent = None

        def add_target(test_name: str, indent: int, line_hint: Optional[int]) -> None:
            target_class = ""
            if indent > 0:
                if (
                    current_class
                    and current_class_indent is not None
                    and indent > current_class_indent
                ):
                    target_class = current_class
                else:
                    target_class = self._find_enclosing_python_class(
                        source_root,
                        current_file,
                        line_hint,
                        indent,
                    )
                if not target_class:
                    return
            target = (
                f"{current_file}::{target_class}::{test_name}"
                if target_class
                else f"{current_file}::{test_name}"
            )
            if target not in seen:
                seen.add(target)
                targets.append(target)

        for line in (test_patch or "").splitlines():
            file_match = re.match(r"^\+\+\+ b/([^\t\r\n]+)", line)
            if file_match:
                current_file = self._normalize_patch_path(file_match.group(1))
                current_class = ""
                current_class_indent = None
                old_line = None
                new_line = None
                python_triple_string = None
                pending_changed_decorator = False
                active_added_test_indent = None
                continue

            if line.startswith(("diff --git ", "--- ")):
                continue

            if not current_file or not self._looks_like_test_file(current_file):
                continue

            hunk_match = re.match(
                r"^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@(.*)$",
                line,
            )
            if hunk_match:
                old_line = int(hunk_match.group(1))
                new_line = int(hunk_match.group(2))
                hunk_context = hunk_match.group(3).strip()
                python_triple_string = None
                pending_changed_decorator = False
                active_added_test_indent = None
                hunk_class_match = re.match(r"class\s+([A-Za-z_][A-Za-z0-9_]*)\b", hunk_context)
                if hunk_class_match:
                    current_class = hunk_class_match.group(1)
                    current_class_indent = 0
                continue

            marker = line[:1]
            body = line[1:] if marker in {" ", "+", "-"} else line
            inside_python_string = python_triple_string is not None

            class_match = re.match(r"^(\s*)class\s+([A-Za-z_][A-Za-z0-9_]*)\b", body)
            if marker in {" ", "+"} and not inside_python_string and class_match:
                current_class_indent = len(class_match.group(1))
                current_class = class_match.group(2)

            function_match = re.match(
                r"^(\s*)def\s+(test_[A-Za-z0-9_]+)\s*\(([^)]*)",
                body,
            )
            any_function_match = re.match(r"^(\s*)def\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", body)
            if marker in {" ", "+"} and not inside_python_string and function_match:
                indent = len(function_match.group(1))
                test_name = function_match.group(2)
                if marker == "+" or pending_changed_decorator:
                    add_target(test_name, indent, old_line)
                pending_changed_decorator = False
                if marker == "+":
                    active_added_test_indent = indent
            elif marker == "+" and active_added_test_indent is not None:
                indent = len(body) - len(body.lstrip(" "))
                if body.strip() and indent <= active_added_test_indent:
                    active_added_test_indent = None
            elif marker in {" ", "+"} and not inside_python_string and any_function_match:
                pending_changed_decorator = False

            if marker in {"+", "-"} and not inside_python_string:
                stripped = body.strip()
                if stripped.startswith("@"):
                    if not stripped.startswith("@pytest.fixture"):
                        pending_changed_decorator = True
                elif (
                    stripped
                    and marker == "-"
                    and source_root
                    and old_line is not None
                ):
                    existing_target = self._find_enclosing_python_test(
                        source_root,
                        current_file,
                        old_line,
                    )
                    if existing_target and existing_target not in seen:
                        seen.add(existing_target)
                        targets.append(existing_target)

            if marker in {" ", "+"}:
                python_triple_string = self._update_python_triple_string_state(
                    body,
                    python_triple_string,
                )

            if marker == " ":
                if old_line is not None:
                    old_line += 1
                if new_line is not None:
                    new_line += 1
            elif marker == "+":
                if new_line is not None:
                    new_line += 1
            elif marker == "-":
                if old_line is not None:
                    old_line += 1

        return targets[:20]

    def _update_python_triple_string_state(
        self,
        line: str,
        current_delimiter: Optional[str],
    ) -> Optional[str]:
        """Track whether patch parsing is inside a Python triple-quoted string."""
        matches = []
        for delimiter in ('"""', "'''"):
            for match in re.finditer(re.escape(delimiter), line or ""):
                index = match.start()
                if index > 0 and line[index - 1] == "\\":
                    continue
                matches.append((index, delimiter))
        delimiter = current_delimiter
        for _, token in sorted(matches, key=lambda item: item[0]):
            if delimiter:
                if token == delimiter:
                    delimiter = None
            else:
                delimiter = token
        return delimiter

    def _find_enclosing_python_test(
        self,
        source_root: Optional[str],
        file_path: str,
        old_line: Optional[int],
    ) -> str:
        if not source_root or not file_path or old_line is None:
            return ""

        source_path = Path(source_root) / file_path
        if not source_path.exists():
            return ""

        try:
            lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""

        limit = min(max(old_line - 1, 0), len(lines))
        for index in range(limit - 1, -1, -1):
            line = lines[index]
            function_match = re.match(r"^(\s*)def\s+(test_[A-Za-z0-9_]+)\s*\(", line)
            if not function_match:
                continue
            indent = len(function_match.group(1))
            test_name = function_match.group(2)
            target_class = self._find_enclosing_python_class(
                source_root,
                file_path,
                index + 1,
                indent,
            )
            return (
                f"{file_path}::{target_class}::{test_name}"
                if target_class
                else f"{file_path}::{test_name}"
            )
        return ""

    def _find_enclosing_python_class(
        self,
        source_root: Optional[str],
        file_path: str,
        old_line: Optional[int],
        function_indent: int,
    ) -> str:
        if not source_root or not file_path or old_line is None or function_indent <= 0:
            return ""

        source_path = Path(source_root) / file_path
        if not source_path.exists():
            return ""

        try:
            lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""

        limit = min(max(old_line - 1, 0), len(lines))
        for index in range(limit - 1, -1, -1):
            line = lines[index]
            class_match = re.match(r"^(\s*)class\s+([A-Za-z_][A-Za-z0-9_]*)\b", line)
            if class_match and len(class_match.group(1)) < function_indent:
                return class_match.group(2)
            top_level_match = re.match(r"^(def|async\s+def)\s+", line)
            if top_level_match:
                return ""
        return ""

    def _build_eval_dockerfile(
        self,
        base_image_line: str,
        repo_url: str,
        base_commit: Optional[str],
        processed_instructions: List[str],
    ) -> str:
        filtered_instructions = [
            instr for instr in processed_instructions
            if not is_generated_apt_bootstrap_run_instruction(instr)
        ]
        has_heredoc = any("<<" in instr for instr in filtered_instructions)
        syntax_directive = "# syntax=docker/dockerfile:1\n" if has_heredoc else ""
        checkout_line = (
            f"RUN cd /testbed && git checkout {base_commit}"
            if base_commit
            else "# No base commit provided; using repository default branch HEAD"
        )
        apt_bootstrap_lines = build_dockerfile_apt_bootstrap_run_instructions()
        apt_bootstrap_block = "\n".join(apt_bootstrap_lines) if apt_bootstrap_lines else ""
        dependency_bootstrap_lines = self._build_eval_dependency_bootstrap_instructions(
            base_image_line,
            filtered_instructions,
        )
        dependency_bootstrap_block = (
            "\n".join(dependency_bootstrap_lines)
            if dependency_bootstrap_lines
            else "# No extra dependency bootstrap needed"
        )
        post_setup_lines = self._build_eval_post_setup_instructions(filtered_instructions)
        post_setup_block = (
            "\n".join(post_setup_lines)
            if post_setup_lines
            else "# No post-setup compatibility helpers needed"
        )
        setup_block = (
            "\n".join(filtered_instructions)
            if filtered_instructions
            else "# No additional setup instructions from agent"
        )
        return f"""{syntax_directive}{base_image_line}
WORKDIR /testbed

# Configure apt reliability for eval image builds
{apt_bootstrap_block}

# Install git for cloning
RUN command -v git >/dev/null 2>&1 || (apt-get update && apt-get install -y git)

# Dependency-manager helpers inferred from verified setup
{dependency_bootstrap_block}

# Clone repository and checkout base commit
RUN git clone {repo_url} /testbed
{checkout_line}

# Agent's verified setup instructions
{setup_block}

# Post-setup compatibility helpers inferred from verified setup
{post_setup_block}
"""

    def _build_eval_dependency_bootstrap_instructions(
        self,
        base_image_line: str,
        processed_instructions: List[str],
    ) -> List[str]:
        """Add small package-manager helpers when the verified setup requires them."""
        instruction_blob = "\n".join(processed_instructions or []).lower()
        if not self._composer_setup_needs_archive_extractor(instruction_blob):
            return []

        base_image = (base_image_line or "").lower()
        if "alpine" in base_image:
            return ["RUN apk add --no-cache unzip"]
        return ["RUN apt-get update && apt-get install -y unzip"]

    def _composer_setup_needs_archive_extractor(self, instruction_blob: str) -> bool:
        """Composer source installs often fail in fresh PHP images without unzip/7z."""
        lowered = instruction_blob or ""
        if "composer install" not in lowered and "composer require" not in lowered:
            return False

        archive_extractors = (
            " unzip",
            " unzip ",
            " unzip\n",
            "7z",
            "p7zip",
            "libzip",
            "php-zip",
            "docker-php-ext-install zip",
        )
        return not any(marker in lowered for marker in archive_extractors)

    def _build_eval_post_setup_instructions(
        self,
        processed_instructions: List[str],
    ) -> List[str]:
        instruction_blob = "\n".join(processed_instructions or []).lower()
        if self._pytest_setup_needs_plugin_cleanup(instruction_blob):
            return ["RUN python -m pip uninstall -y pytest-xdist pytest-forked || true"]
        return []

    def _pytest_setup_needs_plugin_cleanup(self, instruction_blob: str) -> bool:
        """Old pytest pins are incompatible with newer xdist/forked entrypoints."""
        lowered = instruction_blob or ""
        installs_testing_extra = '".[testing]"' in lowered or "'.[testing]'" in lowered
        installs_unpinned_xdist = bool(
            re.search(r"pytest-xdist(?:\s|$|[\"'])", lowered)
            and not re.search(r"pytest-xdist\s*(?:==|<=|>=|<|>|~=)", lowered)
        )
        if not installs_testing_extra and not installs_unpinned_xdist:
            return False

        old_pytest_patterns = (
            r"pytest[^'\"]*<\s*4",
            r"pytest\s*==\s*3\.",
            r"pytest\s*>=\s*3\.[^'\"]*,\s*<\s*4",
        )
        return any(re.search(pattern, lowered) for pattern in old_pytest_patterns)

    def _is_dockerfile_instruction_boundary(self, line: str) -> bool:
        stripped = (line or "").lstrip()
        return bool(stripped and self._DOCKERFILE_INSTRUCTION_PATTERN.match(stripped))

    def _extract_heredoc_delimiters(self, line: str) -> List[str]:
        return [match.group(2) for match in self._HEREDOC_DELIMITER_PATTERN.finditer(line or "")]

    def _should_stop_after_blank_line(self, lines: List[str], index: int) -> bool:
        next_index = index + 1
        while next_index < len(lines) and not lines[next_index].strip():
            next_index += 1
        if next_index >= len(lines):
            return True
        stripped = lines[next_index].lstrip()
        return stripped.startswith("#") or self._is_dockerfile_instruction_boundary(stripped)

    def _extract_run_instruction(self, lines: List[str], index: int) -> Tuple[str, int]:
        full_instruction = lines[index]

        if "<<" in full_instruction:
            pending_delimiters = self._extract_heredoc_delimiters(full_instruction)
            while index + 1 < len(lines):
                next_line = lines[index + 1]
                if not pending_delimiters and self._is_dockerfile_instruction_boundary(next_line):
                    break
                if not pending_delimiters and not next_line.strip() and self._should_stop_after_blank_line(lines, index + 1):
                    break

                index += 1
                full_instruction += "\n" + next_line

                stripped = next_line.strip()
                if pending_delimiters and stripped == pending_delimiters[-1]:
                    pending_delimiters.pop()
                    continue
                pending_delimiters.extend(self._extract_heredoc_delimiters(next_line))
            return full_instruction, index

        quote_count = full_instruction.count('"') - full_instruction.count('\\"')
        while quote_count % 2 == 1 and index + 1 < len(lines):
            index += 1
            next_line = lines[index]
            full_instruction += "\n" + next_line
            quote_count += next_line.count('"') - next_line.count('\\"')

        return full_instruction, index

    def _extract_agent_dockerfile_instructions(self, original_dockerfile: str) -> Tuple[Optional[str], List[str]]:
        base_image_line = None
        agent_run_instructions = []
        lines = (original_dockerfile or "").split("\n")
        index = 0

        while index < len(lines):
            line = lines[index]
            if line.startswith("FROM "):
                base_image_line = line
            elif line.startswith("RUN "):
                full_instruction, index = self._extract_run_instruction(lines, index)
                full_instruction = self._normalize_agent_paths(full_instruction)
                full_instruction = self._normalize_run_instruction_for_docker(full_instruction)
                agent_run_instructions.append(full_instruction)
            index += 1

        return base_image_line, agent_run_instructions

    def _normalize_agent_paths(self, text: str) -> str:
        """Translate the setup container's root repo path to the evaluator path."""
        if not text:
            return text
        return self._AGENT_ROOT_WORKDIR_PATTERN.sub("/testbed", text)
        
    def process_single_instance(self, instance: Dict[str, Any], 
                               base_image: str = "auto",
                               model: str = DEFAULT_LLM_MODEL,
                               max_steps: int = 30,
                               enable_observation_compression: bool = False,
                               enable_long_term_memory: bool = False,
                               memory_path: Optional[str] = None,
                               memory_embedding_model: str = DEFAULT_MEMORY_EMBEDDING_MODEL,
                               enable_artifact_preflight: bool = True,
                               artifact_repair_rounds: int = 1) -> Dict[str, Any]:
        """
        处理单个评估实例
        
        Args:
            instance: 输入数据(包含 repo_url, problem_statement, patch 等)
            base_image: Docker 基础镜像
            model: LLM 模型
            max_steps: 最大步骤数
            
        Returns:
            docker_res 格式的结果字典
        """
        instance_id = instance.get("instance_id", "unknown")
        # Support both 'repo' and 'repo_url' field names
        repo_name = instance.get("repo", instance.get("repo_url", ""))
        if not repo_name.startswith("http"):
            repo_url = f"https://github.com/{repo_name}.git"
        else:
            repo_url = repo_name
        base_commit = instance.get("base_commit")
        problem_statement = instance.get("problem_statement", "")
        source_patch = instance.get("patch", "")
        test_patch = instance.get("test_patch", "")
        language = instance.get("language", "unknown")
        benchmark_evaluation_target = self.build_benchmark_evaluation_target(test_patch)
        
        print(f"\n{'='*60}")
        print(f"Processing instance: {instance_id}")
        print(f"Repository: {repo_url}")
        print(f"Language: {language}")
        print(f"{'='*60}\n")
        
        result = {
            "instance_id": instance_id,
            "repo_url": repo_url,
            "language": language,
            "dockerfile": None,
            "eval_script": None,  # 评估框架期望的字段名
            "build_success": False,
            "test_success": False,
            "platform": None,  # Docker platform override (e.g., linux/amd64 for ARM hosts)
            "logs": {
                "agent_steps": [],
                "error": None,
                "verified_test_command": None,
                "verified_test_commands": [],
                "verified_runtime_preparation_commands": [],
                "verified_post_test_patch_commands": [],
                "source_patch_rebuild_commands": [],
                "deferred_post_test_patch_commands": [],
                "filtered_runtime_preparation_commands": [],
                "filtered_test_commands": [],
                "refined_test_commands": [],
                "dropped_broad_test_commands": [],
                "build_recipe_source": None,
                "build_recipe_error": None,
                "artifact_preflight": [],
                "artifact_repair_rounds": [],
                "test_command_source": None,
                "runtime_preparation_source": None,
                "post_test_patch_source": None,
                "verification_source": None,
                "benchmark_evaluation_target": benchmark_evaluation_target,
                "skip_evaluation": False,
                "platform_support": None,
            },
        }

        platform_support = self._assess_platform_support(instance, language)
        result["logs"]["platform_support"] = platform_support
        if not platform_support["supported"]:
            reason = platform_support["reason"]
            print(f"⚠ Skipping {instance_id}: {reason}")
            result["logs"]["error"] = reason
            result["logs"]["test_command_source"] = "unsupported_platform"
            result["logs"]["skip_evaluation"] = True
            self._save_result(instance_id, result)
            return result
        
        # 创建workplace目录（使用项目目录下的workplace，便于查看）
        workplace = os.path.join("./workplace", f"multi_docker_eval_{instance_id}")
        os.makedirs(workplace, exist_ok=True)
        
        try:
            # 1. 运行 DockerAgent 进行环境配置
            print("[Step 1/4] Running DockerAgent for environment configuration...")
            # Honour DOCKERAGENT_REPAIR_MODE set by run_rat_benchmark.py.
            # selfverify / both → enable the agent's own post-synthesis repair.
            # runner / off      → disable it (runner loop is authoritative, or baseline mode).
            _repair_mode_env = os.environ.get("DOCKERAGENT_REPAIR_MODE", "selfverify")
            _enable_agent_repair = _repair_mode_env in ("selfverify", "both")

            agent = DockerAgent(
                repo_url=repo_url,
                base_image=base_image or "auto",
                model=model,
                workplace=workplace,
                base_commit=base_commit,  # checkout before image selection for accurate LLM analysis
                problem_statement=problem_statement,
                test_patch=test_patch,
                benchmark_evaluation_target=benchmark_evaluation_target,
                language=language,
                enable_observation_compression=enable_observation_compression,
                enable_long_term_memory=enable_long_term_memory,
                memory_path=memory_path,
                memory_embedding_model=memory_embedding_model,
                enable_post_synthesis_repair=_enable_agent_repair,
            )
            
            # base_commit 已在 DockerAgent.__init__ 中完成 checkout
            # 此处无需再次 checkout
            
            # 运行 agent 配置环境
            agent.run(max_steps=max_steps, keep_container=False)
            accepted_verification = getattr(agent, "verification_source", None) == "agent_report"
            
            # 记录 platform override（用于后续评估框架构建镜像时使用正确平台）
            if hasattr(agent, 'platform_override') and agent.platform_override:
                result["platform"] = agent.platform_override
                print(f"[Adapter] Platform override recorded: {agent.platform_override}")
            
            # 2. 提取 Dockerfile（复用 Agent 的配置指令）
            print("\n[Step 2/4] Extracting Dockerfile...")
            dockerfile_path = Path(workplace) / "Dockerfile"
            if dockerfile_path.exists():
                original_dockerfile = dockerfile_path.read_text()
                base_image_line, agent_run_instructions = self._extract_agent_dockerfile_instructions(
                    original_dockerfile
                )
                
                if not base_image_line:
                    print("✗ No FROM Dockerfile: missing FROM instruction")
                    result["logs"]["error"] = "Invalid Dockerfile: missing FROM instruction"
                else:
                    processed_instructions = [
                        self._prepare_agent_run_instruction_for_eval(instr, index + 1)
                        for index, instr in enumerate(agent_run_instructions)
                    ]
                    
                    dockerfile_content = self._build_eval_dockerfile(
                        base_image_line=base_image_line,
                        repo_url=repo_url,
                        base_commit=base_commit,
                        processed_instructions=processed_instructions,
                    )
                    result["dockerfile"] = dockerfile_content
                    print(f"✓ Dockerfile generated with {len(agent_run_instructions)} agent instructions")
            else:
                print("✗ Dockerfile not found")
                result["logs"]["error"] = "Dockerfile generation failed"
                result["logs"]["skip_evaluation"] = True
            
            # 3. 生成测试脚本 & 将 test_patch 注入镜像
            print("\n[Step 3/4] Generating test script...")
            build_recipe = getattr(agent, "build_recipe", None) or {}
            recipe_test_commands = self._normalize_commands(build_recipe.get("test_commands"))
            if accepted_verification and recipe_test_commands:
                structured_eval_test_command = None
                structured_eval_test_commands = recipe_test_commands
            else:
                structured_eval_test_command = (
                    getattr(agent, "verified_test_command", None)
                    if accepted_verification else None
                )
                structured_eval_test_commands = (
                    getattr(agent, "verified_test_commands", None)
                    if accepted_verification else None
                )
            runtime_preparation_commands_for_eval = self._select_runtime_preparation_commands_for_eval(
                agent,
                accepted_verification,
                language=language,
                source_patch=source_patch,
                test_patch=test_patch,
                test_commands=structured_eval_test_commands
                or ([structured_eval_test_command] if structured_eval_test_command else None),
            )
            test_script, setup_scripts, dockerfile_with_patch = self._generate_test_script(
                workplace=workplace,
                language=language,
                problem_statement=problem_statement,
                test_patch=test_patch,
                dockerfile_content=result.get("dockerfile", ""),
                structured_runtime_preparation_commands=runtime_preparation_commands_for_eval,
                structured_test_command=structured_eval_test_command,
                structured_test_commands=structured_eval_test_commands,
                structured_post_test_patch_commands=(
                    build_recipe.get("post_test_patch_commands")
                    if accepted_verification else None
                ),
            )
            result["eval_script"] = test_script
            result["setup_scripts"] = setup_scripts
            result["logs"]["verified_test_command"] = getattr(agent, "verified_test_command", None)
            result["logs"]["verified_test_commands"] = getattr(agent, "verified_test_commands", []) or []
            result["logs"]["verified_runtime_preparation_commands"] = (
                getattr(agent, "verified_runtime_preparation_commands", []) or []
            )
            result["logs"]["recipe_runtime_preparation_commands"] = (
                build_recipe.get("runtime_preparation_commands") or []
            )
            result["logs"]["recipe_test_commands"] = build_recipe.get("test_commands") or []
            result["logs"]["eval_runtime_preparation_commands"] = (
                getattr(self, "_last_eval_runtime_preparation_commands", [])
                or runtime_preparation_commands_for_eval
                or []
            )
            result["logs"]["source_patch_rebuild_commands"] = getattr(
                self,
                "_last_source_patch_rebuild_commands",
                [],
            )
            result["logs"]["deferred_post_test_patch_commands"] = getattr(
                self,
                "_last_deferred_post_test_patch_commands",
                [],
            )
            result["logs"]["filtered_runtime_preparation_commands"] = getattr(
                self,
                "_last_filtered_runtime_preparation_commands",
                [],
            )
            result["logs"]["filtered_test_commands"] = getattr(
                self,
                "_last_filtered_test_commands",
                [],
            )
            result["logs"]["refined_test_commands"] = getattr(
                self,
                "_last_refined_test_commands",
                [],
            )
            result["logs"]["dropped_broad_test_commands"] = getattr(
                self,
                "_last_dropped_broad_test_commands",
                [],
            )
            result["logs"]["verified_post_test_patch_commands"] = (
                build_recipe.get("post_test_patch_commands") or []
            )
            result["logs"]["build_recipe_source"] = getattr(agent, "build_recipe_source", None)
            result["logs"]["build_recipe_error"] = getattr(agent, "build_recipe_error", None)
            result["logs"]["build_recipe"] = getattr(agent, "build_recipe", None)
            result["logs"]["test_command_source"] = getattr(self, "_last_test_command_source", None)
            result["logs"]["runtime_preparation_source"] = getattr(
                self,
                "_last_runtime_preparation_source",
                None,
            )
            result["logs"]["post_test_patch_source"] = getattr(
                self,
                "_last_post_test_patch_source",
                None,
            )
            result["logs"]["verification_source"] = getattr(agent, "verification_source", None)
            result["logs"]["memory_stats"] = getattr(agent, "memory_stats", None)
            if dockerfile_with_patch:
                result["dockerfile"] = dockerfile_with_patch
            if not result["dockerfile"] or not result["eval_script"]:
                result["logs"]["skip_evaluation"] = True
            result["build_success"] = bool(result["dockerfile"] and result["eval_script"] and not result["logs"]["skip_evaluation"])

            if result["build_success"] and enable_artifact_preflight:
                print("\n[Step 4/5] Running artifact preflight and repair loop...")
                result = self._verify_and_repair_artifact(
                    instance=instance,
                    result=result,
                    client=getattr(agent, "client", None),
                    model=model,
                    workplace=workplace,
                    base_image_line=base_image_line if dockerfile_path.exists() else None,
                    repo_url=repo_url,
                    base_commit=base_commit,
                    language=language,
                    source_patch=source_patch,
                    test_patch=test_patch,
                    initial_recipe=build_recipe,
                    max_repair_rounds=max(0, artifact_repair_rounds),
                )
            
            # 5. 保存可交给 Multi-Docker-Eval 的最终 artifact
            print("\n[Step 5/5] Test script generated")
            print("Final validation will be performed by Multi-Docker-Eval framework")
            
            # 保存结果
            self._save_result(instance_id, result)
            
        except Exception as e:
            error_text = self._format_exception(e)
            print(f"\n✗ Error processing instance {instance_id}: {error_text}")
            result["logs"]["error"] = error_text
            if self._is_infrastructure_failure(error_text):
                result["logs"]["error_type"] = "infrastructure"
                result["logs"]["infrastructure_failure"] = True
                result["logs"]["skip_evaluation"] = True
            self._save_result(instance_id, result)
            
        finally:
            # 保留临时目录供查看（如需清理，取消下面注释）
            print(f"\n[Workplace Preserved] {workplace}")
            print(f"To inspect: ls -la {workplace}")
            # if os.path.exists(workplace):
            #     shutil.rmtree(workplace)
        
        return result

    def _verify_and_repair_artifact(
        self,
        instance: Dict[str, Any],
        result: Dict[str, Any],
        client,
        model: str,
        workplace: str,
        base_image_line: Optional[str],
        repo_url: str,
        base_commit: Optional[str],
        language: str,
        source_patch: str,
        test_patch: str,
        initial_recipe: Dict[str, Any],
        max_repair_rounds: int = 1,
    ) -> Dict[str, Any]:
        """Run a Multi-Docker-Eval-compatible preflight, then repair the recipe if needed."""
        preflight_records = []
        repair_records = []
        current_result = result
        current_recipe = dict(initial_recipe or {})

        for attempt in range(max_repair_rounds + 1):
            preflight = self._run_artifact_preflight(instance, current_result, workplace, attempt)
            preflight_records.append(preflight)
            current_result["logs"]["artifact_preflight"] = preflight_records
            current_result["test_success"] = bool(preflight.get("resolved"))

            if preflight.get("resolved"):
                print(f"✓ Artifact preflight passed on attempt {attempt}")
                break

            if attempt >= max_repair_rounds:
                print("✗ Artifact preflight still failed after repair budget was exhausted")
                break

            if preflight.get("infrastructure_failure"):
                print("⚠ Artifact preflight failed due to infrastructure/network signal; skipping LLM repair")
                break

            if not client:
                print("⚠ Artifact preflight failed but no LLM client is available for repair")
                break

            repaired_recipe, repair_record = self._repair_build_recipe(
                client=client,
                model=model,
                instance=instance,
                workplace=workplace,
                current_result=current_result,
                current_recipe=current_recipe,
                preflight=preflight,
                repair_round=attempt,
            )
            repair_records.append(repair_record)
            current_result["logs"]["artifact_repair_rounds"] = repair_records

            if not repaired_recipe:
                print("⚠ Recipe repair did not produce a usable recipe; keeping current artifact")
                break

            current_recipe = repaired_recipe
            current_result = self._render_result_from_build_recipe(
                result=current_result,
                recipe=current_recipe,
                instance=instance,
                workplace=workplace,
                base_image_line=base_image_line,
                repo_url=repo_url,
                base_commit=base_commit,
                language=language,
                source_patch=source_patch,
                test_patch=test_patch,
            )
            print(f"✓ Re-rendered artifact from repaired recipe for attempt {attempt + 1}")

        current_result["logs"]["artifact_preflight"] = preflight_records
        current_result["logs"]["artifact_repair_rounds"] = repair_records
        return current_result

    def _run_artifact_preflight(
        self,
        instance: Dict[str, Any],
        result: Dict[str, Any],
        workplace: str,
        attempt: int,
    ) -> Dict[str, Any]:
        instance_id = result.get("instance_id") or instance.get("instance_id", "unknown")
        safe_instance_id = self._sanitize_name(instance_id)
        preflight_root = Path(workplace) / "logs" / "artifact_preflight" / f"attempt_{attempt}"
        preflight_root.mkdir(parents=True, exist_ok=True)
        dataset_path = preflight_root / "dataset.jsonl"
        docker_res_path = preflight_root / "docker_res.json"
        output_path = preflight_root / "eval_output"
        run_id = f"ArtifactPreflight-{safe_instance_id}-attempt-{attempt}"

        dataset_path.write_text(json.dumps(instance, ensure_ascii=False) + "\n", encoding="utf-8")
        docker_res_path.write_text(
            json.dumps({instance_id: result}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        command = [
            sys.executable,
            str(self.repo_root / "Multi-Docker-Eval" / "evaluation" / "main.py"),
            f"base.dataset={dataset_path}",
            f"base.docker_res={docker_res_path}",
            f"base.run_id={run_id}",
            f"base.output_path={output_path}",
            "run_time.max_workers=1",
            "test.stability_runs=1",
        ]
        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH")
        mde_path = str(self.repo_root / "Multi-Docker-Eval")
        env["PYTHONPATH"] = f"{mde_path}:{existing_pythonpath}" if existing_pythonpath else mde_path

        print(f"  Artifact preflight attempt {attempt}: {' '.join(command)}")
        completed = subprocess.run(
            command,
            cwd=str(self.repo_root),
            env=env,
            text=True,
            capture_output=True,
        )

        instance_eval_dir = output_path / run_id / instance_id
        combined_report_path = instance_eval_dir / "combined_report.json"
        final_report_path = output_path / run_id / "final_report.json"
        combined_report = self._read_json_file(combined_report_path)
        final_report = self._read_json_file(final_report_path)
        log_excerpt = self._collect_preflight_log_excerpt(instance_eval_dir)
        stdout_tail = self._tail_text(completed.stdout, 6000)
        stderr_tail = self._tail_text(completed.stderr, 6000)
        infrastructure_failure = self._is_infrastructure_failure(
            "\n".join([stdout_tail, stderr_tail, log_excerpt])
        )

        return {
            "attempt": attempt,
            "run_id": run_id,
            "preflight_root": str(preflight_root),
            "returncode": completed.returncode,
            "resolved": bool(combined_report and combined_report.get("resolved")),
            "failed_before_patch": bool(combined_report and combined_report.get("failed_before_patch")),
            "passed_after_patch": bool(combined_report and combined_report.get("passed_after_patch")),
            "stable": bool(combined_report and combined_report.get("stable")),
            "infrastructure_failure": infrastructure_failure,
            "combined_report": combined_report,
            "final_report": final_report,
            "combined_report_path": str(combined_report_path),
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "log_excerpt": log_excerpt,
            "command": command,
        }

    def _repair_build_recipe(
        self,
        client,
        model: str,
        instance: Dict[str, Any],
        workplace: str,
        current_result: Dict[str, Any],
        current_recipe: Dict[str, Any],
        preflight: Dict[str, Any],
        repair_round: int,
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        repair_input = self._build_recipe_repair_input(
            instance=instance,
            workplace=workplace,
            current_result=current_result,
            current_recipe=current_recipe,
            preflight=preflight,
        )
        messages = [
            {"role": "system", "content": RECIPE_REPAIR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Repair this build recipe.\n\nInput JSON:\n```json\n"
                + json.dumps(repair_input, indent=2, ensure_ascii=False)
                + "\n```",
            },
        ]

        raw_content = ""
        usage = {}
        error = None
        parsed_recipe = None
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0,
            )
            raw_content = response.choices[0].message.content or ""
            usage_obj = getattr(response, "usage", None)
            if usage_obj:
                usage = {
                    "input_tokens": getattr(usage_obj, "prompt_tokens", 0),
                    "output_tokens": getattr(usage_obj, "completion_tokens", 0),
                    "total_tokens": getattr(usage_obj, "total_tokens", 0),
                }
            parsed_recipe = self._normalize_repaired_recipe(
                self._extract_json_object(raw_content),
                current_recipe,
            )
        except Exception as exc:
            error = str(exc)

        log_path = self._write_recipe_repair_log(
            workplace=workplace,
            repair_round=repair_round,
            messages=messages,
            raw_content=raw_content,
            parsed_recipe=parsed_recipe,
            usage=usage,
            error=error,
        )

        record = {
            "round": repair_round,
            "log_path": str(log_path),
            "usage": usage,
            "error": error,
            "recipe": parsed_recipe,
        }
        return parsed_recipe, record

    def _build_recipe_repair_input(
        self,
        instance: Dict[str, Any],
        workplace: str,
        current_result: Dict[str, Any],
        current_recipe: Dict[str, Any],
        preflight: Dict[str, Any],
    ) -> Dict[str, Any]:
        logs = current_result.get("logs", {})
        return {
            "instance_id": current_result.get("instance_id"),
            "repo_url": current_result.get("repo_url"),
            "language": current_result.get("language"),
            "problem_statement": self._truncate_text(instance.get("problem_statement", ""), 5000),
            "benchmark_evaluation_target": logs.get("benchmark_evaluation_target"),
            "current_recipe": current_recipe or logs.get("build_recipe") or {},
            "current_eval_script": self._truncate_text(current_result.get("eval_script", ""), 5000),
            "verified_test_commands": logs.get("verified_test_commands"),
            "recipe_test_commands": logs.get("recipe_test_commands"),
            "project_config_context": self._collect_repair_project_context(workplace),
            "project_exact_dependency_pins": self._collect_project_exact_dependency_pins(workplace),
            "preflight": {
                "returncode": preflight.get("returncode"),
                "resolved": preflight.get("resolved"),
                "failed_before_patch": preflight.get("failed_before_patch"),
                "passed_after_patch": preflight.get("passed_after_patch"),
                "combined_report": preflight.get("combined_report"),
                "stdout_tail": self._truncate_text(preflight.get("stdout_tail", ""), 6000),
                "stderr_tail": self._truncate_text(preflight.get("stderr_tail", ""), 6000),
                "log_excerpt": self._truncate_text(preflight.get("log_excerpt", ""), 12000),
            },
        }

    def _collect_repair_project_context(self, workplace: str) -> Dict[str, str]:
        """Collect bounded project config snippets for artifact recipe repair."""
        root = Path(workplace)
        if not root.exists():
            return {}

        candidates = []
        seen = set()

        def add_candidate(path: Path) -> None:
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                return
            if rel in seen or not path.is_file():
                return
            seen.add(rel)
            candidates.append((rel, path))

        for rel in self._REPAIR_CONTEXT_FILE_CANDIDATES:
            add_candidate(root / rel)

        for pattern in self._REPAIR_CONTEXT_GLOB_CANDIDATES:
            for path in sorted(root.glob(pattern)):
                add_candidate(path)

        context = {}
        remaining = self._REPAIR_CONTEXT_MAX_TOTAL_CHARS
        for rel, path in candidates:
            if remaining <= 0:
                break
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            max_chars = min(self._REPAIR_CONTEXT_MAX_FILE_CHARS, remaining)
            snippet = self._truncate_text(text, max_chars)
            context[rel] = snippet
            remaining -= len(snippet)

        return context

    def _collect_project_exact_dependency_pins(self, workplace: str) -> Dict[str, str]:
        """Collect unambiguous exact dependency pins declared by project config files."""
        root = Path(workplace)
        if not root.exists():
            return {}

        candidates = [
            root / "tox.ini",
            root / "requirements.txt",
            root / "requirements-dev.txt",
            root / "requirements-test.txt",
            root / "dev-requirements.txt",
            root / "test-requirements.txt",
        ]

        pins_by_name: Dict[str, set] = {}
        for path in candidates:
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                requirement = self._extract_exact_requirement_pin(line)
                if not requirement:
                    continue
                package_name = self._requirement_package_key(requirement)
                if not package_name:
                    continue
                pins_by_name.setdefault(package_name, set()).add(requirement)

        return {
            package: next(iter(pins))
            for package, pins in sorted(pins_by_name.items())
            if len(pins) == 1
        }

    def _extract_exact_requirement_pin(self, line: str) -> Optional[str]:
        stripped = (line or "").strip()
        if not stripped or stripped.startswith(("#", "-", "http://", "https://", "git+")):
            return None
        stripped = stripped.split("#", 1)[0].strip()
        stripped = stripped.split(";", 1)[0].strip()
        match = re.match(
            r"^([A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?)==([A-Za-z0-9_.!*+:-]+)$",
            stripped,
        )
        if not match:
            return None
        return f"{match.group(1)}=={match.group(2)}"

    def _requirement_package_key(self, requirement: str) -> Optional[str]:
        match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?", requirement or "")
        if not match:
            return None
        return match.group(1).lower().replace("_", "-")

    def _apply_project_exact_dependency_pins(
        self,
        recipe: Dict[str, Any],
        workplace: str,
    ) -> Dict[str, Any]:
        pins = self._collect_project_exact_dependency_pins(workplace)
        self._last_project_dependency_pin_rewrites = []
        if not pins:
            return recipe

        updated = dict(recipe or {})
        build_commands = []
        for command in self._normalize_commands(updated.get("build_commands")):
            rewritten = self._pin_pip_install_command_from_project_config(command, pins)
            if rewritten != command:
                self._last_project_dependency_pin_rewrites.append(
                    {"before": command, "after": rewritten}
                )
            build_commands.append(rewritten)
        updated["build_commands"] = build_commands
        return updated

    def _pin_pip_install_command_from_project_config(
        self,
        command: str,
        pins: Dict[str, str],
    ) -> str:
        if not command or "pip" not in command.lower() or " install" not in command.lower():
            return command

        parts = re.split(r"(\s*(?:&&|\|\||;|\|)\s*)", command)
        rewritten_parts = []
        for part in parts:
            if re.fullmatch(r"\s*(?:&&|\|\||;|\|)\s*", part or ""):
                rewritten_parts.append(part)
            else:
                rewritten_parts.append(self._pin_pip_install_segment(part, pins))
        return "".join(rewritten_parts)

    def _pin_pip_install_segment(self, segment: str, pins: Dict[str, str]) -> str:
        if "pip" not in (segment or "").lower() or "install" not in (segment or "").lower():
            return segment
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return segment

        install_index = self._find_pip_install_argument_start(tokens)
        if install_index is None:
            return segment

        changed = False
        result = tokens[:install_index]
        index = install_index
        options_with_values = {
            "-c",
            "--constraint",
            "-r",
            "--requirement",
            "-i",
            "--index-url",
            "--extra-index-url",
            "-f",
            "--find-links",
            "--trusted-host",
            "--timeout",
            "--default-timeout",
            "--python",
            "--config-settings",
        }
        while index < len(tokens):
            token = tokens[index]
            if token in options_with_values and index + 1 < len(tokens):
                result.extend([token, tokens[index + 1]])
                index += 2
                continue
            if token.startswith("-"):
                result.append(token)
                index += 1
                continue

            replacement = self._replace_requirement_with_project_pin(token, pins)
            if replacement != token:
                changed = True
            result.append(replacement)
            index += 1

        return self._shell_join_tokens(result) if changed else segment

    def _find_pip_install_argument_start(self, tokens: List[str]) -> Optional[int]:
        for index, token in enumerate(tokens):
            basename = token.rsplit("/", 1)[-1]
            if basename in {"pip", "pip3"} and index + 1 < len(tokens) and tokens[index + 1] == "install":
                return index + 2
            if (
                basename.startswith("python")
                and index + 3 < len(tokens)
                and tokens[index + 1] == "-m"
                and tokens[index + 2] == "pip"
                and tokens[index + 3] == "install"
            ):
                return index + 4
        return None

    def _replace_requirement_with_project_pin(self, requirement: str, pins: Dict[str, str]) -> str:
        package_key = self._requirement_package_key(requirement)
        if not package_key or package_key not in pins:
            return requirement

        if requirement.startswith((".", "/", "git+", "http://", "https://")):
            return requirement
        if re.search(r"===|==|~=|!=|<=|>=|<|>", requirement):
            return requirement

        match = re.match(r"^([A-Za-z0-9_.-]+)(\[[A-Za-z0-9_,.-]+\])?", requirement)
        if not match:
            return requirement
        extras = match.group(2) or ""
        pinned = pins[package_key]
        pinned_match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==(.+)$", pinned)
        if extras and pinned_match:
            return f"{pinned_match.group(1)}{extras}=={pinned_match.group(2)}"
        return pinned

    def _shell_join_tokens(self, tokens: List[str]) -> str:
        return " ".join(shlex.quote(token) for token in tokens)

    def _render_result_from_build_recipe(
        self,
        result: Dict[str, Any],
        recipe: Dict[str, Any],
        instance: Dict[str, Any],
        workplace: str,
        base_image_line: Optional[str],
        repo_url: str,
        base_commit: Optional[str],
        language: str,
        source_patch: str,
        test_patch: str,
    ) -> Dict[str, Any]:
        if not base_image_line:
            result["logs"]["build_recipe_error"] = "Cannot repair artifact: missing base image line"
            return result

        recipe = self._apply_project_exact_dependency_pins(recipe, workplace)
        recipe = self._ensure_local_pytest_plugin_recipe_install(recipe, workplace)
        processed_instructions = self._build_processed_instructions_from_recipe(recipe)
        dockerfile_content = self._build_eval_dockerfile(
            base_image_line=base_image_line,
            repo_url=repo_url,
            base_commit=base_commit,
            processed_instructions=processed_instructions,
        )

        fake_agent = SimpleNamespace(
            build_recipe=recipe,
            verified_runtime_preparation_commands=[],
            verified_test_commands=recipe.get("test_commands") or [],
        )
        runtime_preparation_commands = self._select_runtime_preparation_commands_for_eval(
            fake_agent,
            accepted_verification=True,
            language=language,
            source_patch=source_patch,
            test_patch=test_patch,
            test_commands=recipe.get("test_commands") or [],
        )
        test_script, setup_scripts, dockerfile_with_patch = self._generate_test_script(
            workplace=workplace,
            language=language,
            problem_statement=instance.get("problem_statement", ""),
            test_patch=test_patch,
            dockerfile_content=dockerfile_content,
            structured_runtime_preparation_commands=runtime_preparation_commands or [],
            structured_test_commands=recipe.get("test_commands") or [],
            structured_post_test_patch_commands=recipe.get("post_test_patch_commands") or [],
            allow_summary_fallback=False,
        )

        result["dockerfile"] = dockerfile_with_patch or dockerfile_content
        result["eval_script"] = test_script
        result["setup_scripts"] = setup_scripts
        result["build_success"] = bool(result["dockerfile"] and result["eval_script"])
        result["logs"]["build_recipe"] = recipe
        result["logs"]["build_recipe_source"] = "artifact_repair_llm"
        result["logs"]["build_recipe_error"] = None
        result["logs"]["recipe_runtime_preparation_commands"] = recipe.get("runtime_preparation_commands") or []
        result["logs"]["recipe_test_commands"] = recipe.get("test_commands") or []
        result["logs"]["verified_post_test_patch_commands"] = recipe.get("post_test_patch_commands") or []
        result["logs"]["project_dependency_pin_rewrites"] = getattr(
            self,
            "_last_project_dependency_pin_rewrites",
            [],
        )
        result["logs"]["eval_runtime_preparation_commands"] = getattr(
            self,
            "_last_eval_runtime_preparation_commands",
            [],
        )
        result["logs"]["source_patch_rebuild_commands"] = getattr(
            self,
            "_last_source_patch_rebuild_commands",
            [],
        )
        result["logs"]["filtered_runtime_preparation_commands"] = getattr(
            self,
            "_last_filtered_runtime_preparation_commands",
            [],
        )
        result["logs"]["filtered_test_commands"] = getattr(
            self,
            "_last_filtered_test_commands",
            [],
        )
        result["logs"]["refined_test_commands"] = getattr(
            self,
            "_last_refined_test_commands",
            [],
        )
        result["logs"]["dropped_broad_test_commands"] = getattr(
            self,
            "_last_dropped_broad_test_commands",
            [],
        )
        return result

    def _ensure_local_pytest_plugin_recipe_install(
        self,
        recipe: Dict[str, Any],
        workplace: str,
    ) -> Dict[str, Any]:
        """Ensure pytest plugin repos register local patched code in fresh artifacts."""
        if not self._recipe_needs_local_pytest_plugin_install(recipe, workplace):
            return recipe

        updated = dict(recipe or {})
        build_commands = self._normalize_commands(updated.get("build_commands"))
        updated["build_commands"] = self._dedupe_preserve_order(
            build_commands + ["cd /testbed && pip install -e ."]
        )
        rationale = str(updated.get("rationale") or "")
        note = (
            " Added local editable install because this repository declares a pytest11 "
            "plugin entry point and benchmark tests must exercise patched local code."
        )
        updated["rationale"] = (rationale + note).strip()
        return updated

    def _recipe_needs_local_pytest_plugin_install(
        self,
        recipe: Dict[str, Any],
        workplace: str,
    ) -> bool:
        commands = self._normalize_commands((recipe or {}).get("build_commands"))
        test_commands = self._normalize_commands((recipe or {}).get("test_commands"))
        if not any(self._is_pytest_command(command) for command in test_commands):
            return False

        command_blob = "\n".join(commands).lower()
        local_install_markers = (
            "pip install -e .",
            "pip install -e /testbed",
            "pip install -e /app",
            "python setup.py develop",
        )
        if any(marker in command_blob for marker in local_install_markers):
            return False

        root = Path(workplace)
        setup_py = root / "setup.py"
        pyproject = root / "pyproject.toml"
        setup_text = ""
        pyproject_text = ""
        try:
            setup_text = setup_py.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            pass
        try:
            pyproject_text = pyproject.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            pass

        declares_pytest_plugin = (
            "pytest11" in setup_text
            or "pytest11" in pyproject_text
            or "pytest11" in command_blob
        )
        return declares_pytest_plugin

    def _build_processed_instructions_from_recipe(self, recipe: Dict[str, Any]) -> List[str]:
        processed = []
        for index, command in enumerate(self._normalize_commands((recipe or {}).get("build_commands")), start=1):
            instruction = command if command.lstrip().upper().startswith("RUN ") else f"RUN {command}"
            instruction = self._normalize_agent_paths(instruction)
            instruction = self._normalize_run_instruction_for_docker(instruction)
            instruction = self._prepare_agent_run_instruction_for_eval(instruction, index)
            processed.append(instruction)
        return processed

    def _normalize_repaired_recipe(
        self,
        candidate: Dict[str, Any],
        current_recipe: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(candidate, dict):
            return None

        repaired = dict(current_recipe or {})
        for key in (
            "build_commands",
            "post_test_patch_commands",
            "runtime_preparation_commands",
            "test_commands",
            "excluded_commands",
        ):
            if key in candidate:
                value = candidate.get(key)
                if key == "excluded_commands":
                    repaired[key] = value if isinstance(value, list) else []
                else:
                    repaired[key] = self._normalize_commands(value)

        repaired["rationale"] = str(candidate.get("rationale") or repaired.get("rationale") or "")
        confidence = str(candidate.get("confidence") or repaired.get("confidence") or "low").lower()
        repaired["confidence"] = confidence if confidence in {"high", "medium", "low"} else "low"

        if not self._normalize_commands(repaired.get("test_commands")):
            return None
        repaired.setdefault("build_commands", [])
        repaired.setdefault("post_test_patch_commands", [])
        repaired.setdefault("runtime_preparation_commands", [])
        repaired.setdefault("excluded_commands", [])
        return repaired

    def _extract_json_object(self, text: str) -> Dict[str, Any]:
        content = (text or "").strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, flags=re.DOTALL)
        if fenced:
            content = fenced.group(1)
        else:
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end != -1 and end > start:
                content = content[start:end + 1]
        return json.loads(content)

    def _write_recipe_repair_log(
        self,
        workplace: str,
        repair_round: int,
        messages: List[Dict[str, str]],
        raw_content: str,
        parsed_recipe: Optional[Dict[str, Any]],
        usage: Dict[str, int],
        error: Optional[str],
    ) -> Path:
        log_dir = Path(workplace) / "logs" / "recipe_repair_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{repair_round}.md"
        payload = {
            "messages": messages,
            "raw_content": raw_content,
            "parsed_recipe": parsed_recipe,
            "usage": usage,
            "error": error,
        }
        log_path.write_text(
            "# Recipe Repair LLM Call\n\n"
            "## LLM INPUT\n"
            "```json\n"
            f"{json.dumps(messages, indent=2, ensure_ascii=False)}\n"
            "```\n\n"
            "## LLM OUTPUT\n"
            "```text\n"
            f"{raw_content or ''}\n"
            "```\n\n"
            "## PARSED RESULT\n"
            "```json\n"
            f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n"
            "```\n",
            encoding="utf-8",
        )
        return log_path

    def _read_json_file(self, path: Path) -> Optional[Dict[str, Any]]:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _collect_preflight_log_excerpt(self, instance_eval_dir: Path) -> str:
        if not instance_eval_dir.exists():
            return ""
        candidate_files = []
        for pattern in (
            "build/build_image.log",
            "logs/run_instance_prev_apply_*.log",
            "logs/run_instance_after_apply_*.log",
            "logs/test_output_prev_apply_*.txt",
            "logs/test_output_after_apply_*.txt",
        ):
            candidate_files.extend(sorted(instance_eval_dir.glob(pattern)))

        excerpts = []
        for path in candidate_files[:12]:
            try:
                excerpts.append(
                    f"===== {path.relative_to(instance_eval_dir)} =====\n"
                    f"{self._tail_text(path.read_text(encoding='utf-8', errors='replace'), 5000)}"
                )
            except OSError:
                continue
        return "\n\n".join(excerpts)

    def _tail_text(self, text: str, max_chars: int) -> str:
        text = text or ""
        if len(text) <= max_chars:
            return text
        return text[-max_chars:]

    def _truncate_text(self, text: str, max_chars: int) -> str:
        text = text or ""
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"\n... [truncated {len(text) - max_chars} chars]"

    def _sanitize_name(self, value: str) -> str:
        return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)

    def _select_runtime_preparation_commands_for_eval(
        self,
        agent,
        accepted_verification: bool,
        language: str = "",
        source_patch: str = "",
        test_patch: str = "",
        test_commands: Optional[List[str]] = None,
    ) -> Optional[List[str]]:
        """Compose commands that must run in the eval container immediately before tests."""
        self._last_source_patch_rebuild_commands = []
        self._last_filtered_runtime_preparation_commands = []
        if not accepted_verification:
            return None

        build_recipe = getattr(agent, "build_recipe", None) or {}
        test_commands = self._normalize_commands(
            test_commands if test_commands is not None else getattr(agent, "verified_test_commands", None)
        )
        patch_rebuild_commands = self._select_source_patch_rebuild_commands(
            build_recipe,
            source_patch=source_patch,
            language=language,
            test_commands=test_commands,
            test_patch=test_patch,
        )
        self._last_source_patch_rebuild_commands = list(patch_rebuild_commands)

        recipe_commands = self._normalize_commands(
            build_recipe.get("runtime_preparation_commands")
        )

        verified_commands = self._normalize_commands(
            getattr(agent, "verified_runtime_preparation_commands", None)
        )
        runtime_commands = recipe_commands or verified_commands
        runtime_commands = self._filter_runtime_preparation_commands_for_eval(
            runtime_commands,
            test_patch,
            reset_log=True,
        )
        combined = self._dedupe_preserve_order(patch_rebuild_commands + runtime_commands)
        return combined or None

    def _select_source_patch_rebuild_commands(
        self,
        build_recipe: Dict[str, Any],
        source_patch: str,
        language: str = "",
        test_commands: Optional[List[str]] = None,
        test_patch: str = "",
    ) -> List[str]:
        """Replay proven build and artifact publication commands after source patches."""
        if not self._patch_modifies_rebuild_relevant_files(source_patch):
            return []
        if self._test_commands_include_rebuild_step(test_commands or []):
            return []

        selected = []
        saw_rebuild_command = False
        for command in self._normalize_commands((build_recipe or {}).get("build_commands")):
            if self._is_rebuild_command(command, language):
                selected.append(command)
                saw_rebuild_command = True
                continue
            if saw_rebuild_command and self._is_build_artifact_publication_command(command):
                selected.append(command)
        if selected and self._test_patch_adds_compiled_test_artifacts(test_patch, test_commands or []):
            selected.append(self._build_compiled_test_artifact_publication_command())
        selected.extend(self._build_expected_executable_publication_commands(test_commands or []))
        return self._dedupe_preserve_order(selected)

    def _test_patch_adds_compiled_test_artifacts(
        self,
        test_patch: str,
        test_commands: List[str],
    ) -> bool:
        """Detect test patches that add source-tree tests backed by compiled artifacts."""
        changed_paths = self._extract_patch_changed_paths(test_patch)
        if not changed_paths:
            return False

        test_tree_paths = [
            path for path in changed_paths
            if path.startswith(("test/", "tests/")) or self._looks_like_test_file(path)
        ]
        if not test_tree_paths:
            return False

        compiled_markers = (
            ".c",
            ".cc",
            ".cpp",
            ".cxx",
            ".h",
            ".hh",
            ".hpp",
        )
        has_compiled_test_change = any(
            path.endswith(compiled_markers) or path.endswith("CMakeLists.txt")
            for path in test_tree_paths
        )
        if not has_compiled_test_change:
            return False

        command_blob = " ".join(test_commands or []).lower()
        return any(
            marker in command_blob
            for marker in ("run_all", "/test/", " test/", "cd test", "cd /testbed/test")
        )

    def _build_compiled_test_artifact_publication_command(self) -> str:
        """Expose CMake-built test helpers to source-tree test runners."""
        return (
            "for dir in build/test build/tests; do "
            "if [ -d \"$dir\" ] && [ -d test ]; then "
            "find \"$dir\" -maxdepth 1 -type f -perm -111 "
            "-exec sh -c 'ln -sf \"/testbed/$1\" \"test/$(basename \"$1\")\"' _ {} \\; ; "
            "fi; "
            "done"
        )

    def _build_expected_executable_publication_commands(self, test_commands: List[str]) -> List[str]:
        executable_names = self._extract_expected_executable_names(test_commands)
        if not executable_names:
            return []

        quoted_names = " ".join(self._shell_single_quote(name) for name in executable_names)
        command = (
            f"for exe in {quoted_names}; do "
            "found=$(find build -type f -name \"$exe\" -perm -111 2>/dev/null | head -n 1); "
            "if [ -n \"$found\" ]; then "
            "ln -sf \"/testbed/$found\" \"$exe\"; "
            "mkdir -p build; "
            "ln -sf \"/testbed/$found\" \"build/$exe\"; "
            "fi; "
            "done"
        )
        return [command]

    def _extract_expected_executable_names(self, test_commands: List[str]) -> List[str]:
        names = []
        seen = set()
        token_pattern = re.compile(
            r"(?:^|[\s;&|()])"
            r"((?:/app|/testbed|\./)?[A-Za-z0-9_./+-]*(?:test|Test)[A-Za-z0-9_./+-]*)"
            r"(?=\s|$)"
        )
        for command in test_commands or []:
            for match in token_pattern.finditer(command or ""):
                token = match.group(1).strip("'\"")
                basename = os.path.basename(token.rstrip("/"))
                if not basename:
                    continue
                prefix = (command or "")[:match.start(1)]
                if re.search(r"(?:^|[\s;&|()])cd\s+$", prefix):
                    continue
                if basename.lower() in {"app", "testbed", "test", "tests", "ctest", "pytest"}:
                    continue
                if "." in basename:
                    continue
                if basename not in seen:
                    seen.add(basename)
                    names.append(basename)
        return names

    def _filter_runtime_preparation_commands_for_eval(
        self,
        commands: List[str],
        test_patch: str,
        reset_log: bool = False,
    ) -> List[str]:
        """Remove runtime exports that conflict with the benchmark's own env tests."""
        if reset_log:
            self._last_filtered_runtime_preparation_commands = []

        kept = []
        removed = []
        for command in commands or []:
            if self._runtime_export_conflicts_with_test_patch(command, test_patch):
                removed.append(command)
            else:
                kept.append(command)

        if removed:
            previous = getattr(self, "_last_filtered_runtime_preparation_commands", [])
            self._last_filtered_runtime_preparation_commands = self._dedupe_preserve_order(
                previous + removed
            )
        return kept

    def _filter_test_commands_for_eval(
        self,
        commands: List[str],
        test_patch: str,
        reset_log: bool = False,
    ) -> List[str]:
        """Keep final test command semantics intact.

        Inline env assignments may be required for outer test collection even when
        the benchmark test removes the env var inside a nested subprocess.
        """
        if reset_log:
            self._last_filtered_test_commands = []
        return self._normalize_commands(commands)

    def _remove_conflicting_test_command_env(self, command: str, test_patch: str) -> str:
        if not command or not test_patch:
            return command

        filtered = command
        for variable in self._extract_inline_env_vars(command):
            if not self._test_patch_removes_or_unsets_env_var(test_patch, variable):
                continue
            filtered = self._remove_inline_env_assignment(filtered, variable)
            filtered = self._remove_env_command_assignment(filtered, variable)
            filtered = self._remove_export_env_segment(filtered, variable)
        return self._cleanup_shell_command_spacing(filtered)

    def _extract_inline_env_vars(self, command: str) -> List[str]:
        variables = []
        seen = set()
        env_value = r"(?:'[^']*'|\"[^\"]*\"|[^\s;&|]+)"
        assignments_pattern = re.compile(
            rf"(^|(?:&&|\|\||;)\s*)(?:env\s+)?"
            rf"((?:[A-Za-z_][A-Za-z0-9_]*={env_value}\s+)+)"
        )
        variable_pattern = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")
        for match in assignments_pattern.finditer(command or ""):
            for variable_match in variable_pattern.finditer(match.group(2)):
                variable = variable_match.group(1)
                if variable not in seen:
                    seen.add(variable)
                    variables.append(variable)
        return variables

    def _remove_inline_env_assignment(self, command: str, variable: str) -> str:
        env_value = r"(?:'[^']*'|\"[^\"]*\"|[^\s;&|]+)"
        escaped = re.escape(variable)
        pattern = re.compile(
            rf"(^|(?:&&|\|\||;)\s*){escaped}={env_value}\s+"
        )
        return pattern.sub(r"\1", command or "")

    def _remove_env_command_assignment(self, command: str, variable: str) -> str:
        env_value = r"(?:'[^']*'|\"[^\"]*\"|[^\s;&|]+)"
        escaped = re.escape(variable)
        pattern = re.compile(
            rf"(^|(?:&&|\|\||;)\s*)env\s+"
            rf"((?:[A-Za-z_][A-Za-z0-9_]*={env_value}\s+)+)"
        )
        assignment_pattern = re.compile(
            rf"([A-Za-z_][A-Za-z0-9_]*={env_value}\s+)"
        )

        def replace(match):
            kept = []
            for assignment_match in assignment_pattern.finditer(match.group(2)):
                assignment = assignment_match.group(1)
                if re.match(rf"{escaped}=", assignment):
                    continue
                kept.append(assignment)
            if kept:
                return match.group(1) + "env " + "".join(kept)
            return match.group(1)

        return pattern.sub(replace, command or "")

    def _remove_export_env_segment(self, command: str, variable: str) -> str:
        env_value = r"(?:'[^']*'|\"[^\"]*\"|[^\s;&|]+)"
        escaped = re.escape(variable)
        pattern = re.compile(
            rf"(^|(?:&&|\|\||;)\s*)export\s+{escaped}={env_value}\s*"
            rf"(?:(?:&&|\|\||;)\s*)?"
        )
        return pattern.sub(r"\1", command or "")

    def _cleanup_shell_command_spacing(self, command: str) -> str:
        cleaned = re.sub(r"\s+", " ", command or "").strip()
        cleaned = re.sub(r"\s*(?:&&|\|\||;)\s*$", "", cleaned).strip()
        cleaned = re.sub(r"^(?:&&|\|\||;)\s*", "", cleaned).strip()
        return cleaned

    def _refine_test_commands_to_changed_targets(
        self,
        commands: List[str],
        test_patch: str,
        source_root: Optional[str] = None,
        reset_log: bool = False,
    ) -> List[str]:
        """Prefer benchmark-added pytest nodes over old smoke-test nodes when safe."""
        if reset_log:
            self._last_refined_test_commands = []

        targets = self._extract_changed_test_targets(test_patch, source_root=source_root)
        if not targets:
            return commands

        targets_by_file = self._group_test_targets_by_file(targets)
        refined_commands = []
        changes = []
        for command in commands or []:
            refined = command
            if self._is_pytest_command(command):
                matching_targets = self._select_pytest_targets_for_command(command, targets_by_file)
                if matching_targets:
                    refined = self._replace_pytest_selection_with_targets(
                        command,
                        matching_targets,
                        targets_by_file,
                    )
            if refined != command:
                changes.append({"original": command, "refined": refined})
            refined_commands.append(refined)

        if changes:
            previous = getattr(self, "_last_refined_test_commands", [])
            self._last_refined_test_commands = previous + changes
        return refined_commands

    def _adapt_pytest_commands_for_nested_pytester(
        self,
        commands: List[str],
        test_patch: str,
    ) -> List[str]:
        """Avoid leaking parent pytest config into nested pytester/testdir runs."""
        patch = test_patch or ""
        if not (
            re.search(r"\b(?:testdir|pytester)\.runpytest\b|\brunpytest\s*\(", patch)
            and "settings.configure" in patch
            and "DJANGO_SETTINGS_MODULE" in patch
        ):
            return commands

        needs_empty_django_env = bool(
            re.search(
                r"monkeypatch\.delenv\(\s*['\"]DJANGO_SETTINGS_MODULE['\"]",
                patch,
            )
        )

        adapted = []
        changes = []
        for command in commands or []:
            refined = self._adapt_nested_pytester_command(
                command,
                needs_empty_django_env=needs_empty_django_env,
            )
            if refined != command:
                changes.append(
                    {
                        "original": command,
                        "refined": refined,
                        "reason": "nested_pytester_uses_own_django_settings",
                    }
                )
            adapted.append(refined)

        if changes:
            previous = getattr(self, "_last_refined_test_commands", [])
            self._last_refined_test_commands = previous + changes
        return adapted

    def _add_pytest_null_config(self, command: str) -> str:
        return self._adapt_nested_pytester_command(
            command,
            needs_empty_django_env=False,
        )

    def _adapt_nested_pytester_command(
        self,
        command: str,
        needs_empty_django_env: bool = False,
    ) -> str:
        if not self._is_pytest_command(command):
            return command

        refined = command
        additions = []
        if not re.search(r"(^|\s)(?:-c|--config-file)(?:\s|=)", refined or ""):
            additions.extend(["-c", "/dev/null"])
        if not re.search(r"(^|\s)-p\s+pytester(?:\s|$)", refined or ""):
            additions.extend(["-p", "pytester"])
        if not re.search(r"(^|\s)--noconftest(?:\s|$)", refined or ""):
            additions.append("--noconftest")

        invocation_pattern = (
            r"(?<![A-Za-z0-9_.-])"
            r"((?:python3?|pypy3?)\s+-m\s+pytest|py\.test|pytest)\b"
        )
        if additions:
            refined = re.sub(
                invocation_pattern,
                r"\1 " + " ".join(additions),
                refined,
                count=1,
            )

        if needs_empty_django_env and not re.search(
            r"(^|[\s;&|])DJANGO_SETTINGS_MODULE=",
            refined or "",
        ):
            refined = re.sub(
                invocation_pattern,
                r"DJANGO_SETTINGS_MODULE= \1",
                refined,
                count=1,
            )
        return refined

    def _select_target_covering_test_commands(
        self,
        commands: List[str],
        test_patch: str,
        source_root: Optional[str] = None,
        reset_log: bool = False,
    ) -> List[str]:
        """Drop broad fallback test commands once a narrower command covers the benchmark target."""
        if reset_log:
            self._last_dropped_broad_test_commands = []

        if len(commands or []) <= 1:
            return commands

        changed_files = self._extract_changed_test_files(test_patch)
        changed_targets = self._extract_changed_test_targets(test_patch, source_root=source_root)
        if not changed_files and not changed_targets:
            return commands

        covering = []
        dropped = []
        for command in commands or []:
            if self._command_covers_benchmark_target(command, changed_files, changed_targets):
                covering.append(command)
            else:
                dropped.append(command)

        if not covering or not dropped:
            return commands

        self._last_dropped_broad_test_commands = dropped
        return covering

    def _command_covers_benchmark_target(
        self,
        command: str,
        changed_files: List[str],
        changed_targets: List[str],
    ) -> bool:
        if not command:
            return False
        if any(target and target in command for target in changed_targets or []):
            return True
        return any(self._command_mentions_test_file(command, path) for path in changed_files or [])

    def _group_test_targets_by_file(self, targets: List[str]) -> Dict[str, List[str]]:
        grouped: Dict[str, List[str]] = {}
        for target in targets or []:
            file_path = target.split("::", 1)[0]
            grouped.setdefault(file_path, []).append(target)
        return grouped

    def _is_pytest_command(self, command: str) -> bool:
        return bool(
            re.search(
                r"(?<![A-Za-z0-9_.-])(?:python3?|pypy3?)\s+-m\s+pytest\b"
                r"|(?<![A-Za-z0-9_.-])py\.test\b"
                r"|(?<![A-Za-z0-9_.-])pytest\b",
                command or "",
            )
        )

    def _select_pytest_targets_for_command(
        self,
        command: str,
        targets_by_file: Dict[str, List[str]],
    ) -> List[str]:
        selected = []
        for file_path, targets in targets_by_file.items():
            if self._command_mentions_test_file(command, file_path):
                selected.extend(targets)
                continue
            if self._command_mentions_parent_test_collection(command, file_path):
                selected.extend(targets)
        return self._dedupe_preserve_order(selected)

    def _command_mentions_test_file(self, command: str, file_path: str) -> bool:
        normalized = self._strip_eval_root_prefixes(command)
        return file_path in normalized

    def _command_mentions_parent_test_collection(self, command: str, file_path: str) -> bool:
        parent_dirs = self._parent_test_directories(file_path)
        if not parent_dirs:
            return False

        tokens = self._extract_shell_path_tokens(command)
        for token in tokens:
            normalized = self._strip_eval_root_prefixes(token).rstrip("/")
            if normalized in parent_dirs:
                return True
        return False

    def _parent_test_directories(self, file_path: str) -> List[str]:
        parts = (file_path or "").split("/")
        parents = []
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in {"test", "tests"} or parent.endswith(("/test", "/tests")):
                parents.append(parent)
        return sorted(set(parents), key=len, reverse=True)

    def _extract_shell_path_tokens(self, command: str) -> List[str]:
        tokens = []
        for match in re.finditer(r"(?<![A-Za-z0-9_./-])(?:/app/|/testbed/|\./)?[A-Za-z0-9_./-]+(?=\s|$)", command or ""):
            tokens.append(match.group(0))
        return tokens

    def _replace_pytest_selection_with_targets(
        self,
        command: str,
        matching_targets: List[str],
        targets_by_file: Dict[str, List[str]],
    ) -> str:
        refined = command
        for file_path, targets in targets_by_file.items():
            file_targets = [target for target in matching_targets if target.startswith(f"{file_path}::")]
            if not file_targets:
                continue
            refined, replaced = self._replace_test_file_selection(
                refined,
                file_path,
                file_targets,
            )
            if replaced:
                return refined

        refined, replaced = self._replace_test_collection_selection(refined, matching_targets)
        if replaced:
            return refined
        return f"{refined} {' '.join(matching_targets)}"

    def _replace_test_file_selection(
        self,
        command: str,
        file_path: str,
        targets: List[str],
    ) -> Tuple[str, bool]:
        target_text = " ".join(targets)
        escaped = re.escape(file_path)
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_./-])(?:/app/|/testbed/|\./)?"
            rf"{escaped}(?:::[^\s]+)?"
        )
        replaced = False

        def replace(match):
            nonlocal replaced
            if not replaced:
                replaced = True
                return target_text
            return ""

        refined, count = pattern.subn(replace, command)
        return self._cleanup_shell_command_spacing(refined), count > 0

    def _replace_test_collection_selection(
        self,
        command: str,
        matching_targets: List[str],
    ) -> Tuple[str, bool]:
        target_text = " ".join(matching_targets)
        parent_dirs = []
        for target in matching_targets:
            parent_dirs.extend(self._parent_test_directories(target.split("::", 1)[0]))
        for parent in sorted(set(parent_dirs), key=len, reverse=True):
            escaped = re.escape(parent)
            pattern = re.compile(
                rf"(?<![A-Za-z0-9_./-])(?:/app/|/testbed/|\./)?"
                rf"{escaped}/?(?=\s|$)"
            )
            refined, count = pattern.subn(target_text, command, count=1)
            if count:
                return refined, True
        return command, False

    def _strip_eval_root_prefixes(self, text: str) -> str:
        stripped = text or ""
        stripped = stripped.replace("/testbed/", "")
        stripped = stripped.replace("/app/", "")
        stripped = re.sub(r"(?<![A-Za-z0-9_.-])\./", "", stripped)
        return stripped

    def _runtime_export_conflicts_with_test_patch(self, command: str, test_patch: str) -> bool:
        exported_vars = self._extract_exported_env_vars(command)
        if not exported_vars or not test_patch:
            return False

        for variable in exported_vars:
            if self._test_patch_removes_or_unsets_env_var(test_patch, variable):
                return True
        return False

    def _extract_exported_env_vars(self, command: str) -> List[str]:
        variables = []
        for segment in re.split(r"\s*(?:&&|;|\n)\s*", command or ""):
            match = re.match(r"export\s+([A-Za-z_][A-Za-z0-9_]*)=", segment.strip())
            if match:
                variables.append(match.group(1))
        return variables

    def _test_patch_removes_or_unsets_env_var(self, test_patch: str, variable: str) -> bool:
        escaped = re.escape(variable)
        patterns = (
            rf"(?:monkeypatch\.)?delenv\(\s*['\"]{escaped}['\"]",
            rf"os\.environ\.pop\(\s*['\"]{escaped}['\"]",
            rf"del\s+os\.environ\[\s*['\"]{escaped}['\"]\s*\]",
            rf"\bunset\s+{escaped}\b",
        )
        return any(re.search(pattern, test_patch or "") for pattern in patterns)

    def _shell_single_quote(self, value: str) -> str:
        return "'" + (value or "").replace("'", "'\"'\"'") + "'"

    def _patch_modifies_rebuild_relevant_files(self, patch_text: str) -> bool:
        for path in self._extract_patch_changed_paths(patch_text):
            if not self._is_non_rebuild_path(path):
                return True
        return False

    def _extract_patch_changed_paths(self, patch_text: str) -> List[str]:
        paths = []
        seen = set()
        for left, right in re.findall(r"^diff --git a/(.*?) b/(.*?)$", patch_text or "", re.MULTILINE):
            for path in (left, right):
                normalized = self._normalize_patch_path(path)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    paths.append(normalized)

        for marker in re.findall(r"^(?:---|\+\+\+) [ab]/([^\t\n\r]+)", patch_text or "", re.MULTILINE):
            normalized = self._normalize_patch_path(marker)
            if normalized and normalized not in seen:
                seen.add(normalized)
                paths.append(normalized)
        return paths

    def _is_non_rebuild_path(self, path: str) -> bool:
        lowered = (path or "").lower()
        if not lowered:
            return True
        if self._looks_like_test_file(lowered):
            return True
        if lowered.startswith(("doc/", "docs/", ".github/", "examples/")):
            return True
        if lowered.endswith((
            ".md",
            ".rst",
            ".txt",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".json",
            ".yml",
            ".yaml",
        )):
            return True
        return False

    def _test_commands_include_rebuild_step(self, test_commands: List[str]) -> bool:
        return any(self._is_rebuild_command(command, "") for command in test_commands or [])

    def _is_rebuild_command(self, command: str, language: str = "") -> bool:
        normalized = " ".join((command or "").strip().split())
        if not normalized:
            return False
        lowered = normalized.lower()

        excluded_prefixes = (
            "apt ",
            "apt-get ",
            "apk ",
            "yum ",
            "dnf ",
            "pip ",
            "pip3 ",
            "python -m pip ",
            "python3 -m pip ",
            "git ",
            "sed ",
            "perl ",
            "ln ",
            "chmod ",
            "chown ",
            "cp ",
            "mv ",
            "rm ",
        )
        if lowered.startswith(excluded_prefixes):
            return False

        rebuild_patterns = (
            r"\bcmake\b.*(?:\s-s\s|\s-b\s|--build\b)",
            r"^(?:make|gmake|ninja)\b",
            r"^(?:\./)?mvnw?\b.*\b(?:compile|test-compile|package|install|verify|test)\b",
            r"^(?:\./)?gradlew?\b.*\b(?:assemble|build|classes|testclasses|compilejava|compiletestjava|test)\b",
            r"^sbt\b.*\b(?:compile|test:compile|test)\b",
            r"^cargo\b.*\b(?:build|test|nextest)\b",
            r"^go\b.*\b(?:build|test|install)\b",
            r"^(?:npm|yarn|pnpm)\b.*\b(?:build|compile|test)\b",
            r"^meson\b.*\b(?:setup|compile|test)\b",
            r"^bazel\b.*\b(?:build|test)\b",
        )
        return any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in rebuild_patterns)

    def _is_build_artifact_publication_command(self, command: str) -> bool:
        """Detect commands that expose freshly rebuilt artifacts to the paths tests use."""
        normalized = " ".join((command or "").strip().split())
        if not normalized:
            return False
        lowered = normalized.lower()

        artifact_path_pattern = (
            r"(?<![a-z0-9_.-])(?:build|target|dist|out|\.build)/"
            r"|/(?:build|target|dist|out|\.build)/"
            r"|(?<![a-z0-9_.-/])bin/"
        )
        if not re.search(artifact_path_pattern, lowered):
            return False

        publication_segments = re.split(r"\s*(?:&&|;)\s*", lowered)
        publication_prefixes = (
            "cp ",
            "ln ",
            "install ",
            "rsync ",
        )
        return any(
            segment.strip().startswith(publication_prefixes)
            for segment in publication_segments
        )

    def _dedupe_preserve_order(self, commands: List[str]) -> List[str]:
        result = []
        seen = set()
        for command in commands or []:
            if command in seen:
                continue
            seen.add(command)
            result.append(command)
        return result
    
    def _assess_platform_support(self, instance: Dict[str, Any], language: str) -> Dict[str, Any]:
        """
        Detect benchmark instances that require a non-Linux toolchain/runtime.

        The current adapter only produces Linux container specs, so these instances
        must be skipped explicitly instead of being evaluated with misleading Linux tests.
        """
        problem_statement = instance.get("problem_statement", "")
        patch = instance.get("patch", "")
        test_patch = instance.get("test_patch", "")
        evidence_blob = "\n".join([problem_statement, patch, test_patch]).lower()

        windows_patterns = {
            "visual_studio_project": [
                r"\.vcproj\b",
                r"\.vcxproj\b",
            ],
            "msvc_toolchain": [
                r"\bmsvc\b",
                r"visual c\+\+",
                r"\bmsbuild\b",
                r"\bnmake\b",
                r"\bdevenv\b",
                r"\bappveyor\b",
                r"windowsservercore",
            ],
        }
        embedded_patterns = {
            "iar_toolchain": [
                r"\.ewp\b",
                r"\bembedded workbench\b",
                r"\biar\b",
                r"\bewarm\b",
            ],
        }
        macos_patterns = {
            "xcode_toolchain": [
                r"\.xcodeproj\b",
                r"\.xcworkspace\b",
                r"\bxcodebuild\b",
                r"\bcocoapods\b",
                r"\bpod install\b",
            ],
        }

        indicators: List[str] = []
        required_platform = "linux"

        for label, patterns in windows_patterns.items():
            if any(re.search(pattern, evidence_blob) for pattern in patterns):
                indicators.append(label)
        if indicators:
            required_platform = "windows"

        if not indicators:
            for label, patterns in embedded_patterns.items():
                if any(re.search(pattern, evidence_blob) for pattern in patterns):
                    indicators.append(label)
            if indicators:
                required_platform = "embedded"

        if not indicators:
            for label, patterns in macos_patterns.items():
                if any(re.search(pattern, evidence_blob) for pattern in patterns):
                    indicators.append(label)
            if indicators:
                required_platform = "macos"

        supported = not indicators
        reason = None
        if not supported:
            reason = (
                f"This instance appears to require a {required_platform}-specific build/test path "
                f"({', '.join(indicators)}), but the current adapter only generates Linux container evaluations."
            )

        return {
            "supported": supported,
            "detected_runtime": "linux",
            "required_platform": required_platform,
            "indicators": indicators,
            "reason": reason,
        }

    def _extract_test_command_from_setup_logs(self, workplace: str) -> Optional[str]:
        """
        从 setup_logs 中提取 Agent 实际验证成功的测试命令。
        策略：在含 'Final Answer: Success' 的日志中，提取 Final Answer 之前
        最后一次出现的测试命令（Agent 最终验证通过所用的那条），而非第一次出现的。
        """
        setup_logs_dir = Path(workplace) / "logs" / "setup_logs"
        if not setup_logs_dir.exists():
            setup_logs_dir = Path(workplace) / "setup_logs"
        if not setup_logs_dir.exists():
            return None

        # 按编号排序查找所有 setup log 文件
        log_files = sorted(
            (path for path in setup_logs_dir.glob("*.md") if path.stem.isdigit()),
            key=lambda x: int(x.stem),
        )

        # 匹配 Action 行中整条命令（取行内 Action: 后的全部内容，再后处理判断是否为测试命令）
        # 格式：Action: <cmd>  或  Action: `<cmd>`
        action_line_pattern = re.compile(
            r'^Action:\s*`?([^\n`]+?)`?\s*$',
            re.MULTILINE
        )
        # 测试命令关键词：只要命令含这些词之一，就认为是测试命令
        test_keywords = (
            'ctest', 'pytest', 'python -m pytest', 'python3 -m pytest',
            'make test', 'make check', 'npm test', 'bundle exec rake',
            'bundle exec rspec', 'go test', 'cargo test', 'mvn test',
            'vendor/bin/phpunit', 'run_all', 'run_tests',
            '--target test',  # cmake --build build --target test
        )
        # 排除纯查看/安装类命令（不是测试命令）
        exclude_keywords = (
            'cat ', 'ls ', 'find ', 'echo ', 'apt-get', 'pip install',
            'gem install', 'npm install', 'make -j',
        )

        for log_file in reversed(log_files):  # 从最新的开始查找
            content = log_file.read_text()
            # 仅处理包含成功验证的日志
            if "Final Answer: Success" not in content and "100% tests passed" not in content:
                continue

            # 截取 Final Answer 之前的内容，避免提取到 Final Answer 后面的无关内容
            success_pos = content.find("Final Answer: Success")
            if success_pos == -1:
                success_pos = len(content)
            content_before_success = content[:success_pos]

            # 找出所有 Action 行，过滤出测试命令，取最后一个（Agent 最终使用的）
            last_test_cmd = None
            for m in action_line_pattern.finditer(content_before_success):
                cmd = m.group(1).strip()
                cmd_lower = cmd.lower()
                # 检查是否含测试关键词
                is_test = any(kw in cmd_lower for kw in test_keywords)
                # 排除明显的非测试命令
                is_excluded = any(kw in cmd_lower for kw in exclude_keywords)
                if is_test and not is_excluded:
                    last_test_cmd = cmd
            if last_test_cmd:
                cmd = last_test_cmd
                # 清理多余空格
                cmd = re.sub(r'\s+', ' ', cmd)
                # 替换 Agent sandbox 路径 /app 为评估框架路径 /testbed
                cmd = self._normalize_agent_paths(cmd)
                print(f"  Extracted test command from setup_logs: {cmd}")
                return cmd
        return None

    def _load_run_summary(self, workplace: str) -> Optional[Dict[str, Any]]:
        """Load the structured runtime summary emitted by DockerAgent."""
        summary_file = Path(workplace) / "agent_run_summary.json"
        if not summary_file.exists():
            return None

        try:
            return json.loads(summary_file.read_text())
        except Exception as e:
            print(f"  Warning: Failed to read agent_run_summary.json: {e}")
            return None

    def _normalize_commands(self, commands: Optional[List[str]]) -> List[str]:
        """Drop empty entries while preserving order."""
        if isinstance(commands, str):
            commands = [commands]

        normalized_commands: List[str] = []
        for command in commands or []:
            if not command:
                continue
            stripped = command.strip()
            if stripped:
                normalized_commands.append(stripped)
        return normalized_commands

    def _load_agent_report_summary(self, workplace: str) -> Optional[Dict[str, Any]]:
        """Only trust verification data that came from an accepted agent-reported bundle."""
        summary = self._load_run_summary(workplace)
        if not summary:
            return None

        if summary.get("verification_source") != "agent_report":
            return None

        return summary

    def _extract_structured_runtime_preparation_commands(self, workplace: str) -> Tuple[List[str], Optional[str]]:
        """Read runtime preparation commands reported by DockerAgent."""
        summary = self._load_agent_report_summary(workplace)
        if not summary:
            return [], None

        bundle = summary.get("verification_bundle") or {}
        commands = self._normalize_commands(bundle.get("runtime_preparation_commands"))
        if commands:
            print(
                f"  Loaded runtime preparation commands from accepted verification bundle ({len(commands)}): {commands}"
            )
            return commands, "agent_report_verification_bundle"

        commands = self._normalize_commands(summary.get("verified_runtime_preparation_commands"))
        if commands:
            print(
                f"  Loaded structured runtime preparation command list ({len(commands)}): {commands}"
            )
            return commands, "agent_report_runtime_verified_runtime_preparation_commands"

        return [], None

    def _extract_structured_test_commands(self, workplace: str) -> Tuple[List[str], Optional[str]]:
        """Read test commands from an accepted agent-reported verification bundle."""
        summary = self._load_agent_report_summary(workplace)
        if not summary:
            return [], None

        bundle = summary.get("verification_bundle") or {}
        commands = self._normalize_commands(bundle.get("test_commands"))
        if commands:
            print(f"  Loaded test command list from accepted verification bundle ({len(commands)}): {commands}")
            return commands, "agent_report_verification_bundle"

        commands = self._normalize_commands(summary.get("verified_test_commands"))
        if commands:
            print(f"  Loaded structured test command list ({len(commands)}): {commands}")
            return commands, "agent_report_runtime_verified_test_commands"

        command = summary.get("verified_test_command")
        if command:
            print(f"  Loaded structured test command: {command}")
            return [command], "agent_report_runtime_verified_test_command"

        return [], None

    def _extract_structured_post_test_patch_commands(self, workplace: str) -> Tuple[List[str], Optional[str]]:
        """Read post-test-patch commands from the agent-synthesized build recipe."""
        summary = self._load_agent_report_summary(workplace)
        if not summary:
            return [], None

        recipe = summary.get("build_recipe") or {}
        commands = self._normalize_commands(recipe.get("post_test_patch_commands"))
        if commands:
            print(
                f"  Loaded post-test-patch command list from build recipe ({len(commands)}): {commands}"
            )
            return commands, "agent_report_build_recipe"

        return [], None

    def _resolve_test_commands(
        self,
        workplace: str,
        structured_test_command: Optional[str],
        structured_test_commands: Optional[List[str]],
    ) -> Tuple[List[str], str]:
        """Use only agent-selected final evaluation commands."""
        commands = self._normalize_commands(structured_test_commands)
        if commands:
            return commands, "agent_runtime_argument_list"

        if structured_test_command:
            return [structured_test_command], "agent_runtime_argument"

        commands, source = self._extract_structured_test_commands(workplace)
        if commands:
            return commands, source or "agent_report_summary"

        return [], "missing_agent_verification_bundle"

    def _resolve_runtime_preparation_commands(
        self,
        workplace: str,
        structured_runtime_preparation_commands: Optional[List[str]],
    ) -> Tuple[List[str], str]:
        """Use only agent-selected runtime preparation commands."""
        commands = self._normalize_commands(structured_runtime_preparation_commands)
        if commands:
            return commands, "agent_runtime_argument_list"

        commands, source = self._extract_structured_runtime_preparation_commands(workplace)
        if commands:
            return commands, source or "agent_report_summary"

        return [], "no_runtime_preparation_commands"

    def _generate_test_script(self, workplace: str, language: str,
                              problem_statement: str, test_patch: str,
                              dockerfile_content: str = "",
                              structured_runtime_preparation_commands: Optional[List[str]] = None,
                              structured_test_command: Optional[str] = None,
                              structured_test_commands: Optional[List[str]] = None,
                              structured_post_test_patch_commands: Optional[List[str]] = None,
                              allow_summary_fallback: bool = True) -> tuple:
        """
        生成测试脚本，并将 test_patch 注入 Dockerfile。
        优先使用 Agent 运行时记录的结构化测试命令，老数据再回退到 setup_logs。

        Returns:
            (eval_script, setup_scripts, updated_dockerfile)
        """
        self._last_eval_runtime_preparation_commands = []
        self._last_deferred_post_test_patch_commands = []
        self._last_filtered_test_commands = []
        self._last_refined_test_commands = []
        self._last_dropped_broad_test_commands = []
        # 优先使用运行时结构化记录，旧数据才回退到 setup_logs。
        if allow_summary_fallback:
            extracted_commands, command_source = self._resolve_test_commands(
                workplace,
                structured_test_command,
                structured_test_commands,
            )
        else:
            extracted_commands = self._normalize_commands(structured_test_commands)
            if not extracted_commands and structured_test_command:
                extracted_commands = [structured_test_command]
            command_source = "recipe_runtime_argument_list" if extracted_commands else "missing_recipe_test_commands"
        self._last_test_command_source = command_source
        if extracted_commands:
            base_commands = extracted_commands
            print(f"  Using {len(base_commands)} test command(s) from {command_source}: {base_commands}")
        else:
            print("  No accepted Verification Bundle test commands were found; skipping evaluation script generation.")
            self._last_runtime_preparation_source = None
            return "", {}, dockerfile_content

        base_commands = self._filter_test_commands_for_eval(
            base_commands,
            test_patch,
            reset_log=True,
        )
        base_commands = self._refine_test_commands_to_changed_targets(
            base_commands,
            test_patch,
            source_root=workplace,
            reset_log=True,
        )
        base_commands = self._adapt_pytest_commands_for_nested_pytester(
            base_commands,
            test_patch,
        )
        base_commands = self._select_target_covering_test_commands(
            base_commands,
            test_patch,
            source_root=workplace,
            reset_log=True,
        )
        if not base_commands:
            print("  Test command filtering removed all commands; skipping evaluation script generation.")
            self._last_runtime_preparation_source = None
            return "", {}, dockerfile_content

        if allow_summary_fallback:
            runtime_preparation_commands, runtime_source = self._resolve_runtime_preparation_commands(
                workplace,
                structured_runtime_preparation_commands,
            )
        else:
            runtime_preparation_commands = self._normalize_commands(
                structured_runtime_preparation_commands
            )
            runtime_source = (
                "recipe_runtime_argument_list"
                if runtime_preparation_commands
                else "no_runtime_preparation_commands"
            )
        self._last_runtime_preparation_source = runtime_source
        post_test_patch_commands = self._normalize_commands(structured_post_test_patch_commands)
        if post_test_patch_commands:
            self._last_post_test_patch_source = "agent_runtime_build_recipe"
        elif not allow_summary_fallback:
            self._last_post_test_patch_source = "no_post_test_patch_commands"
        else:
            post_test_patch_commands, post_source = self._extract_structured_post_test_patch_commands(
                workplace
            )
            self._last_post_test_patch_source = post_source or "no_post_test_patch_commands"
        post_test_patch_commands = self._normalize_cmake_build_targets_from_test_patch(
            post_test_patch_commands,
            test_patch,
        )
        post_test_patch_commands, deferred_post_test_patch_commands = (
            self._split_post_test_patch_commands_for_eval(post_test_patch_commands)
        )
        self._last_deferred_post_test_patch_commands = list(deferred_post_test_patch_commands)
        if deferred_post_test_patch_commands:
            runtime_preparation_commands = self._dedupe_preserve_order(
                runtime_preparation_commands + deferred_post_test_patch_commands
            )
        runtime_preparation_commands = self._filter_runtime_preparation_commands_for_eval(
            runtime_preparation_commands,
            test_patch,
        )

        # 根据工作目录调整命令路径
        # 如果命令包含 cd 到子目录，需要处理
        base_commands = [
            self._adjust_command_for_testbed(command)
            for command in base_commands
            if command
        ]
        runtime_preparation_commands = [
            self._adjust_command_for_testbed(command)
            for command in runtime_preparation_commands
            if command
        ]
        self._last_eval_runtime_preparation_commands = list(runtime_preparation_commands)
        post_test_patch_commands = [
            self._adjust_command_for_testbed(command)
            for command in post_test_patch_commands
            if command
        ]

        return self._build_eval_script(
            base_commands,
            language,
            test_patch,
            dockerfile_content,
            runtime_preparation_commands=runtime_preparation_commands,
            post_test_patch_commands=post_test_patch_commands,
        )

    def _split_post_test_patch_commands_for_eval(
        self,
        post_test_patch_commands: List[str],
    ) -> Tuple[List[str], List[str]]:
        """Keep patch rewrites in Docker build, but defer rebuilds until after source patch."""
        kept_commands = []
        deferred_commands = []
        defer_publication = False
        for command in post_test_patch_commands or []:
            if self._is_rebuild_command(command):
                deferred_commands.append(command)
                defer_publication = True
                continue
            if defer_publication and self._is_build_artifact_publication_command(command):
                deferred_commands.append(command)
                continue
            kept_commands.append(command)
            defer_publication = False
        return kept_commands, deferred_commands

    def _normalize_cmake_build_targets_from_test_patch(
        self,
        commands: List[str],
        test_patch: str,
    ) -> List[str]:
        """Map wrapper script names to actual CMake targets introduced by test patches."""
        cmake_targets = self._extract_added_cmake_targets(test_patch)
        if not cmake_targets:
            return commands

        normalized = []
        for command in commands or []:
            if "cmake" not in (command or "") or "--target" not in command:
                normalized.append(command)
                continue

            def replace_target_group(match):
                prefix = match.group(1)
                target_group = match.group(2)
                rewritten_targets = []
                for target in target_group.split():
                    candidate = target[:-2] if target.endswith(".t") else target
                    if target.endswith(".t") and candidate in cmake_targets:
                        rewritten_targets.append(candidate)
                    else:
                        rewritten_targets.append(target)
                return prefix + " ".join(rewritten_targets)

            normalized.append(
                re.sub(
                    r"(--target\s+)([^;&|\n]+)",
                    replace_target_group,
                    command,
                    count=1,
                )
            )
        return normalized

    def _extract_added_cmake_targets(self, test_patch: str) -> set:
        targets = set()
        in_cmake_file = False
        for line in (test_patch or "").splitlines():
            file_match = re.match(r"^\+\+\+ b/([^\t\r\n]+)", line)
            if file_match:
                in_cmake_file = file_match.group(1).endswith("CMakeLists.txt")
                continue
            if not in_cmake_file or not line.startswith("+") or line.startswith("+++"):
                continue

            body = line[1:].strip()
            executable_match = re.search(
                r"\badd_executable\s*\(\s*([A-Za-z0-9_.+-]+)",
                body,
                flags=re.IGNORECASE,
            )
            if executable_match:
                targets.add(executable_match.group(1))

            set_match = re.search(
                r"\bset\s*\(\s*[A-Za-z0-9_]+\s+([^)]*)\)",
                body,
                flags=re.IGNORECASE,
            )
            if set_match:
                for token in set_match.group(1).split():
                    if re.match(r"^[A-Za-z0-9_.+-]+$", token):
                        targets.add(token)
        return targets

    def _adjust_command_for_testbed(self, command: str) -> str:
        """
        调整命令，确保在 /testbed 目录下正确执行。
        处理相对路径问题。
        """
        if not command:
            return command

        command = self._normalize_agent_paths(command)
        command = quote_shell_sensitive_package_specs(command)

        # 如果命令以 cd 开头，允许它在 /testbed 或子目录中自行切换。
        if command.startswith("cd "):
            return command

        # 相对路径可执行文件保持不变，依赖前面的 `cd /testbed` 作为工作目录。
        return command

    def _build_eval_script(
        self,
        base_commands: List[str],
        language: str,
        test_patch: str,
        dockerfile_content: str,
        runtime_preparation_commands: Optional[List[str]] = None,
        post_test_patch_commands: Optional[List[str]] = None,
    ) -> tuple:
        """
        构建最终的 eval_script，处理 runtime preparation 和 test_patch 注入。

        Returns:
            (eval_script, setup_scripts, updated_dockerfile)
        """
        runtime_preparation_commands = runtime_preparation_commands or []
        runtime_service_setup = self._build_runtime_preparation_block(runtime_preparation_commands)
        post_test_patch_commands = post_test_patch_commands or []
        eval_commands = list(base_commands)
        command_block = " && \\\n".join(f"(\n{command}\n)" for command in eval_commands)

        eval_script = f"""#!/bin/bash

cd /testbed

{runtime_service_setup}
cd /testbed

set +e
{command_block}
TEST_EXIT_CODE=$?
set -e

echo "echo OMNIGRIL_EXIT_CODE=$TEST_EXIT_CODE"
exit $TEST_EXIT_CODE
"""

        # 若有 test_patch 且有 Dockerfile，将 test_patch 注入镜像 build context
        setup_scripts = {}
        updated_dockerfile = ""
        if test_patch and dockerfile_content:
            setup_scripts["test.patch"] = test_patch
            setup_scripts["apply_test_patch.sh"] = self._build_test_patch_apply_script()

            # 在 Dockerfile 中找到最后一个 RUN 行后插入 test.patch 相关命令
            lines = dockerfile_content.split('\n')
            new_lines = []
            for line in lines:
                new_lines.append(line)
            
            # 添加 test.patch 处理
            new_lines.append("")
            new_lines.append("# Apply test patch")
            new_lines.append("COPY test.patch /tmp/test.patch")
            new_lines.append("COPY apply_test_patch.sh /tmp/apply_test_patch.sh")
            new_lines.append("RUN chmod +x /tmp/apply_test_patch.sh && /bin/bash /tmp/apply_test_patch.sh")

            if post_test_patch_commands:
                post_patch_script_name = "post_test_patch_commands.sh"
                setup_scripts[post_patch_script_name] = self._build_post_test_patch_script(
                    post_test_patch_commands
                )
                new_lines.append("COPY post_test_patch_commands.sh /tmp/post_test_patch_commands.sh")
                new_lines.append(
                    "RUN chmod +x /tmp/post_test_patch_commands.sh && "
                    "/bin/bash /tmp/post_test_patch_commands.sh"
                )
                for command in post_test_patch_commands:
                    print(f"  Running agent-specified post-test-patch command: {command}")
            
            updated_dockerfile = '\n'.join(new_lines)
            print("✓ test_patch injected into Dockerfile (baked into image)")

        return eval_script, setup_scripts, updated_dockerfile

    def _build_post_test_patch_script(self, post_test_patch_commands: List[str]) -> str:
        """Build a shell script for post-patch commands so multiline commands stay valid."""
        normalized_commands = [
            self._normalize_shell_command_for_docker_run(command)
            for command in post_test_patch_commands
            if command and command.strip()
        ]
        command_block = "\n\n".join(normalized_commands)
        return f"""#!/bin/bash
set -euo pipefail

cd /testbed

{command_block}
"""

    def _build_runtime_preparation_block(self, runtime_preparation_commands: List[str]) -> str:
        """Run agent-verified runtime preparation commands before the final tests."""
        if not runtime_preparation_commands:
            return ""

        return (
            "# Runtime preparation commands verified by the setup agent\n"
            "set -e\n"
            f"{chr(10).join(runtime_preparation_commands)}\n"
            "set +e\n"
        )

    def _normalize_run_instruction_for_docker(self, instruction: str) -> str:
        """Rewrite bash-only snippets into POSIX-compatible RUN instructions."""
        if not instruction.startswith("RUN "):
            return instruction

        command = instruction[4:]
        normalized_command = self._normalize_shell_command_for_docker_run(command)
        return f"RUN {normalized_command}"

    def _prepare_agent_run_instruction_for_eval(self, instruction: str, sequence: int) -> str:
        """Keep multiline RUN instructions valid when replayed in a fresh Dockerfile."""
        if "\n" not in (instruction or "") or not instruction.startswith("RUN "):
            return instruction

        command = instruction[4:]
        encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
        script_path = f"/tmp/jayint_eval_run_{sequence}.sh"
        return (
            f"RUN printf '%s' {self._shell_single_quote(encoded)} "
            f"| base64 -d > {script_path} "
            f"&& chmod +x {script_path} "
            f"&& /bin/sh {script_path}"
        )

    def _normalize_shell_command_for_docker_run(self, command: str) -> str:
        """Docker RUN uses /bin/sh by default, so avoid bash-only `source`."""
        if not command:
            return command
        command = self._normalize_agent_paths(command)
        command = quote_shell_sensitive_package_specs(command)
        return re.sub(r"(^|(?:&&|\|\||;)\s*)source\s+", r"\1. ", command)

    def _format_exception(self, exc: Exception) -> str:
        if isinstance(exc, subprocess.CalledProcessError):
            stdout = (exc.stdout or b"").decode(errors="replace")
            stderr = (exc.stderr or b"").decode(errors="replace")
            details = "\n".join(part for part in [stdout, stderr] if part.strip())
            if details:
                return f"{exc}\n{details}"
        return str(exc)

    def _is_infrastructure_failure(self, text: str) -> bool:
        lowered = (text or "").lower()
        indicators = [
            "connection reset",
            "recv failure",
            "could not resolve",
            "temporary failure resolving",
            "failed to fetch",
            "502 bad gateway",
            "503 service unavailable",
            "tls handshake",
            "operation timed out",
            "network is unreachable",
            "early eof",
            "the remote end hung up unexpectedly",
        ]
        if any(indicator in lowered for indicator in indicators):
            return True
        return "returned a non-zero code: 100" in lowered and "apt-get update" in lowered

    def _build_test_patch_apply_script(self) -> str:
        """Build a strict test patch applicator for the Docker build context."""
        return """#!/bin/bash
set -euo pipefail

cd /testbed

echo "[test_patch] validating patch with git apply --check"
if git apply --check /tmp/test.patch; then
    echo "[test_patch] git apply --check passed"
    git apply /tmp/test.patch
    echo "[test_patch] git apply succeeded"
    exit 0
fi

echo "[test_patch] git apply --check failed, trying patch fallback"
if command -v patch >/dev/null 2>&1; then
    if patch --batch --fuzz=5 -p1 -i /tmp/test.patch; then
        echo "[test_patch] patch fallback succeeded"
        exit 0
    fi
    echo "[test_patch] patch fallback failed"
else
    echo "[test_patch] patch command is not available for fallback"
fi

echo "[test_patch] unable to apply /tmp/test.patch"
exit 1
"""
    
    def _save_result(self, instance_id: str, result: Dict[str, Any]):
        """保存结果到文件"""
        output_file = self.output_dir / f"{instance_id}.json"
        with open(output_file, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\nResult saved to: {output_file}")
    
    def process_dataset(self, dataset_path: str, 
                       base_image: str = "auto",
                       model: str = DEFAULT_LLM_MODEL,
                       max_steps: int = 30,
                       enable_observation_compression: bool = False,
                       enable_long_term_memory: bool = False,
                       memory_path: Optional[str] = None,
                       memory_embedding_model: str = DEFAULT_MEMORY_EMBEDDING_MODEL,
                       enable_artifact_preflight: bool = True,
                       artifact_repair_rounds: int = 1,
                       limit: Optional[int] = None) -> str:
        """
        批量处理数据集
        
        Args:
            dataset_path: JSONL 格式的数据集路径
            base_image: Docker 基础镜像
            model: LLM 模型
            max_steps: 每个实例的最大步骤数
            limit: 限制处理的实例数量(用于测试)
            
        Returns:
            汇总结果文件路径
        """
        results = []
        
        with open(dataset_path, 'r') as f:
            instances = [json.loads(line) for line in f]
        
        if limit:
            instances = instances[:limit]
        
        print(f"Processing {len(instances)} instances from {dataset_path}")
        
        for i, instance in enumerate(instances, 1):
            print(f"\n{'#'*60}")
            print(f"Instance {i}/{len(instances)}")
            print(f"{'#'*60}")
            
            result = self.process_single_instance(
                instance=instance,
                base_image=base_image,
                model=model,
                max_steps=max_steps,
                enable_observation_compression=enable_observation_compression,
                enable_long_term_memory=enable_long_term_memory,
                memory_path=memory_path,
                memory_embedding_model=memory_embedding_model,
                enable_artifact_preflight=enable_artifact_preflight,
                artifact_repair_rounds=artifact_repair_rounds,
            )
            results.append(result)
        
        # 保存汇总结果（评估框架期望字典格式，以 instance_id 为 key）
        summary_file = self.output_dir / "docker_res.json"
        docker_res_dict = {
            r["instance_id"]: r
            for r in results
            if not r["logs"].get("skip_evaluation") and r.get("dockerfile") and r.get("eval_script")
        }
        with open(summary_file, "w") as f:
            json.dump(docker_res_dict, f, indent=2)
        
        # 打印统计信息
        total = len(results)
        build_success = sum(1 for r in results if r["build_success"])
        skipped = sum(1 for r in results if r["logs"].get("skip_evaluation"))
        evaluable = len(docker_res_dict)
        
        print(f"\n{'='*60}")
        print("SUMMARY")
        print(f"{'='*60}")
        print(f"Total instances: {total}")
        print(f"Evaluable instances: {evaluable}/{total}")
        print(f"Skipped instances: {skipped}/{total}")
        print(f"Build success: {build_success}/{total} ({100*build_success/total:.1f}%)")
        print(f"Results saved to: {summary_file}")
        print(f"{'='*60}\n")
        
        return str(summary_file)


def main():
    parser = argparse.ArgumentParser(
        description="Multi-Docker-Eval Adapter for DockerAgent"
    )
    parser.add_argument(
        "dataset",
        help="Path to Multi-Docker-Eval dataset (JSONL format)"
    )
    parser.add_argument(
        "--output-dir",
        default="./multi_docker_eval_output",
        help="Output directory for results"
    )
    parser.add_argument(
        "--base-image",
        default="auto",
        help="Default Docker base image"
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_LLM_MODEL,
        help="LLM model to use"
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=30,
        help="Maximum steps per instance"
    )
    parser.add_argument(
        "--enable-observation-compression",
        action="store_true",
        help="Enable AgentDiet-style observation compression"
    )
    parser.add_argument(
        "--enable-long-term-memory",
        action="store_true",
        help="Enable failure-triggered long-term memory retrieval and post-run memory writing"
    )
    parser.add_argument(
        "--memory-path",
        default=None,
        help="Path to the JSONL long-term memory store"
    )
    parser.add_argument(
        "--memory-embedding-model",
        default=DEFAULT_MEMORY_EMBEDDING_MODEL,
        help=f"Embedding model for long-term memory (default: {DEFAULT_MEMORY_EMBEDDING_MODEL})"
    )
    parser.add_argument(
        "--disable-artifact-preflight",
        action="store_true",
        help="Disable adapter-side Docker artifact preflight and LLM recipe repair"
    )
    parser.add_argument(
        "--artifact-repair-rounds",
        type=int,
        default=1,
        help="Maximum LLM recipe repair rounds after artifact preflight failure (default: 1)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of instances to process (for testing)"
    )
    
    args = parser.parse_args()
    
    adapter = MultiDockerEvalAdapter(output_dir=args.output_dir)
    adapter.process_dataset(
        dataset_path=args.dataset,
        base_image=args.base_image,
        model=args.model,
        max_steps=args.max_steps,
        enable_observation_compression=args.enable_observation_compression,
        enable_long_term_memory=args.enable_long_term_memory,
        memory_path=args.memory_path,
        memory_embedding_model=args.memory_embedding_model,
        enable_artifact_preflight=not args.disable_artifact_preflight,
        artifact_repair_rounds=args.artifact_repair_rounds,
        limit=args.limit
    )


if __name__ == "__main__":
    main()
