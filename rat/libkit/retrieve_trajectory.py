#!/usr/bin/env python3
"""
retrieve_trajectory tool implementation.

Purpose: extract lessons learned from agent run trajectories.
Input: trajectory.json path.
Output: a summary of failure patterns to guide future setup and avoid repeating pointless attempts.
"""

import argparse
import json
import sys
import re
from pathlib import Path
from collections import defaultdict, Counter


class TrajectoryAnalyzer:
    """Trajectory analyzer."""

    def __init__(self, trajectory_path):
        self.trajectory_path = Path(trajectory_path)
        self.trajectory = []
        self.failed_commands = []
        self.repeated_failures = []
        self.error_patterns = []
        self.insights = []

    def load_trajectory(self):
        """Load the trajectory file."""
        try:
            with open(self.trajectory_path, "r", encoding="utf-8") as f:
                self.trajectory = json.load(f)
            print(f"✅ Loaded trajectory successfully: {len(self.trajectory)} entries")
            return True
        except FileNotFoundError:
            print(f"❌ Trajectory file not found: {self.trajectory_path}")
            return False
        except json.JSONDecodeError as e:
            print(f"❌ Invalid trajectory JSON format: {e}")
            return False
        except Exception as e:
            print(f"❌ Failed to load trajectory: {e}")
            return False

    def extract_commands_from_content(self, content):
        """Extract bash commands from content."""
        # Extract commands inside ```bash ... ``` blocks
        bash_pattern = r"```bash\s*\n(.*?)\n```"
        matches = re.findall(bash_pattern, content, re.DOTALL)

        commands = []
        for match in matches:
            # Split lines and drop empties
            lines = [line.strip() for line in match.split("\n") if line.strip()]
            # Split by &&
            for line in lines:
                cmds = [cmd.strip() for cmd in line.split("&&") if cmd.strip()]
                commands.extend(cmds)

        return commands

    def identify_failed_commands(self):
        """Identify failed commands."""
        print("\n📌 Analyzing failed commands...")

        for i, entry in enumerate(self.trajectory):
            if entry.get("role") != "assistant":
                continue

            # Extract commands from assistant output
            content = entry.get("content", "")
            commands = self.extract_commands_from_content(content)

            # Inspect the next (system/user) entry to judge failure
            if i + 1 < len(self.trajectory):
                next_entry = self.trajectory[i + 1]
                if next_entry.get("role") in ["user", "system"]:
                    response = next_entry.get("content", "")

                    # Error heuristics
                    is_error = any(
                        [
                            "ERROR" in response.upper(),
                            "FAILED" in response.upper(),
                            "command not found" in response.lower(),
                            "no such file" in response.lower(),
                            "permission denied" in response.lower(),
                            "cannot" in response.lower(),
                            "error:" in response.lower(),
                            "traceback" in response.lower(),
                            "exception" in response.lower(),
                            "returncode: 1" in response.lower(),
                            "returncode: 2" in response.lower(),
                            "Your reply does not contain valid block" in response,
                        ]
                    )

                    if is_error:
                        for cmd in commands:
                            self.failed_commands.append(
                                {
                                    "command": cmd,
                                    "response": response[
                                        :500
                                    ],  # Keep a snippet for analysis
                                    "turn": i // 2,  # Approximate turn
                                }
                            )

        print(f"✅ Found {len(self.failed_commands)} failed commands")
        return self.failed_commands

    def find_repeated_failures(self):
        """Find repeated failure patterns."""
        print("\n📌 Analyzing repeated failure patterns...")

        # Count failures per exact command
        command_counter = Counter([fc["command"] for fc in self.failed_commands])

        # Count failures per command type (first token)
        command_type_counter = Counter(
            [
                fc["command"].split()[0] if fc["command"].split() else ""
                for fc in self.failed_commands
            ]
        )

        # Repeated failures (>= 2)
        for cmd, count in command_counter.items():
            if count >= 2:
                self.repeated_failures.append(
                    {"command": cmd, "count": count, "type": "exact_command"}
                )

        # Repeated failures by command type
        for cmd_type, count in command_type_counter.items():
            if count >= 3 and cmd_type:  # At least 3
                self.repeated_failures.append(
                    {"command_type": cmd_type, "count": count, "type": "command_type"}
                )

        print(f"✅ Found {len(self.repeated_failures)} repeated failure patterns")
        return self.repeated_failures

    def extract_error_patterns(self):
        """Extract common error patterns."""
        print("\n📌 Extracting error patterns...")

        error_keywords = [
            "command not found",
            "no such file or directory",
            "permission denied",
            "cannot find",
            "module not found",
            "import error",
            "syntax error",
            "connection refused",
            "timeout",
            "failed to",
            "unable to",
            "not installed",
        ]

        error_pattern_counter = defaultdict(list)

        for fc in self.failed_commands:
            response_lower = fc["response"].lower()
            for keyword in error_keywords:
                if keyword in response_lower:
                    error_pattern_counter[keyword].append(fc["command"])

        # Keep patterns that appear multiple times
        self.error_patterns = [
            {
                "pattern": pattern,
                "count": len(commands),
                "examples": list(set(commands[:3])),  # Up to 3 examples
            }
            for pattern, commands in error_pattern_counter.items()
            if len(commands) >= 1
        ]

        print(f"✅ Found {len(self.error_patterns)} common error patterns")
        return self.error_patterns

    def check_environment_state(self):
        """Check whether the environment stayed in its original state."""
        print("\n📌 Checking environment state...")

        # Count occurrences of "container remains in its original state"
        original_state_count = 0
        total_observations = 0

        for entry in self.trajectory:
            if entry.get("role") in ["user", "system"]:
                content = entry.get("content", "")
                if "Observation:" in content:
                    total_observations += 1
                    if "remains in its original state" in content.lower():
                        original_state_count += 1

        if total_observations > 0 and original_state_count / total_observations > 0.5:
            return {
                "always_original": True,
                "percentage": original_state_count / total_observations,
                "message": f"Environment stayed in its original state for {original_state_count}/{total_observations} observations",
            }

        return {
            "always_original": False,
            "percentage": original_state_count / total_observations
            if total_observations > 0
            else 0,
            "message": f"Environment stayed in its original state for {original_state_count}/{total_observations} observations",
        }

    def generate_insights(self):
        """Generate insights and recommendations."""
        print("\n📌 Generating configuration recommendations...")

        # 1. Recommendations for repeated failures
        if self.repeated_failures:
            exact_commands = [
                rf for rf in self.repeated_failures if rf["type"] == "exact_command"
            ]
            if exact_commands:
                self.insights.append(
                    {
                        "category": "Repeated failing commands",
                        "severity": "high",
                        "items": [
                            f"Command `{rf['command']}` failed {rf['count']} times; try a different approach"
                            for rf in exact_commands[:5]
                        ],
                    }
                )

            command_types = [
                rf for rf in self.repeated_failures if rf["type"] == "command_type"
            ]
            if command_types:
                self.insights.append(
                    {
                        "category": "Failing command types",
                        "severity": "medium",
                        "items": [
                            f"Command type `{rf['command_type']}` failed {rf['count']} times; it may not apply to this environment"
                            for rf in command_types[:5]
                        ],
                    }
                )

        # 2. Error pattern recommendations
        if self.error_patterns:
            top_patterns = sorted(
                self.error_patterns, key=lambda x: x["count"], reverse=True
            )[:5]
            self.insights.append(
                {
                    "category": "Common error patterns",
                    "severity": "high",
                    "items": [
                        f"Error '{ep['pattern']}' appeared {ep['count']} times; commands: {', '.join(ep['examples'][:2])}"
                        for ep in top_patterns
                    ],
                }
            )

        # 3. Environment state recommendations
        env_state = self.check_environment_state()
        if env_state["always_original"]:
            self.insights.append(
                {
                    "category": "Environment configuration state",
                    "severity": "critical",
                    "items": [
                        f"⚠️ {env_state['message']}; this suggests most commands never executed successfully",
                        "Recommendation: check command syntax, paths, and permissions",
                        "Recommendation: verify that your setup approach fits this repository",
                    ],
                }
            )

        # 4. Tool invocation recommendations
        tool_failures = [
            fc
            for fc in self.failed_commands
            if any(
                tool in fc["command"]
                for tool in ["construct test", "search", "retrieve"]
            )
        ]
        if tool_failures:
            self.insights.append(
                {
                    "category": "Tool invocation issues",
                    "severity": "medium",
                    "items": [
                        f"Tool commands failed {len(tool_failures)} times",
                        "Tip: these tools must be invoked via Python scripts, not as direct bash commands",
                        "Example: use `python /home/tools/construct_test.py` instead of `construct test`",
                    ],
                }
            )

        print(f"✅ Generated {len(self.insights)} recommendations")
        return self.insights

    def format_output(self):
        """Format human-readable output."""
        output = []
        output.append("=" * 70)
        output.append("📋 Trajectory analysis results - configuration lessons")
        output.append("=" * 70)
        output.append("")

        if not self.insights:
            output.append(
                "✅ No obvious failure patterns found; the configuration process looks smooth"
            )
            return "\n".join(output)

        for i, insight in enumerate(self.insights, 1):
            severity_emoji = {
                "critical": "🔴",
                "high": "🟠",
                "medium": "🟡",
                "low": "🟢",
            }
            emoji = severity_emoji.get(insight["severity"], "⚪")

            output.append(f"{emoji} {i}. {insight['category']}")
            output.append("-" * 70)
            for item in insight["items"]:
                output.append(f"   • {item}")
            output.append("")

        output.append("=" * 70)
        output.append("💡 Overall recommendations:")
        output.append("-" * 70)

        # Generate high-level tips
        if any(insight["severity"] == "critical" for insight in self.insights):
            output.append(
                "   ⚠️ Critical issues detected; reconsider the overall setup strategy"
            )

        if self.repeated_failures:
            output.append(
                "   • Avoid retrying commands that have already failed repeatedly"
            )
            output.append("   • Analyze failures and try alternative setup approaches")

        if self.error_patterns:
            output.append(
                "   • Watch for common error patterns and address them directly"
            )

        output.append("   • Refer to historical successful trajectories (if any)")
        output.append(
            "   • Verify basics: file paths, command syntax, environment dependencies"
        )

        output.append("=" * 70)

        return "\n".join(output)

    def save_result(self, output_path="/tmp/retrieve_trajectory_result.json"):
        """Save analysis results to a file."""
        result = {
            "total_entries": len(self.trajectory),
            "failed_commands_count": len(self.failed_commands),
            "repeated_failures_count": len(self.repeated_failures),
            "error_patterns_count": len(self.error_patterns),
            "failed_commands": self.failed_commands[:20],  # Save up to 20
            "repeated_failures": self.repeated_failures,
            "error_patterns": self.error_patterns,
            "insights": self.insights,
        }

        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            print(f"\n💾 Results saved to: {output_path}")
            return True
        except Exception as e:
            print(f"⚠️ Failed to save results: {e}")
            return False


