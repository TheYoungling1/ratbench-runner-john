import json
import os
import re
import subprocess
import time

import weave

try:
    from libkit.ablation_config import ENABLE_TOOL_ABLATION, ESSENTIAL_TOOLS
except ImportError:
    ENABLE_TOOL_ABLATION = False
    ESSENTIAL_TOOLS = set()

from libkit.colors import Colors, colorize
from libkit.llm import LLMChat
from libkit.parser import extract_commands, parse_commands, split_cmd_statements
from libkit.tool import (
    TIME_OUT_LABEL,
    update_trajectory,
)
from libkit.tool_dispatcher import ToolDispatcher


def manage_token_usage(messages, max_tokens=150000):
    """
    Manage token usage for message history.

    When the message list exceeds the token limit, drop the oldest messages until the total token count is below max_tokens.
    Uses slicing to keep the list manageable.
    """
    total_tokens = sum(len(str(message)) for message in messages)
    if total_tokens <= max_tokens:
        return messages  # Within limit
    # Compute how many messages to keep
    new_messages = messages[:]
    while sum(len(str(message)) for message in new_messages) > max_tokens:
        # new_messages = new_messages[4:]  # Slice-delete oldest messages (not index 0)
        new_messages = new_messages[:4] + new_messages[6:]
    return new_messages


def res_truncate(text):
    keywords = [
        """waitinglist command usage error, the following command formats are leagal:
1. `waitinglist add -p package_name1 -v >=1.0.0 -t pip`
Explanation: Add package_name1>=1.0.0 into waiting list(using pip), and version constraints string cannot contain spaces.
2. `waitinglist add -p package_name1 -t pip`
Explanation: Add package_name1 into waiting list, no `-v` means download the latest version by default.
3. `waitinglist addfile /path/to/file`
Explanation: Add all the items in the /path/to/file into waiting list. Note that you must make sure each line's item meet the formats like [package_name][version_constraints].
4. `waitinglist clear`
Explanation: Clear all the items in the waiting list.""",
        "If you have multiple elements to add to the waitinglist, you can use && to connect multiple `waitinglist add` statements and surround them with ```bash and ```. Please make sure to write the complete statements; we will only recognize complete statements. Do not use ellipses or other incomplete forms.",
        """conflictlist command usage error, the following command formats are legal:
1. `conflictlist solve`
Explanation: The standalone `conflictlist solve` command means not to impose any version constraints, i.e., to default to downloading the latest version of the third-party library. This will update the version constraint in the waiting list to be unrestricted.
2. `conflictlist solve -v "==2.0"`
Explanation: Adding -v followed by a version constraint enclosed in double quotes updates the version constraint in the waiting list to that specific range, such as "==2.0", meaning to take version 2.0.
3. `conflictlist solve -v ">3.0"`
Explanation: Similar to the command 2, this constraint specifies a version number greater than 3.0.
4. `conflictlist solve -u`
Explanation: Adding -u indicates giving up all the constraints in the conflict list while still retaining the constraints in the waiting list, i.e., not updating the constraints for that library in the waiting list.
5. `conflictlist clear`
Explanation: Clear all the items in the conflict list.""",
        "If you have multiple elements to remove from the conflict list, you can use && to connect multiple `conflictlist solve` statements and surround them with ```bash and ```. Please make sure to write the complete statements; we will only recognize complete statements. Do not use ellipses or other incomplete forms.",
    ]
    all_positions = {}
    for keyword in keywords:
        positions = [i for i in range(len(text)) if text.startswith(keyword, i)]
        if len(positions) > 1:
            all_positions[keyword] = positions

    if not all_positions:
        return text
    new_text = text
    keywords_to_remove = sorted(
        all_positions.items(), key=lambda item: item[1][-1], reverse=True
    )

    for keyword, positions in keywords_to_remove:
        last_position = positions[-1]
        before_last_position = new_text[:last_position].replace(
            keyword, "", len(positions) - 1
        )
        after_last_position = new_text[last_position:]
        new_text = before_last_position + after_last_position

    return new_text


