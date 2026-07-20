#!/usr/bin/env python3
"""
Tool dispatcher - parse and execute tool commands.

Executes tool scripts inside the Docker container.
"""

import json
import shlex
import time
from typing import Optional

import weave

from libkit.colors import Colors, colorize, get_tool_color
from libkit.language_config import LanguageConfig
from libkit.language_configs.python_config import PythonConfig


class ToolDispatcher:
    """Tool dispatcher: parse commands and dispatch tool scripts in-container."""

    def __init__(
        self,
        session,
        language_config: LanguageConfig,
        repo_path="/repo",
        use_color=True,
        timeout=300,
        test_timeout=600,
    ):
        """
        Initialize the tool dispatcher.

        Args:
            session: environment.Env.Session used to run commands in the container.
            repo_path: Repo path inside the container.
            use_color: Whether to use colored output.
            language_config: Language config object.
            timeout: Default command timeout (seconds).
            test_timeout: Test command timeout (seconds).
        """
        self.session = session
        self.repo_path = repo_path
        self.tools_path = "/home/tools"  # In-container tool scripts path
        self.use_color = use_color
        self.timeout = timeout
        self.test_timeout = test_timeout

        self.language_config = language_config

        if not use_color:
            Colors.disable()

        # Base tool map (language-agnostic)
        self.base_tool_map = {
            "search-repo": self._execute_search_repo,
            "construct-test": self._execute_construct_test,
            "run-test": self._execute_run_test,
            "read-file": self._execute_read_file,
            "retrieve-image": self._execute_retrieve_image,
            "search-web": self._execute_search_web,
            "retrieve-issue": self._execute_retrieve_issue,
            "retrieve-trajectory": self._execute_retrieve_trajectory,
            "view-outline": self._execute_view_outline,
            "ls-structure": self._execute_ls_structure,
            "edit-file": self._execute_edit_file,
            "detect-environment": self._execute_detect_environment,
            "cicd-config": self._execute_cicd_config,
            "stop": self._execute_stop,
        }

        # Build language-specific tool map
        self.language_tool_map = self._build_language_tools()

        # Merge tool maps
        self.tool_map = {**self.base_tool_map, **self.language_tool_map}

        # Stats
        self.stats = {}
        self._init_stats()

    def _build_language_tools(self) -> dict:
        """Build language-specific tool map from the language config.

        Returns:
            dict: Tool name -> executor function.
        """
        tools = {}

        # Add test runner tools
        test_runners = self.language_config.get_test_runner_tools()
        for tool_name, tool_config in test_runners.items():
            # Create executor dynamically
            tools[tool_name] = self._create_test_runner(tool_name, tool_config)

        # Add version switch tool (if supported)
        version_tool_config = self.language_config.get_version_change_tool()
        if version_tool_config:
            tool_name = version_tool_config["tool_name"]
            tools[tool_name] = self._create_version_changer(version_tool_config)

        return tools

    def _create_test_runner(self, tool_name: str, config: dict):
        """Dynamically create a test runner executor.

        Args:
            tool_name: Tool name.
            config: Tool config dict.

        Returns:
            Executor function.
        """

        def executor(command: str) -> tuple:
            """Execute the test runner."""
            script = config.get("script", "")
            timeout = config.get("timeout", 600)

            container_cmd = f"python3 {self.tools_path}/{script}"

            try:
                output, return_code = self.session.execute(
                    container_cmd, timeout=timeout
                )
                return output, return_code
            except Exception as e:
                return f"Error executing {tool_name}: {str(e)}", 1

        return executor

    def _create_version_changer(self, config: dict):
        """Dynamically create a version switch executor.

        Args:
            config: Version switch tool config dict.

        Returns:
            Executor function.
        """
        tool_name = config["tool_name"]
        method_name = config["method_name"]
        emoji = config["emoji"]
        example = config["example"]

        def executor(command: str) -> tuple:
            """Execute version switch."""
            args = self._parse_args(command, tool_name)

            # Get version argument (positional)
            version = None
            if "positional" in args and args["positional"]:
                version = args["positional"][0]
            else:
                return (
                    f"❌ Error: {tool_name} requires a version argument, e.g. {example}",
                    1,
                )

            if not version:
                return "❌ Error: no version specified", 1

            print(f"{emoji} Trying to switch version to: {version}")

            try:
                # Access the env via session and call the version-switch method
                if hasattr(self.session, "env"):
                    # Dynamically call env method
                    change_method = getattr(self.session.env, method_name, None)
                    if not change_method:
                        return (
                            f"❌ Error: environment does not support {method_name}",
                            1,
                        )

                    result = change_method(version)
                    # Check return: string means error; self means success
                    if isinstance(result, str) and result.startswith("❌"):
                        # Error message
                        return result, 1
                    else:
                        # Success - refresh session because env may have restarted
                        self.session = self.session.env.get_session()
                        return (
                            f"✅ Switched version to {version}\n📦 New image: {self.session.env.namespace}",
                            0,
                        )
                else:
                    return "❌ Error: cannot access environment object", 1
            except Exception as e:
                return f"❌ Error while switching version: {str(e)}", 1

        return executor

    def _init_stats(self):
        """Initialize stats."""
        for tool_name in self.tool_map.keys():
            self.stats[tool_name] = {
                "count": 0,  # Call count
                "total_time": 0.0,  # Total time (seconds)
                "total_tokens": 0,  # Total token usage
                "success_count": 0,  # Success count
                "failed_count": 0,  # Failure count
                "calls": [],  # Per-call details
            }

    def _record_tool_call(
        self,
        tool_name: str,
        elapsed_time: float,
        tokens: int,
        return_code: int,
        command: str,
    ):
        """
        Record tool call statistics.

        Args:
            tool_name: Tool name.
            elapsed_time: Elapsed time (seconds).
            tokens: Token usage.
            return_code: Return code.
            command: Executed command.
        """
        if tool_name not in self.stats:
            self.stats[tool_name] = {
                "count": 0,
                "total_time": 0.0,
                "total_tokens": 0,
                "success_count": 0,
                "failed_count": 0,
                "calls": [],
            }

        stats = self.stats[tool_name]
        stats["count"] += 1
        stats["total_time"] += elapsed_time
        stats["total_tokens"] += tokens

        # For run-pytest, return codes 0 and 1 both count as success
        # (0: all tests passed, 1: some tests failed but pytest executed)
        # For run-pytest-collect, return codes 0 and 5 both count as success
        # (0: collection succeeded, 5: no tests collected but collection executed)
        if tool_name == "run-pytest":
            if return_code in (0, 1):
                stats["success_count"] += 1
            else:
                stats["failed_count"] += 1
        elif tool_name == "run-pytest-collect":
            if return_code in (0, 5):
                stats["success_count"] += 1
            else:
                stats["failed_count"] += 1
        else:
            if return_code == 0:
                stats["success_count"] += 1
            else:
                stats["failed_count"] += 1

        # Record per-call details
        stats["calls"].append(
            {
                "command": command,
                "time": elapsed_time,
                "tokens": tokens,
                "return_code": return_code,
                "timestamp": time.time(),
            }
        )

    def _print_tool_execution(self, tool_name: str, command: str):
        """Print tool execution information (colorized)."""
        color = get_tool_color(tool_name)
        separator = "=" * 60
        print(colorize(f"\n{separator}", color))
        print(colorize(f"🔧 Executing tool: {tool_name}", color, bold=True))
        print(colorize(f"📝 Command: {command}", color))
        print(colorize(separator, color))
        print()  # Blank line for readability

    def is_tool_command(self, command: str) -> bool:
        """Return True if the command is a tool command."""
        command = command.strip()
        for tool_name in self.tool_map.keys():
            if command.startswith(tool_name):
                return True
        return False

    @weave.op
    def dispatch(self, command: str) -> tuple:
        """
        Dispatch a tool command.

        Args:
            command: Tool command string, e.g. "search-repo --query 'user auth'"

        Returns:
            (output: str, return_code: int)
        """
        command = command.strip()

        # Find tool by name (sort by length to avoid prefix mismatches)
        tool_name = None
        for name in sorted(self.tool_map.keys(), key=len, reverse=True):
            if command.startswith(name):
                tool_name = name
                break

        if not tool_name:
            return f"Error: Unknown tool command: {command}", 1

        # Track time
        start_time = time.time()
        tokens_used = 0

        try:
            # Call tool executor
            tool_func = self.tool_map[tool_name]
            output, return_code = tool_func(command)
            # Try extracting token usage from output (if present)
            tokens_used = self._extract_tokens_from_output(output)
            # Record stats
            elapsed_time = time.time() - start_time
            self._record_tool_call(
                tool_name, elapsed_time, tokens_used, return_code, command
            )

            return output, return_code
        except Exception as e:
            # Record stats even on error
            elapsed_time = time.time() - start_time
            self._record_tool_call(tool_name, elapsed_time, tokens_used, 1, command)
            return f"Error executing tool '{tool_name}': {str(e)}", 1

    def _parse_args(self, command: str, tool_name: str) -> dict:
        """
        Parse tool command arguments.

        Args:
            command: Full command, e.g. "search-repo --query 'test' --max 5".
            tool_name: Tool name.

        Returns:
            Args dict.
        """
        # Strip tool name prefix
        args_str = command[len(tool_name) :].strip()

        # Tokenize via shlex (handles quotes)
        try:
            tokens = shlex.split(args_str)
        except ValueError as e:
            # Fall back to simple split
            tokens = args_str.split()

        # Known boolean flags (no value)
        boolean_flags = {
            "regex",
            "list",
            "no-llm",
            "recursive",
            "no-line-numbers",
            "llm",
            "r",
            "l",
        }

        # Parse
        args = {}
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token.startswith("--"):
                # Long flag
                key = token[2:]
                if key in boolean_flags:
                    # Boolean flag
                    args[key] = True
                    i += 1
                elif i + 1 < len(tokens):
                    # Always take the next token as value, even if it starts with '-'
                    args[key] = tokens[i + 1]
                    i += 2
                else:
                    # No more tokens; treat as boolean
                    args[key] = True
                    i += 1
            elif token.startswith("-"):
                # Short flag
                key = token[1:]
                if key in boolean_flags:
                    args[key] = True
                    i += 1
                elif i + 1 < len(tokens):
                    args[key] = tokens[i + 1]
                    i += 2
                else:
                    args[key] = True
                    i += 1
            else:
                # Positional args
                if "positional" not in args:
                    args["positional"] = []
                args["positional"].append(token)
                i += 1

        return args

    # ==================== Tool executors ====================

    def _execute_search_repo(self, command: str) -> tuple:
        """Execute search-repo (in container)."""
        args = self._parse_args(command, "search-repo")

        query = args.get("query") or args.get("q", "")
        repo_path = args.get("repo", self.repo_path)
        mode = args.get("mode", "llm")
        max_results = int(args.get("max", 10))

        if not query:
            return "Error: search-repo requires --query parameter", 1

        # Build in-container command
        container_cmd = f"python3 {self.tools_path}/search_repo.py --query '{query}' --repo {repo_path} --mode {mode} --max {max_results}"

        try:
            output, return_code = self.session.execute(
                container_cmd, timeout=self.timeout
            )
            return output, return_code
        except Exception as e:
            return f"Error executing search-repo in container: {str(e)}", 1

    def _execute_construct_test(self, command: str) -> tuple:
        """Execute construct-test (in container)."""
        args = self._parse_args(command, "construct-test")

        repo_path = args.get("repo", self.repo_path)
        mode = args.get("mode", "llm")

        # Build in-container command
        container_cmd = (
            f"python3 {self.tools_path}/create_test.py --repo {repo_path} --mode {mode}"
        )

        try:
            output, return_code = self.session.execute(
                container_cmd, timeout=self.timeout
            )
            return output, return_code
        except Exception as e:
            return f"Error executing construct-test in container: {str(e)}", 1

    def _execute_run_test(self, command: str) -> tuple:
        """Execute run-test (in container)."""
        args = self._parse_args(command, "run-test")

        repo_path = args.get("repo", self.repo_path)
        # Ensure max is an int
        max_tests = int(args.get("max", 0)) if args.get("max") else 0
        test_type = args.get("type")
        list_only = args.get("list", False)

        # Build in-container command
        container_cmd = f"python3 {self.tools_path}/run_test.py --repo {repo_path}"

        if max_tests > 0:
            container_cmd += f" --max {max_tests}"

        if test_type:
            container_cmd += f" --type {test_type}"

        if list_only:
            container_cmd += " --list"

        try:
            # Tests may take longer; use test_timeout
            output, return_code = self.session.execute(
                container_cmd, timeout=self.test_timeout
            )
            return output, return_code
        except Exception as e:
            return f"Error executing run-test in container: {str(e)}", 1

    def _execute_run_pytest(self, command: str) -> tuple:
        """Execute run-pytest (in container) - fixed config, no args."""
        # Fixed command; no parameters
        container_cmd = f"python3 {self.tools_path}/run_pytest.py"

        try:
            # pytest may take longer; use test_timeout
            output, return_code = self.session.execute(
                container_cmd, timeout=self.test_timeout
            )
            return output, return_code
        except Exception as e:
            return f"Error executing run-pytest in container: {str(e)}", 1

    def _execute_run_pytest_collect(self, command: str) -> tuple:
        """Execute run-pytest-collect (in container) - fixed config, no args."""
        # Fixed command; no parameters
        container_cmd = f"python3 {self.tools_path}/run_pytest_collect.py"

        try:
            # pytest collect is usually quick; set a ~60s timeout
            output, return_code = self.session.execute(container_cmd, timeout=120)
            return output, return_code
        except Exception as e:
            return f"Error executing run-pytest-collect in container: {str(e)}", 1

    def _execute_read_file(self, command: str) -> tuple:
        """Execute read-file (in container)."""
        args = self._parse_args(command, "read-file")

        # Get file path (positional or --file)
        file_path = None
        if "positional" in args and args["positional"]:
            file_path = args["positional"][0]
        else:
            file_path = args.get("file")

        if not file_path:
            return "Error: read-file requires a file path", 1

        question = args.get("question") or args.get("q")
        use_llm = not args.get("no-llm", False)

        # Build in-container command
        container_cmd = f"python3 {self.tools_path}/read_file.py '{file_path}'"
        if question:
            container_cmd += f" --question '{question}'"
        if not use_llm:
            container_cmd += " --no-llm"

        try:
            output, return_code = self.session.execute(
                container_cmd, timeout=self.timeout
            )
            return output, return_code
        except Exception as e:
            return f"Error executing read-file in container: {str(e)}", 1

    def _execute_retrieve_image(self, command: str) -> tuple:
        """Execute retrieve-image (in container)."""
        args = self._parse_args(command, "retrieve-image")

        repo_path = args.get("repo", self.repo_path)
        max_results = int(args.get("max", 10))

        # Build in-container command
        container_cmd = f"python3 {self.tools_path}/retrieve_image.py --repo {repo_path} --max {max_results}"

        try:
            output, return_code = self.session.execute(
                container_cmd, timeout=self.timeout
            )
            return output, return_code
        except Exception as e:
            return f"Error executing retrieve-image in container: {str(e)}", 1

    def _execute_search_web(self, command: str) -> tuple:
        """Execute search-web (in container)."""
        args = self._parse_args(command, "search-web")

        # Get query (positional or --query)
        query = None
        if "positional" in args and args["positional"]:
            query = " ".join(args["positional"])
        else:
            query = args.get("query") or args.get("q")

        if not query:
            return "Error: search-web requires a query", 1

        # Build in-container command
        container_cmd = f"python3 {self.tools_path}/search_web.py --query '{query}'"

        try:
            output, return_code = self.session.execute(
                container_cmd, timeout=self.timeout
            )
            return output, return_code
        except Exception as e:
            return f"Error executing search-web in container: {str(e)}", 1

    def _execute_retrieve_issue(self, command: str) -> tuple:
        """Execute retrieve-issue (in container)."""
        args = self._parse_args(command, "retrieve-issue")

        # Get error message (positional or --error/-e)
        error_msg = None
        if "positional" in args and args["positional"]:
            error_msg = " ".join(args["positional"])
        else:
            error_msg = args.get("error") or args.get("e")

        if not error_msg:
            return "Error: retrieve-issue requires an error message", 1

        # Optional args
        repo_path = args.get("repo", self.repo_path)
        max_results = int(args.get("max", 10))
        use_llm = args.get("llm", False)
        llm_name = args.get("llm-name", "deepseek-chat")

        # Build in-container command
        container_cmd = f"python3 {self.tools_path}/retrieve_issue.py -e '{error_msg}' --repo {repo_path} --max {max_results}"

        if use_llm:
            container_cmd += " --llm"
            if llm_name:
                container_cmd += f" --llm-name {llm_name}"

        try:
            output, return_code = self.session.execute(
                container_cmd, timeout=self.timeout
            )
            return output, return_code
        except Exception as e:
            return f"Error executing retrieve-issue in container: {str(e)}", 1

    def _execute_retrieve_trajectory(self, command: str) -> tuple:
        """Execute retrieve-trajectory."""
        # TODO: implement retrieve_trajectory
        return (
            "retrieve-trajectory: Fetching configuration ideas from historical trajectories...\n(not implemented yet)",
            0,
        )

    def _execute_view_outline(self, command: str) -> tuple:
        """Execute view-outline (in container)."""
        args = self._parse_args(command, "view-outline")

        # Get path (positional or default repo path)
        path = self.repo_path
        if "positional" in args and args["positional"]:
            path = args["positional"][0]

        # Build in-container command
        container_cmd = f"python3 {self.tools_path}/view_outline.py '{path}'"

        try:
            output, return_code = self.session.execute(
                container_cmd, timeout=self.timeout
            )
            return output, return_code
        except Exception as e:
            return f"Error executing view-outline in container: {str(e)}", 1

    def _execute_ls_structure(self, command: str) -> tuple:
        """Execute ls-structure (in container)."""
        args = self._parse_args(command, "ls-structure")

        repo_path = args.get("repo", self.repo_path)
        depth = int(args.get("depth", 5))

        # Build in-container command
        container_cmd = f"python3 {self.tools_path}/ls_structure.py --repo {repo_path} --depth {depth}"

        try:
            output, return_code = self.session.execute(
                container_cmd, timeout=self.timeout
            )
            return output, return_code
        except Exception as e:
            return f"Error executing ls-structure in container: {str(e)}", 1

    def _execute_edit_file(self, command: str) -> tuple:
        """Execute edit-file (in container)."""
        args = self._parse_args(command, "edit-file")

        # Get file path (positional or --file)
        file_path = None
        if "positional" in args and args["positional"]:
            file_path = args["positional"][0]
        else:
            file_path = args.get("file")

        if not file_path:
            return "Error: edit-file requires a file path", 1

        mode = args.get("mode", "llm")

        # Build in-container command
        container_cmd = (
            f"python3 {self.tools_path}/edit_file.py '{file_path}' --mode {mode}"
        )

        # Add arguments based on mode
        if mode == "replace":
            start_line = args.get("start-line")
            end_line = args.get("end-line")
            content = args.get("content", "")
            if not start_line or not end_line:
                return "Error: replace mode requires --start-line and --end-line", 1
            container_cmd += f" --start-line {start_line} --end-line {end_line} --content '{content}'"

        elif mode == "insert":
            at_line = args.get("at-line")
            content = args.get("content", "")
            if at_line is None:
                return "Error: insert mode requires --at-line", 1
            container_cmd += f" --at-line {at_line} --content '{content}'"

        elif mode == "delete":
            start_line = args.get("start-line")
            end_line = args.get("end-line")
            if not start_line or not end_line:
                return "Error: delete mode requires --start-line and --end-line", 1
            container_cmd += f" --start-line {start_line} --end-line {end_line}"

        elif mode == "search":
            search_text = args.get("search")
            replace_text = args.get("replace", "")
            if not search_text:
                return "Error: search mode requires --search", 1
            container_cmd += f" --search '{search_text}' --replace '{replace_text}'"
            if args.get("regex"):
                container_cmd += " --regex"
            if "count" in args:
                container_cmd += f" --count {args.get('count')}"

        elif mode == "llm":
            instruction = args.get("instruction")
            if not instruction:
                return "Error: llm mode requires --instruction", 1
            container_cmd += f" --instruction '{instruction}'"

        elif mode == "append":
            content = args.get("content", "")
            if not content:
                return "Error: append mode requires --content", 1
            container_cmd += f" --content '{content}'"

        try:
            output, return_code = self.session.execute(
                container_cmd, timeout=self.timeout
            )
            return output, return_code
        except Exception as e:
            return f"Error executing edit-file in container: {str(e)}", 1

    def _execute_detect_environment(self, command: str) -> tuple:
        """Execute detect-environment (in container)."""
        args = self._parse_args(command, "detect-environment")

        output_format = args.get("format", "text")
        save_path = args.get("save")

        # Build in-container command
        container_cmd = (
            f"python3 {self.tools_path}/detect_environment.py --format {output_format}"
        )

        if save_path:
            container_cmd += f" --save {save_path}"

        try:
            output, return_code = self.session.execute(container_cmd, timeout=60)
            return output, return_code
        except Exception as e:
            return f"Error executing detect-environment in container: {str(e)}", 1

    def _execute_cicd_config(self, command: str) -> tuple:
        """Execute cicd-config (in container)."""
        args = self._parse_args(command, "cicd-config")

        repo_path = args.get("repo", self.repo_path)
        output_format = args.get("format", "text")
        workflow = args.get("workflow")
        save_path = args.get("output")
        use_llm = args.get("llm", False)
        execute = args.get("execute", False)

        # Build in-container command
        container_cmd = f"python3 {self.tools_path}/cicd_config.py --repo {repo_path}"

        if output_format:
            container_cmd += f" --format {output_format}"

        if workflow:
            container_cmd += f" --workflow {workflow}"

        if save_path:
            container_cmd += f" --output {save_path}"

        if use_llm:
            container_cmd += " --llm"

        if execute:
            container_cmd += " --execute"

        # If the language config supports use_uv, pass it through
        if hasattr(self.language_config, "use_uv") and self.language_config.use_uv:
            container_cmd += " --use-uv"

        try:
            # CI/CD config may take longer; use test_timeout
            output, return_code = self.session.execute(
                container_cmd, timeout=self.test_timeout
            )
            return output, return_code
        except Exception as e:
            return f"Error executing cicd-config in container: {str(e)}", 1

    def _execute_stop(self, command: str) -> tuple:
        """Execute stop."""
        return "STOP: Environment configuration stopped by agent.", 0

    def _extract_tokens_from_output(self, output: str) -> int:
        """
        Extract token usage from tool output.

        Args:
            output: Tool output text.

        Returns:
            Extracted token count; returns 0 if not found.
        """
        # Match common token output formats
        patterns = [
            r"tokens?[:\s]+(\d+)",
            r"(\d+)\s+tokens?",
            r"token[s]?\s*usage[:\s]+(\d+)",
            r"total[_\s]tokens?[:\s]+(\d+)",
        ]

        for pattern in patterns:
            import re

            match = re.search(pattern, output, re.IGNORECASE)
            if match:
                return int(match.group(1))

        return 0

    def get_stats(self) -> dict:
        """
        Get tool call statistics.

        Returns:
            Stats dict.
        """
        return self.stats

    def _display_width(self, text: str) -> int:
        """
        Compute display width (CJK/fullwidth counted as 2 columns, ASCII as 1).

        Args:
            text: Input string.

        Returns:
            Display width.
        """
        width = 0
        for char in str(text):
            # CJK/fullwidth characters typically occupy 2 columns
            if ord(char) > 0x2000:
                width += 2
            else:
                width += 1
        return width

    def _pad_string(self, text: str, width: int) -> str:
        """
        Pad a string to a target display width.

        Args:
            text: Input string.
            width: Target display width.

        Returns:
            Padded string.
        """
        current_width = self._display_width(text)
        padding = width - current_width
        if padding > 0:
            return str(text) + " " * padding
        else:
            return str(text)

    def print_stats(self, detailed: bool = False):
        """
        Print tool call statistics.

        Args:
            detailed: Whether to print per-call details.
        """
        print("\n" + "=" * 100)
        print("📊 Tool call statistics")
        # Filter tools that were never called
        used_tools = {k: v for k, v in self.stats.items() if v["count"] > 0}

        if not used_tools:
            print("⚠️  No tools were called")
            return

        # Sort by call count
        sorted_tools = sorted(
            used_tools.items(), key=lambda x: x[1]["count"], reverse=True
        )

        # Compute tool name column width (min 20, max 30)
        max_tool_name_width = max(
            self._display_width(tool_name) for tool_name, _ in sorted_tools
        )
        max_tool_name_width = max(self._display_width("Tool"), max_tool_name_width)
        tool_name_width = min(max(max_tool_name_width + 2, 20), 30)

        # Header
        header = (
            self._pad_string("Tool", tool_name_width)
            + self._pad_string("Calls", 10)
            + self._pad_string("Success", 10)
            + self._pad_string("Failed", 10)
            + self._pad_string("Total(s)", 15)
            + self._pad_string("Avg(s)", 15)
            + self._pad_string("Tokens", 12)
        )
        print(f"\n{header}")
        print("-" * 100)

        total_calls = 0
        total_time = 0.0
        total_tokens = 0

        for tool_name, stats in sorted_tools:
            count = stats["count"]
            success = stats["success_count"]
            failed = stats["failed_count"]
            time_total = stats["total_time"]
            time_avg = time_total / count if count > 0 else 0
            tokens = stats["total_tokens"]

            total_calls += count
            total_time += time_total
            total_tokens += tokens

            row = (
                self._pad_string(tool_name, tool_name_width)
                + self._pad_string(str(count), 10)
                + self._pad_string(str(success), 10)
                + self._pad_string(str(failed), 10)
                + self._pad_string(f"{time_total:.2f}", 15)
                + self._pad_string(f"{time_avg:.2f}", 15)
                + self._pad_string(str(tokens), 12)
            )
            print(row)

        # Summary
        print("-" * 100)
        summary = (
            self._pad_string("Total", tool_name_width)
            + self._pad_string(str(total_calls), 10)
            + self._pad_string("", 10)
            + self._pad_string("", 10)
            + self._pad_string(f"{total_time:.2f}", 15)
            + self._pad_string("", 15)
            + self._pad_string(str(total_tokens), 12)
        )
        print(summary)
        print("=" * 100)

        # Details
        if detailed:
            print("\n" + "=" * 80)
            print("📋 Detailed call records")
            print("=" * 80)

            for tool_name, stats in sorted_tools:
                if stats["calls"]:
                    print(f"\n🔧 {tool_name} ({len(stats['calls'])} calls):")
                    for i, call in enumerate(stats["calls"], 1):
                        status = "✅" if call["return_code"] == 0 else "❌"
                        print(f"  {i}. {status} {call['command'][:60]}...")
                        print(
                            f"     Time: {call['time']:.2f}s, Tokens: {call['tokens']}"
                        )

    def save_stats(self, filepath: Optional[str] = None):
        """
        Save statistics to a JSON file.

        Args:
            filepath: Save path (default: /repo/logs/tool_stats.json)
        """
        import os

        # Build data to save
        stats_to_save = {}
        for tool_name, stats in self.stats.items():
            if stats["count"] > 0:  # Save only called tools
                stats_to_save[tool_name] = {
                    "count": stats["count"],
                    "total_time": stats["total_time"],
                    "avg_time": stats["total_time"] / stats["count"]
                    if stats["count"] > 0
                    else 0,
                    "total_tokens": stats["total_tokens"],
                    "success_count": stats["success_count"],
                    "failed_count": stats["failed_count"],
                    "calls": stats["calls"],  # Optionally keep per-call details
                }

        # Nothing to save
        if not stats_to_save:
            print("\n⚠️  No tools were called; skipping stats save")
            return

        # Resolve path
        if filepath is None:
            filepath = f"{self.repo_path}/logs/tool_stats.json"

        # Save
        try:
            # Ensure directory exists
            dirpath = os.path.dirname(filepath)
            if dirpath:  # If there is a directory part
                os.makedirs(dirpath, exist_ok=True)

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(stats_to_save, f, indent=2, ensure_ascii=False)
            print(f"\n💾 Stats saved to: {filepath}")
        except Exception as e:
            print(f"\n⚠️  Failed to save stats: {e}")
            print(f"   Path attempted: {filepath}")


if __name__ == "__main__":
    # ToolDispatcher demo
    print("Note: ToolDispatcher requires a Session object to run")
    print("Example usage:")
    print("""
    from libkit.environment import Env

    # Create environment and start container
    env = Env('python:3.10', 'owner/repo-name', '/path/to/root')
    env.create_container()

    # Get session
    session = env.get_session()

    # Create tool dispatcher
    dispatcher = ToolDispatcher(session, '/repo')

    # Execute a tool command
    output, code = dispatcher.dispatch("ls-structure --repo /repo --depth 3")
    print(f"Return code: {code}")
    print(f"Output:\\n{output}")

    # Cleanup
    session.close()
    env.stop_container()
    """)