def retrieve_trajectory_main(trajectory_path="trajectory.json"):
    """
    Main function: analyze a trajectory and extract configuration lessons.

    Args:
        trajectory_path: Path to the trajectory file.

    Returns:
        0: Success
        1: Failure
    """
    print("=" * 70)
    print("🔍 Retrieve Trajectory - analyzing configuration trajectory")
    print("=" * 70)
    print()

    analyzer = TrajectoryAnalyzer(trajectory_path)

    # 1. Load
    if not analyzer.load_trajectory():
        return 1

    # 2. Identify failed commands
    analyzer.identify_failed_commands()

    # 3. Find repeated failures
    analyzer.find_repeated_failures()

    # 4. Extract error patterns
    analyzer.extract_error_patterns()

    # 5. Generate insights
    analyzer.generate_insights()

    # 6. Format output
    output = analyzer.format_output()
    print()
    print(output)

    # 7. Save
    analyzer.save_result()

    print()
    print("✅ Trajectory analysis completed")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Analyze agent trajectory and extract configuration insights"
    )
    parser.add_argument(
        "--trajectory",
        type=str,
        default="trajectory.json",
        help="Path to trajectory JSON file",
    )

    args = parser.parse_args()

    sys.exit(retrieve_trajectory_main(args.trajectory))