class CodeAgent:
    def __init__(
        self,
        code_environment,
        image_name,
        full_name,
        root_dir,
        language="python",
        llm_name="gpt-4o-2024-05-13",
        max_turn=70,
        use_color=True,
        use_custom_plan=False,
        use_repo_dockerfile=False,
        use_uv=False,
        timeout=450,
        test_timeout=600,
    ):
        self.llm = LLMChat(llm_name)
        self.llm_name = llm_name
        self.env = code_environment
        self.env_session = self.env.get_session()
        self.full_name = full_name
        self.root_dir = root_dir
        self.max_turn = max_turn
        self.use_color = use_color
        self.use_custom_plan = use_custom_plan
        self.use_repo_dockerfile = use_repo_dockerfile
        self.use_uv = use_uv
        self.language = language
        self.timeout = timeout
        self.test_timeout = test_timeout

        if not use_color:
            Colors.disable()

        # Reuse the env's language_config to avoid duplicate instances.
        # This ensures CodeAgent and Env share the same config object
        # (e.g., for Java projects: shared is_maven/is_gradle state).
        self.language_config = self.env.language_config

        # Get tool set from language config
        self.toolkit = self.language_config.get_toolkit()

        if ENABLE_TOOL_ABLATION:
            print("**************************************************")
            print(f"* TOOL ABLATION ENABLED: Keeping {ESSENTIAL_TOOLS}")
            print("**************************************************")
            filtered_toolkit = []
            for tool in self.toolkit:
                # Handle Enum objects by accessing .value
                tool_val = tool.value if hasattr(tool, "value") else tool
                if isinstance(tool_val, dict) and "command" in tool_val:
                    cmd_name = tool_val["command"].split()[0]
                    if cmd_name in ESSENTIAL_TOOLS:
                        filtered_toolkit.append(tool)
            self.toolkit = filtered_toolkit

        self.image_name = image_name
        self.outer_commands = list()
        self.tool_dispatcher = ToolDispatcher(
            session=self.env_session,
            repo_path="/repo",
            language_config=self.language_config,  # Pass language config to dispatcher
            timeout=self.timeout,
            test_timeout=self.test_timeout,
        )

        # Track test runner execution status
        self.test_runners_executed = {
            tool_name: False
            for tool_name in self.language_config.get_test_runner_tools().keys()
        }

        # Build tools list
        tools_list = ""
        tool_index = 0

        # 1. Add ToolKit tools
        for tool in self.toolkit:
            if isinstance(tool.value, dict):
                tools_list += f"{tool_index}.{tool.value['command']} # {tool.value['description']}\n"
            elif isinstance(tool.value, tuple):
                tools_list += f"{tool_index}.{tool.value[0]} # {tool.value[1]}\n"
            else:
                raise ValueError(f"Unexpected tool.value type: {type(tool.value)}")
            tool_index += 1

        # 2. Add test_runner_tools
        test_runner_tools = self.language_config.get_test_runner_tools()
        for tool_name, tool_config in test_runner_tools.items():
            description = tool_config.get("description", f"{tool_name} test tool")
            tools_list += f"{tool_index}.{tool_name} # {description}\n"
            tool_index += 1

        self.tools_list = tools_list

        # Get initial prompt from language config
        self.init_prompt = self.language_config.get_init_prompt(
            self.image_name,
            tools_list,
            self.use_custom_plan,
            self.max_turn,
            self.use_repo_dockerfile,
        )

    def get_max_turn(self):
        return self.max_turn

    def colorize_output(self, text: str) -> str:
        """
        Add colors to keywords in output.

        Args:
            text: Raw text.

        Returns:
            Colored text.
        """
        if not self.use_color:
            return text

        text = re.sub(
            r"(### Thought:)", colorize(r"\1", Colors.BRIGHT_CYAN, bold=True), text
        )

        # Action - yellow bold
        text = re.sub(
            r"(### Action:)", colorize(r"\1", Colors.BRIGHT_YELLOW, bold=True), text
        )

        # Observation - green bold
        text = re.sub(
            r"(### Observation:)", colorize(r"\1", Colors.BRIGHT_GREEN, bold=True), text
        )

        # Current directory - blue bold
        text = re.sub(
            r"(\[Current directory\]:)",
            colorize(r"\1", Colors.BRIGHT_BLUE, bold=True),
            text,
        )

        # ENVIRONMENT REMINDER - magenta bold
        text = re.sub(
            r"(ENVIRONMENT REMINDER:)",
            colorize(r"\1", Colors.BRIGHT_MAGENTA, bold=True),
            text,
        )

        return text

    def _copy_test_results_from_container(self):
        """Copy test results from container to host output dir (multi-language)."""
        if not self.env.container:
            return

        # File list from language config
        files_to_copy = self.language_config.get_test_result_files()

        # Host output dir: output/{full_name}/
        output_dir = f"{self.root_dir}/output/{self.full_name}"
        os.makedirs(output_dir, exist_ok=True)

        for file_info in files_to_copy:
            try:
                source_path = file_info["source"]
                dest_path = f"{output_dir}/{file_info['dest_name']}"

                # Copy via docker cp
                cmd = f"docker cp {self.env.container.name}:{source_path} {dest_path}"
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)

                if result.returncode == 0:
                    print(f"✅ Copied {file_info['description']}: {dest_path}")
                else:
                    # The file may not exist (tool may not have run)
                    if "No such file" not in result.stderr:
                        print(
                            f"⚠️  Failed to copy {file_info['description']}: {result.stderr}"
                        )
            except Exception as e:
                print(f"⚠️  Copy error for {file_info['description']}: {e}")

    def _copy_junit_xml_from_container(self):
        """Copy JUnit XML report from container to host output dir."""
        if not self.env.container:
            return

        try:
            # Source: /repo/logs/junit_report.xml in container
            source_path = "/repo/logs/junit_report.xml"

            # Destination dir: output/{full_name} on host
            output_dir = f"{self.root_dir}/output/{self.full_name}"
            os.makedirs(output_dir, exist_ok=True)

            # Destination path
            dest_path = f"{output_dir}/junit_report.xml"

            # Copy via docker cp
            cmd = f"docker cp {self.env.container.name}:{source_path} {dest_path}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)

            if result.returncode == 0:
                print(f"✅ Copied JUnit XML report to: {dest_path}")
            else:
                # The file may not exist (pytest may not have run)
                if "No such file" not in result.stderr:
                    print(f"⚠️  Failed to copy JUnit XML: {result.stderr}")
        except Exception as e:
            print(f"⚠️  JUnit XML copy error: {e}")

    def print_bash_command_stats(self):
        """Print bash command execution stats (non-tool commands)."""
        bash_commands = [
            cmd
            for cmd in self.outer_commands
            if "command" in cmd and cmd.get("time", -1) > 0
        ]

        if not bash_commands:
            return

        print("\n" + "=" * 100)
        print("📊 Bash command execution statistics")

        # Print detailed list
        print(f"\n{'No.':<6}{'Command':<60}{'RC':<10}{'Time(s)':<12}")
        print("-" * 100)

        for idx, cmd in enumerate(bash_commands, 1):
            command_str = cmd["command"]
            # Truncate long commands
            if len(command_str) > 57:
                command_str = command_str[:54] + "..."

            returncode = cmd["returncode"]
            exec_time = cmd["time"]
            status = "✅" if returncode == 0 else "❌"

            print(
                f"{idx:<6}{command_str:<60}{status} {returncode:<7}{exec_time:<12.2f}"
            )

        # Summary
        total_time = sum(cmd["time"] for cmd in bash_commands)
        success_count = sum(1 for cmd in bash_commands if cmd["returncode"] == 0)
        failed_count = len(bash_commands) - success_count

        print("-" * 100)
        print(
            f"Total: {len(bash_commands)} commands, success: {success_count}, failed: {failed_count}"
        )
        print(
            f"Total time: {total_time:.2f}s, avg time: {total_time / len(bash_commands):.2f}s"
        )
        print("=" * 100)

    @weave.op
    def run(self, project_path, trajectory, hitl=False):
        print("************** configuration **************")
        # print(self.init_prompt)
        start_time0 = time.time()
        self.messages = []
        system_message = {"role": "system", "content": self.init_prompt}
        self.messages.append(system_message)
        user_message = {"role": "user", "content": "[Project root Path]: /repo"}
        self.messages.append(user_message)

        turn = 0
        cost_tokens = 0
        diff_no = 1

        # Feed internal instructions; collect successfully executed history

        while turn < self.max_turn:
            turn += 1
            finish = False
            # Allow user input mid-run
            if hitl:
                user_query = input(f" == Turn {turn} user query: ")
                if user_query != "":
                    self.messages.append({"role": "user", "content": user_query})

            LLM_start_time = time.time()
            current_messages = manage_token_usage(self.messages)
            instruction_list, usage = self.llm.chat(current_messages)
            LLM_end_time = time.time()
            LLM_elasped_time = LLM_end_time - LLM_start_time
            self.outer_commands.append({"LLM_time": LLM_elasped_time})
            cost_tokens += usage.completion_tokens

            # Append model reply
            assistant_message = {"role": "assistant", "content": instruction_list}
            self.messages.append(assistant_message)
            print("---------------------------")
            print(self.colorize_output(instruction_list))
            system_res = "### Observation:\n"
            init_commands = extract_commands(instruction_list)

            commands = list()
            for command in init_commands:
                commands.extend(split_cmd_statements(command))

            # diffs = extract_diffs(configuration_agent)
            # If the reply contains both a diff and commands, reject it
            # if len(diffs) != 0 and len(commands) != 0:
            #     system_res = f"ERROR! Your reply contains both bash block and diff block, which is not accepted. Each round of your reply can only contain one {BASH_FENCE[0]} {BASH_FENCE[1]} block or one {DIFF_FENCE[0]} {DIFF_FENCE[1]} block. Each round of your answers contain only *ONE* action!"
            if len(commands) != 0:
                for i in range(len(commands)):
                    self.outer_commands.append(
                        {"command": commands[i], "returncode": -2, "time": -1}
                    )
                    start_time = time.time()
                    if self.tool_dispatcher.is_tool_command(commands[i]):
                        # Execute via tool dispatcher
                        tool_output, tool_return_code = self.tool_dispatcher.dispatch(
                            commands[i]
                        )
                        sandbox_res = tool_output
                        return_code = tool_return_code
                        # If change-*-version succeeded, refresh session
                        if (
                            commands[i].strip().startswith("change-python-version")
                            or commands[i].strip().startswith("change-java-version")
                        ) and tool_return_code == 0:
                            version_type = (
                                "Python" if "python" in commands[i] else "Java"
                            )
                            print(
                                f"🔄 change-{version_type.lower()}-version succeeded; refreshing session..."
                            )
                            # Close old session first
                            if self.env_session:
                                try:
                                    self.env_session.close()
                                except Exception as e:
                                    print(f"⚠️  Failed to close old session: {e}")
                            self.env_session = self.env.get_session()
                    else:
                        sandbox_res, return_code = self.env_session.execute(commands[i])
                    sandbox_res = res_truncate(sandbox_res)
                    system_res += sandbox_res
                    if return_code != "unknown":
                        system_res += f"\n`{commands[i]}` executes with returncode: {return_code}\n"
                    end_time = time.time()
                    elasped_time = end_time - start_time
                    self.outer_commands[-1]["time"] = elasped_time
                    self.outer_commands[-1]["returncode"] = 0
                    if TIME_OUT_LABEL in sandbox_res:
                        # Close old session first
                        if self.env_session:
                            try:
                                self.env_session.close()
                            except Exception as e:
                                print(f"⚠️  Failed to close old session: {e}")
                        self.env_session = self.env.get_session()
                        self.outer_commands[-1]["returncode"] = 1
                    if (
                        "STOP: Environment configuration stopped by agent."
                        in sandbox_res
                        and "# This is $runtest.py$" not in sandbox_res
                    ):
                        # with open(
                        #     f"{self.root_dir}/output/{self.full_name}/test.txt", "w"
                        # ) as w3:
                        #     w3.write("\n".join(sandbox_res.splitlines()[1:]))

                        try:
                            construct_test_output, construct_test_return_code = (
                                self.env_session.execute(
                                    'cat /repo/logs/construct_test_result.json 2>/dev/null || echo "File not found"'
                                )
                            )
                            # Confirm the file is actually missing (output is exactly "File not found")
                            if (
                                construct_test_return_code == 0
                                and construct_test_output.strip() != "File not found"
                                and len(construct_test_output)
                                > 20  # Ensure content exists
                            ):
                                # Remove command output prefix "Running `...`..."
                                lines = construct_test_output.split("\n")
                                if lines and lines[0].startswith("Running `"):
                                    construct_test_output = "\n".join(lines[1:])

                                construct_test_path = f"{self.root_dir}/output/{self.full_name.split('/')[0]}/{self.full_name.split('/')[1]}/construct_test_result.json"
                                # Ensure directory exists
                                import os

                                os.makedirs(
                                    os.path.dirname(construct_test_path), exist_ok=True
                                )
                                with open(construct_test_path, "w") as w4:
                                    w4.write(construct_test_output)
                                print(
                                    f"✅ Saved construct_test_result.json to {construct_test_path}"
                                )
                        except Exception as e:
                            print(f"❌ Failed to save construct_test_result.json: {e}")
                            import traceback

                            traceback.print_exc()
                        finish = True
                        break
                if finish:
                    break
            else:
                self.outer_commands[-1]["returncode"] = 2
                system_res += (
                    "ERROR! Your reply does not contain valid block or final answer."
                )

            # Guard errors so pwd failure doesn't break the loop
            try:
                current_directory, return_code = self.env_session.execute("$pwd$")
                current_directory = (
                    "\n[Current directory]:\n" + current_directory + "\n"
                )
            except Exception as e:
                # Fall back if pwd fails
                current_directory = "\n[Current directory]:\n/repo\n"
            system_res += current_directory
            system_res += f"You are currently in a [{self.env.namespace.replace('build_env_', '')}] container.\n"
            reminder = f"\nENVIRONMENT REMINDER: You have {self.max_turn - turn} turns left to complete the task."
            system_res += reminder
            success_cmds = parse_commands(self.env.commands)

            if len(success_cmds) > 0:
                appendix = (
                    "\nThe container successfully executed the following commands in order. Use this history to reflect and decide next steps. Remember: your goal is to configure the environment and pass tests.\n"
                    + "\n".join(success_cmds)
                )
            else:
                appendix = "\nThe container remains in its original state."
            pattern = (
                r'python\s+/home/tools/pip_download.py\s+-p\s+(\S+)\s+-v\s+""([^""]+)""'
            )

            # Pick the right package manager command based on use_uv
            pkg_manager = "uv pip install" if self.use_uv else "pip install"
            replacement = rf"{pkg_manager} \1\2"
            appendix = re.sub(pattern, replacement, appendix)
            pattern1 = r"python\s+/home/tools/pip_download.py\s+-p\s+(\S+)"
            replacement1 = rf"{pkg_manager} \1"
            appendix = re.sub(pattern1, replacement1, appendix)

            system_res += appendix
            if "gpt" in self.llm_name:
                system_message = {"role": "system", "content": system_res}
            else:
                system_message = {"role": "user", "content": system_res}
            self.messages.append(system_message)
            with open(
                f"{self.root_dir}/output/{self.full_name}/outer_commands.json", "w"
            ) as w1:
                w1.write(json.dumps(self.outer_commands, indent=4))
            with open(
                f"{self.root_dir}/output/{self.full_name}/inner_commands.json", "w"
            ) as w1:
                w1.write(json.dumps(self.env.commands, indent=4))
            print(self.colorize_output(system_res))

        # Auto-run tests when max turns reached and tests haven't run (multi-language)
        if turn >= self.max_turn:
            # Find test runners that have not been executed
            test_runners = self.language_config.get_test_runner_tools()
            for tool_name, tool_config in test_runners.items():
                # Check if executed (via stats count)
                tool_stats = self.tool_dispatcher.stats.get(tool_name, {})
                if tool_stats.get("count", 0) == 0:
                    print(
                        self.colorize_output(
                            f"\n⚠️  Max turns reached and {tool_name} not run; auto-running...\n"
                            f"\n⚠️  Max turns reached and {tool_name} not run; auto-running...\n"
                        )
                    )
                    try:
                        auto_output, auto_code = self.tool_dispatcher.dispatch(
                            tool_name
                        )
                        print(
                            self.colorize_output(
                                f"### Auto-run {tool_name} output:\n{auto_output}"
                            )
                        )

                        # Add auto-run info to history
                        auto_message = {
                            "role": "system",
                            "content": f"[SYSTEM AUTO-EXECUTION] Maximum turns reached without running {tool_name}. Automatically executed {tool_name}.\n### Observation:\n{auto_output}",
                        }
                        self.messages.append(auto_message)

                        # Record in outer_commands
                        self.outer_commands.append(
                            {
                                "command": f"{tool_name} (auto-executed)",
                                "returncode": auto_code,
                                "time": 0,
                                "auto_executed": True,
                            }
                        )
                    except Exception as e:
                        print(
                            self.colorize_output(
                                f"\n⚠️  Auto-run {tool_name} failed: {e}\n"
                            )
                        )

        update_trajectory(trajectory, self.messages, "configuration")

        end_time0 = time.time()
        cost_time = end_time0 - start_time0
        trajectory.append(
            {
                "agent": "configuration",
                "cost_time": cost_time,
                "cost_tokens": cost_tokens,
            }
        )

        # Save tool call statistics
        # self.tool_dispatcher.print_stats(detailed=False)
        # Print bash command stats
        self.print_bash_command_stats()
        # Save to host output dir
        stats_path = f"{self.root_dir}/output/{self.full_name}/tool_stats.json"
        self.tool_dispatcher.save_stats(filepath=stats_path)

        # Copy test results from container to host output dir (multi-language)
        self._copy_test_results_from_container()

        # Optionally save trajectory to output dir
        # trajectory_path = f'{self.root_dir}/output/{self.full_name}/trajectory.json'
        # with open(trajectory_path, 'w', encoding='utf-8') as f:
        #     json.dump(trajectory, f, ensure_ascii=False, indent=2)
        self.env_session.close()
        return trajectory, self.outer_commands
