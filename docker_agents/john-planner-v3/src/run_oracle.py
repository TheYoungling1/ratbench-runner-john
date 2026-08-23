"""RunOracle — zero-dependency extraction of the test-classification logic.

All methods are verbatim copies of the corresponding members of
``Synthesizer`` in src/synthesizer.py.  This file carries NO imports from
src.synthesizer or any other src.* module so that the v3-only branch can
delete synthesizer.py without touching anything that imports RunOracle.
"""
from __future__ import annotations

import re
import shlex


class RunOracle:
    """Classifies shell commands and test-run observations.

    Extracted from ``Synthesizer``.  All logic is stateless — the __init__
    is a no-op and every method only reads ``self`` for class-level constants.
    """

    TEST_COMMAND_PATTERNS = [
        # Python
        r"^pytest\b",
        r"^py\.test\b",
        r"^python3?\s+-m\s+pytest\b",
        r"^(?:poetry|pdm|uv)\s+run\s+pytest\b",
        r"^(?:poetry|pdm|uv)\s+run\s+python3?\s+-m\s+pytest\b",
        r"^python3?\s+-m\s+unittest\b",
        r"^tox\b",
        r"^nox\b",
        r"^nosetests\b",
        r"^nose\b",
        # JavaScript / TypeScript
        r"^(?:npm|yarn|pnpm)\s+test\b",
        r"^jest\b",
        r"^mocha\b",
        r"^karma\b",
        r"^vitest\b",
        r"^cypress\b",
        # Rust / Go / Java / Ruby / PHP
        r"^cargo\s+test\b",
        r"^go\s+test\b",
        r"^(?:mvn|\.?/mvnw)\s+test\b",
        r"^(?:gradle|\.?/gradlew)\s+test\b",
        r"^bundle\s+exec\s+rspec\b",
        r"^bundle\s+exec\s+rake\b",
        r"^rake\s+test\b",
        r"^rspec\b",
        r"^(?:\./)?(?:vendor/bin/)?phpunit\b",
        r"^(?:\./)?(?:vendor/bin/)?pest\b",
        # C / C++
        r"^ctest\b",
        r"^cmake\b.*\b--target\b\s*(?:test|tests)\b",
        r"^(?:make|gmake|mingw32-make|ninja)\b.*\b(?:test|tests|check|tdd)\b",
    ]
    SAFE_READONLY_COMMANDS = {
        "cd", "ls", "cat", "echo", "pwd", "whoami", "who", "date", "cal", "df", "du",
        "free", "uname", "uptime", "w", "ps", "pgrep", "top", "dmesg", "tail", "head",
        "grep", "find", "locate", "which", "file", "stat", "cmp", "diff", "xz", "unxz",
        "sort", "wc", "tr", "cut", "paste", "tee", "awk", "env", "printenv", "hostname",
        "xargs",
        "ping", "traceroute", "ssh",
    }
    VERSION_PROBE_COMMANDS = {
        "php", "composer",
        "python", "python3", "pip", "pip3",
        "node", "npm", "yarn", "pnpm",
        "java", "javac", "mvn", "gradle", "gradlew",
        "go", "rustc", "cargo",
        "ruby", "gem", "bundle",
        "gcc", "g++", "cc", "c++", "make", "cmake", "ninja",
        "git",
    }

    def __init__(self):
        pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_test_command(self, command):
        """判断指令是否是测试命令。"""
        if not command or not command.strip():
            return False

        # Read-only commands such as `echo "tests passed"` must never be treated as test runs.
        if self._is_readonly_command(command):
            return False

        for _, normalized in self._iter_command_segments(command):
            if self._is_test_like_segment(normalized):
                return True

        return False

    def analyze_test_run(self, command, observation=""):
        """Judge whether a successful command actually executed meaningful tests."""
        result = {
            "is_test_command": False,
            "is_effective_test_run": False,
            "confidence": "none",
            "reason": "not_test_command",
        }

        if not self.is_test_command(command):
            return result

        result["is_test_command"] = True

        if self._observation_looks_like_help_text(observation):
            result["reason"] = "help_or_usage_output"
            return result

        if self.is_truncated_test_output_command(command):
            result["reason"] = "truncated_test_output"
            return result

        if self._observation_has_test_failure_signal(observation):
            result["reason"] = "test_failure_signal"
            return result

        if self._observation_has_effective_test_signal(observation):
            result["is_effective_test_run"] = True
            result["confidence"] = "high"
            result["reason"] = "observed_test_execution_signal"
            return result

        # Some runners (notably `go test ./...`) can mix real package results with
        # informational lines such as `[no test files]`. Treat explicit positive
        # execution signals as authoritative before falling back to empty-run hints.
        if self._observation_has_empty_test_run_signal(observation):
            result["reason"] = "no_tests_executed"
            return result

        if observation and any(
            self._looks_like_test_executable(normalized)
            for _, normalized in self._iter_command_segments(command)
        ):
            result["is_effective_test_run"] = True
            result["confidence"] = "medium"
            result["reason"] = "direct_test_executable_with_output"
            return result

        result["reason"] = "no_reliable_test_execution_signal"
        return result

    def is_truncated_test_output_command(self, command):
        """Return True when a test command pipes output through a lossy filter."""
        if not command or not command.strip():
            return False

        for raw_segment, _ in self._split_shell_chain(command):
            pipeline_components = self._split_pipeline(raw_segment)
            if len(pipeline_components) < 2:
                continue

            saw_test_component = False
            for component in pipeline_components:
                normalized = self._normalize_command_segment(component)
                if not normalized:
                    continue

                if self._is_test_like_segment(normalized):
                    saw_test_component = True
                    continue

                if saw_test_component and self._is_output_truncation_component(normalized):
                    return True

        return False

    # ------------------------------------------------------------------
    # Private helpers — shell parsing
    # ------------------------------------------------------------------

    def _iter_command_segments(self, command):
        """Yield normalized shell command segments split on common separators."""
        for segment, _ in self._split_shell_chain(command):
            normalized = self._normalize_command_segment(segment)
            if normalized:
                yield segment.strip(), normalized

    def _split_shell_chain(self, command):
        """Split a shell command into ordered segments while respecting quotes, escapes, and heredocs."""
        segments = []
        current = []
        in_single = False
        in_double = False
        escape = False
        index = 0

        while index < len(command):
            char = command[index]

            if escape:
                current.append(char)
                escape = False
                index += 1
                continue

            if char == "\\":
                current.append(char)
                escape = True
                index += 1
                continue

            if char == "'" and not in_double:
                in_single = not in_single
                current.append(char)
                index += 1
                continue

            if char == '"' and not in_single:
                in_double = not in_double
                current.append(char)
                index += 1
                continue

            if not in_single and not in_double:
                if command.startswith("&&", index) or command.startswith("||", index):
                    raw_segment = "".join(current).strip()
                    separator = command[index:index + 2]
                    if raw_segment:
                        segments.append((raw_segment, separator))
                    current = []
                    index += 2
                    continue

                if char in {";", "\n"}:
                    if char == "\n":
                        heredoc_descriptor = self._extract_heredoc_descriptor("".join(current))
                        if heredoc_descriptor is not None:
                            delimiter, strip_tabs = heredoc_descriptor
                            current.append(char)
                            index += 1
                            index = self._consume_heredoc_body(
                                command,
                                index,
                                current,
                                delimiter,
                                strip_tabs=strip_tabs,
                            )
                            raw_segment = "".join(current).strip()
                            if raw_segment:
                                separator = "\n" if index < len(command) else ""
                                segments.append((raw_segment, separator))
                            current = []
                            continue

                    raw_segment = "".join(current).strip()
                    if raw_segment:
                        segments.append((raw_segment, char))
                    current = []
                    index += 1
                    continue

            current.append(char)
            index += 1

        raw_segment = "".join(current).strip()
        if raw_segment:
            segments.append((raw_segment, ""))
        return segments

    def _extract_heredoc_descriptor(self, segment):
        match = re.search(
            r"<<(?P<strip>-)?\s*(?P<quote>['\"]?)(?P<delimiter>[^\s'\"`]+)(?P=quote)\s*$",
            segment or "",
        )
        if not match:
            return None
        return match.group("delimiter"), bool(match.group("strip"))

    def _consume_heredoc_body(self, command, index, current, delimiter, strip_tabs=False):
        while index < len(command):
            next_newline = command.find("\n", index)
            if next_newline == -1:
                line = command[index:]
                current.append(line)
                return len(command)

            line = command[index:next_newline]
            current.append(line)
            current.append("\n")
            index = next_newline + 1

            candidate = line.lstrip("\t") if strip_tabs else line
            if candidate == delimiter:
                return index

        return index

    def _split_pipeline(self, segment):
        """Split a shell segment on single-pipe operators while respecting quotes/escapes."""
        components = []
        current = []
        in_single = False
        in_double = False
        escape = False
        index = 0

        while index < len(segment):
            char = segment[index]

            if escape:
                current.append(char)
                escape = False
                index += 1
                continue

            if char == "\\":
                current.append(char)
                escape = True
                index += 1
                continue

            if char == "'" and not in_double:
                in_single = not in_single
                current.append(char)
                index += 1
                continue

            if char == '"' and not in_single:
                in_double = not in_double
                current.append(char)
                index += 1
                continue

            if (
                not in_single
                and not in_double
                and char == "|"
                and not segment.startswith("||", index)
            ):
                component = "".join(current).strip()
                if component:
                    components.append(component)
                current = []
                index += 1
                continue

            current.append(char)
            index += 1

        component = "".join(current).strip()
        if component:
            components.append(component)
        return components

    def _has_output_redirection(self, command):
        in_single = False
        in_double = False
        escape = False

        for char in command:
            if escape:
                escape = False
                continue

            if char == "\\":
                escape = True
                continue

            if char == "'" and not in_double:
                in_single = not in_single
                continue

            if char == '"' and not in_single:
                in_double = not in_double
                continue

            if not in_single and not in_double and char == ">":
                return True

        return False

    def _pipeline_component_is_safe_readonly(self, normalized_command):
        if self._is_version_probe_segment(normalized_command):
            return True
        if self._is_package_manager_inspection_segment(normalized_command):
            return True
        if self._is_python_readonly_probe_segment(normalized_command):
            return True

        executable = normalized_command.split()[0]
        executable = executable.strip("\"'`")
        executable_name = executable.rsplit("/", 1)[-1]
        return executable_name in self.SAFE_READONLY_COMMANDS

    def _is_package_manager_inspection_segment(self, normalized_command):
        normalized = self._strip_dev_null_redirections(normalized_command).strip()
        patterns = (
            r"^(?:python3?|/[\w./-]*python3?)\s+-m\s+pip\s+(?:list|show|freeze|check|index\s+versions)\b",
            r"^(?:pip3?|/[\w./-]*pip3?)\s+(?:list|show|freeze|check|index\s+versions)\b",
            r"^uv\s+pip\s+(?:list|show|freeze|check)\b",
            r"^(?:poetry|pdm)\s+(?:show|list)\b",
            r"^(?:npm|yarn|pnpm)\s+(?:list|ls|why)\b",
            r"^conda\s+(?:list|info)\b",
        )
        return any(re.match(pattern, normalized) for pattern in patterns)

    def _is_python_readonly_probe_segment(self, normalized_command):
        try:
            parts = shlex.split(normalized_command)
        except ValueError:
            return False
        if len(parts) < 3:
            return False

        executable = parts[0].strip("\"'`")
        executable_name = executable.rsplit("/", 1)[-1]
        if executable_name not in {"python", "python3"}:
            return False
        if "-c" not in parts:
            return False

        code_index = parts.index("-c") + 1
        if code_index >= len(parts):
            return False
        code = parts[code_index].strip()
        if not code:
            return False

        dangerous_markers = (
            "=",
            "open(",
            ".write",
            "write_text(",
            "touch(",
            "mkdir(",
            "unlink(",
            "remove(",
            "rmdir(",
            "rename(",
            "replace(",
            "chmod(",
            "chown(",
            "shutil",
            "subprocess",
            "os.system",
            "os.popen",
            "pip ",
            "pip.",
            "install",
            "exec(",
            "eval(",
            "__import__",
            "importlib",
        )
        lowered_code = code.lower()
        if any(marker in lowered_code for marker in dangerous_markers):
            return False

        statements = [part.strip() for part in re.split(r"[;\n]", code) if part.strip()]
        if not statements:
            return False

        imported_names = set()
        for statement in statements:
            if statement.startswith("import "):
                imported_names.update(self._extract_imported_python_names(statement))
            elif statement.startswith("from "):
                imported_names.update(self._extract_from_imported_python_names(statement))

        safe_call_pattern = re.compile(
            r"^(?:print|dir|getattr|hasattr|len|repr|str|type|bool)\s*\(",
            re.IGNORECASE,
        )
        for statement in statements:
            if statement.startswith(("import ", "from ")):
                continue
            if safe_call_pattern.match(statement):
                if not imported_names:
                    return False
                if not any(re.search(rf"\b{re.escape(name)}\b", statement) for name in imported_names):
                    return False
                continue
            return False
        return True

    def _extract_imported_python_names(self, statement):
        imported = statement[len("import "):]
        names = set()
        for item in imported.split(","):
            item = item.strip()
            if not item:
                continue
            if " as " in item:
                name = item.rsplit(" as ", 1)[-1].strip()
            else:
                name = item.split(".", 1)[0].strip()
            if name and re.match(r"^[a-z_][a-z0-9_]*$", name):
                names.add(name)
        return names

    def _extract_from_imported_python_names(self, statement):
        if " import " not in statement:
            return set()
        imported = statement.split(" import ", 1)[1]
        names = set()
        for item in imported.split(","):
            item = item.strip()
            if not item or item == "*":
                continue
            if " as " in item:
                name = item.rsplit(" as ", 1)[-1].strip()
            else:
                name = item.split(".", 1)[0].strip()
            if name and re.match(r"^[a-z_][a-z0-9_]*$", name):
                names.add(name)
        return names

    def _strip_dev_null_redirections(self, command):
        """Ignore harmless output silencing when classifying read-only probes."""
        if not command:
            return ""

        stripped = re.sub(r"\s+(?:[12]?>|&>)\s*/dev/null\b", "", command)
        stripped = re.sub(r"\s+(?:[12]?>|&>)/dev/null\b", "", stripped)
        return stripped

    def _is_version_probe_segment(self, normalized_command):
        normalized = self._strip_dev_null_redirections(normalized_command).strip()
        if not normalized:
            return False

        parts = normalized.split()
        if len(parts) != 2:
            return False

        executable = parts[0].strip("\"'`")
        executable_name = executable.rsplit("/", 1)[-1]
        if executable_name not in self.VERSION_PROBE_COMMANDS:
            return False

        return parts[1] in {"--version", "-v", "version"}

    def _normalize_command_segment(self, segment):
        normalized = segment.strip().lower()
        if not normalized:
            return ""

        normalized = re.sub(
            r"^(?:[a-z_][a-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S+)\s+)+",
            "",
            normalized,
        )
        normalized = re.sub(r"^time\s+", "", normalized)
        return normalized.strip()

    def _segment_matches_test_pattern(self, normalized_command, test_patterns):
        return any(re.match(pattern, normalized_command) for pattern in test_patterns)

    def _is_test_like_segment(self, normalized_command):
        return (
            self._segment_matches_test_pattern(normalized_command, self.TEST_COMMAND_PATTERNS)
            or self._looks_like_test_executable(normalized_command)
        )

    def _is_output_truncation_component(self, normalized_command):
        if not normalized_command:
            return False

        executable = normalized_command.split()[0]
        executable = executable.strip("\"'`")
        executable_name = executable.rsplit("/", 1)[-1]
        return executable_name in {"head", "tail", "grep", "egrep", "fgrep"}

    def _is_readonly_command(self, command):
        """Treat safe inspection/search commands as read-only when they do not redirect output."""
        if not command or not command.strip():
            return False
        if self._has_output_redirection(self._strip_dev_null_redirections(command)):
            return False

        saw_component = False
        for raw_segment, _ in self._split_shell_chain(command):
            for component in self._split_pipeline(raw_segment):
                normalized = self._normalize_command_segment(component)
                if not normalized:
                    continue
                if not self._pipeline_component_is_safe_readonly(normalized):
                    return False
                saw_component = True
        return saw_component

    def _looks_like_test_executable(self, normalized_command):
        """Detect direct execution of built test binaries such as ./FooTests."""
        if not normalized_command:
            return False

        executable = normalized_command.split()[0]
        if not (executable.startswith("./") or executable.startswith("/") or "/" in executable):
            return False

        basename = executable.rsplit("/", 1)[-1]
        if basename in {"configure", "install-sh", "test-driver"}:
            return False

        test_suffixes = (
            "test",
            "tests",
            "unittest",
            "unittests",
            "spec",
            "specs",
            "test.exe",
            "tests.exe",
            "spec.exe",
            "specs.exe",
        )
        if basename.endswith(test_suffixes):
            return True

        return bool(
            re.search(r"(test|tests|unittest|unittests|spec|specs)", basename)
            and ("/test" in executable or "/tests" in executable or executable.startswith("./"))
        )

    def _observation_has_empty_test_run_signal(self, observation):
        """Detect successful commands that clearly did not run any tests."""
        if not observation:
            return False

        normalized = self._normalize_observation_text(observation).lower()
        empty_run_patterns = [
            r"no tests were found",
            r"no tests found",
            r"collected\s+0\s+items",
            r"ran\s+0\s+tests?",
            r"\b0\s+tests?\s+ran\b",
            r"\[no test files\]",
            r"no test cases matched",
            r"no tests to run",
            r"\b0\s+examples?,\s+0\s+failures?\b",
        ]
        return any(re.search(pattern, normalized, re.MULTILINE) for pattern in empty_run_patterns)

    def _observation_has_test_failure_signal(self, observation):
        """Detect output summaries that report nonzero test failures or errors."""
        if not observation:
            return False

        normalized_observation = self._normalize_observation_text(observation)
        failure_patterns = [
            r"\b[1-9]\d*[ \t]+(?:failed|failures?|errors?)\b",
            r"\b[1-9]\d*[ \t]+tests?[ \t]+failed\b",  # ctest "N tests failed" (audit [7])
            r"\b(?:failures?|errors?):\s*[1-9]\d*\b",
            r"\btests?[ \t]+run:\s*\d+,\s*failures?:\s*[1-9]\d*\b",
            r"\btests?[ \t]+run:\s*\d+,\s*failures?:\s*\d+,\s*errors?:\s*[1-9]\d*\b",
            r"\btest result:\s+failed\b",
            r"^\s*not ok\b",
            r"^\s*(?:FAILED|ERROR)\s+\S+",
            r"\bBUILD FAILURE\b",
            r"\bthere (?:was|were)\s+[1-9]\d*\s+(?:failure|failures|error|errors)\b",
        ]
        return any(
            re.search(pattern, normalized_observation, re.IGNORECASE | re.MULTILINE)
            for pattern in failure_patterns
        )

    def _observation_has_effective_test_signal(self, observation):
        """Detect observation text that strongly suggests real tests were executed."""
        if not observation:
            return False

        normalized_observation = self._normalize_observation_text(observation)
        positive_patterns = [
            r"collected\s+[1-9]\d*\s+items",
            r"\b[1-9]\d*\s+tests?\s+collected\b",
            r"ran\s+[1-9]\d*\s+tests?",
            r"\b[1-9]\d*\s+passed\b",
            r"\b[1-9]\d*\s+failed\b",
            r"\b[1-9]\d*\s+skipped\b",
            r"tests\s+run:\s*[1-9]\d*,\s*failures:\s*\d+,\s*errors:\s*\d+,\s*skipped:\s*\d+",
            r"\bok\s+\([1-9]\d*\s+tests?,",
            r"\b[1-9]\d*\s+tests?,\s+[1-9]\d*\s+ran\b",
            r"\[=+\]\s+running\s+[1-9]\d*\s+tests?",
            r"test result:\s+(?:ok|failed)\.",
            r"\b[1-9]\d*%\s+tests\s+passed\b",
            r"^\s*ok\s+\S+\s+\d+(?:\.\d+)?s(?:\s|$)",
            r"\b[1-9]\d*\s+examples?,\s+\d+\s+failures?\b",
            r"\b[1-9]\d*\s+checks?,\s+\d+\s+ignored\b",
            r"^\s*passed:\s*[1-9]\d*\b",
            r"start\s+\d+:",
            r"suites:\s+\d+\s+of\s+[1-9]\d*\s+completed",
            r"asserts:\s+\d+\s+of\s+[1-9]\d*",
            r"^\s*#\s*subtest:",
            r"^\s*not ok\b",
        ]
        return any(
            re.search(pattern, normalized_observation, re.IGNORECASE | re.MULTILINE)
            for pattern in positive_patterns
        )

    def _observation_looks_like_help_text(self, observation):
        """Exclude `--help` or usage screens from being treated as test execution."""
        if not observation:
            return False

        normalized = self._normalize_observation_text(observation).lower()
        help_markers = [
            "usage:",
            "optional arguments:",
            "positional arguments:",
            "show this help",
        ]
        return any(marker in normalized for marker in help_markers)

    def _normalize_observation_text(self, observation):
        """Strip ANSI control codes and zero-width formatting artifacts before pattern matching."""
        normalized = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", observation)
        normalized = normalized.replace("​", "")
        normalized = normalized.replace("﻿", "")
        return normalized
