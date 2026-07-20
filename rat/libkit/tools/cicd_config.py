#!/usr/bin/env python3
"""
cicd_config.py - Configure environment from GitHub CI/CD workflows

Features:
1. Scan `.github/workflows/*.yml`
2. Parse GitHub Actions workflow configuration
3. Convert setup steps into executable commands
4. Optional: execute the commands

Usage:
  python cicd_config.py --repo /path/to/repo [--execute] [--output output.json]

Args:
  --repo      Repository path (default: /repo)
  --execute   Execute commands (default: false; print only)
  --output    Save parsed result to JSON file
  --workflow  Specify workflow file name (default: all workflows)
"""

import argparse
import json
import os
import sys
import subprocess
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Any

# Try to import yaml
try:
    import yaml
except ImportError:
    print("❌ PyYAML not found. Install: pip install pyyaml")
    sys.exit(1)

# Add libkit to path
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

try:
    from llm import LLMChat
except ImportError:

    class FallbackLLMChat:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, *args, **kwargs):
            return ("LLM is not available", None)

    LLMChat = FallbackLLMChat


class GitHubActionsParser:
    """GitHub Actions workflow parser."""

    # Common GitHub Actions and conversion rules
    ACTION_MAPPINGS = {
        "actions/checkout": {
            "description": "Checkout repository",
            "command": None,  # Usually unnecessary in a container
            "skip": True,
        },
        "actions/setup-python": {
            "description": "Set up Python",
            "command": lambda params: f"python -m pip install --upgrade pip && python -m pip install virtualenv",
            "condition": lambda params: True,
        },
        "actions/setup-node": {
            "description": "Set up Node.js",
            "command": lambda params: f"curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs",
            "condition": lambda params: True,
        },
        "docker/setup-buildx-action": {
            "description": "Set up Docker Buildx",
            "command": lambda params: "docker buildx create --name builder --use && docker buildx inspect --bootstrap",
            "condition": lambda params: True,
        },
        "actions/cache": {
            "description": "Cache dependencies",
            "command": None,
            "skip": True,
        },
        "actions/upload-artifact": {
            "description": "Upload artifacts",
            "command": None,
            "skip": True,
        },
        "actions/download-artifact": {
            "description": "Download artifacts",
            "command": None,
            "skip": True,
        },
        "astral-sh/setup-uv": {
            "description": "Install uv (Python package manager)",
            "command": lambda params: "curl -LsSf https://astral.sh/uv/install.sh | sh",
            "condition": lambda params: True,
        },
        "pypa/gh-action-pypi-publish": {
            "description": "Publish to PyPI",
            "command": None,
            "skip": True,
        },
        "actions/setup-go": {
            "description": "Set up Go",
            "command": lambda params: "apt-get update && apt-get install -y golang",
            "condition": lambda params: True,
        },
        "actions/setup-java": {
            "description": "Set up Java",
            "command": lambda params: "apt-get update && apt-get install -y default-jdk",
            "condition": lambda params: True,
        },
        "codecov/codecov-action": {
            "description": "Upload coverage report",
            "command": None,
            "skip": True,
        },
        "coverallsapp/github-action": {
            "description": "Upload coverage report to Coveralls",
            "command": None,
            "skip": True,
        },
    }

    def __init__(self, repo_path: str, use_uv: bool = False):
        self.repo_path = Path(repo_path)
        self.workflows_dir = self.repo_path / ".github" / "workflows"
        self.workflows = []
        self.use_uv = use_uv

        # Update Python setup command based on use_uv
        if use_uv:
            self.ACTION_MAPPINGS["actions/setup-python"]["command"] = (
                lambda params: f"python -m pip install --upgrade pip && python -m pip install virtualenv"
            )
        else:
            self.ACTION_MAPPINGS["actions/setup-python"]["command"] = (
                lambda params: f"python -m pip install --upgrade pip && python -m pip install virtualenv"
            )

    def find_workflows(self) -> List[Path]:
        """Find all workflow files."""
        if not self.workflows_dir.exists():
            print(f"⚠️  Workflows directory not found: {self.workflows_dir}")
            return []

        workflows = list(self.workflows_dir.glob("*.yml")) + list(
            self.workflows_dir.glob("*.yaml")
        )
        print(f"📁 Found {len(workflows)} workflow file(s)")
        return workflows

    def parse_workflow(self, workflow_path: Path) -> Optional[Dict[str, Any]]:
        """Parse a single workflow file."""
        try:
            with open(workflow_path, "r", encoding="utf-8") as f:
                content = yaml.safe_load(f)

            workflow_info = {
                "file": str(workflow_path.relative_to(self.repo_path)),
                "name": content.get("name", "Unnamed workflow"),
                "on": content.get("on", {}),
                "jobs": [],
            }

            # Parse jobs
            for job_name, job_config in content.get("jobs", {}).items():
                job_info = self._parse_job(job_name, job_config)
                workflow_info["jobs"].append(job_info)

            return workflow_info

        except yaml.YAMLError as e:
            print(f"❌ Failed to parse YAML file {workflow_path}: {e}")
            return None
        except Exception as e:
            print(f"❌ Failed to parse workflow {workflow_path}: {e}")
            return None

    def _parse_job(self, job_name: str, job_config: Dict) -> Dict[str, Any]:
        """Parse a single job."""
        job_info = {
            "name": job_name,
            "runs-on": job_config.get("runs-on", "ubuntu-latest"),
            "steps": [],
            "commands": [],  # Converted commands
            "strategy": job_config.get("strategy", {}),
            "needs": job_config.get("needs", []),
            "if": job_config.get("if", ""),
        }

        # Parse steps
        for step in job_config.get("steps", []):
            step_info = self._parse_step(step)
            job_info["steps"].append(step_info)

            # Convert step -> command
            command = self._step_to_command(step)
            if command:
                job_info["commands"].append(command)

        return job_info

    def _parse_step(self, step: Dict) -> Dict[str, Any]:
        """Parse a single step."""
        step_info = {
            "name": step.get("name", "Unnamed step"),
            "uses": step.get("uses"),
            "run": step.get("run"),
            "with": step.get("with", {}),
            "env": step.get("env", {}),
            "if": step.get("if", ""),
            "id": step.get("id", ""),
        }
        return step_info

    def _step_to_command(self, step: Dict) -> Optional[str]:
        """Convert a step into an executable command."""
        # Handle `uses`
        if "uses" in step:
            action = step["uses"]
            # Strip version suffix
            action_name = action.split("@")[0] if "@" in action else action

            if action_name in self.ACTION_MAPPINGS:
                mapping = self.ACTION_MAPPINGS[action_name]
                if mapping.get("skip"):
                    return None
                elif mapping.get("command"):
                    # Action with parameters
                    params = step.get("with", {})
                    try:
                        return mapping["command"](params)
                    except Exception as e:
                        print(f"⚠️  Failed to generate command for {action}: {e}")
                        return None
            else:
                # Unknown action
                print(f"⚠️  Unknown action: {action}; skipping")
                return None

        # Handle `run`
        elif "run" in step:
            command = step["run"]

            # Shell
            shell = step.get("shell", "bash")
            if shell != "bash":
                print(f"⚠️  Non-bash shell: {shell}; command may be incompatible")

            # Normalize multi-line commands
            if "\n" in command:
                # Multi-line command
                lines = [
                    line.strip() for line in command.strip().split("\n") if line.strip()
                ]
                # Simple join (may need more complex logic)
                command = " && ".join(lines)

            return command

        return None

    def get_all_commands(self, workflow_info: Dict) -> List[str]:
        """Extract all commands from workflow info."""
        all_commands = []

        for job in workflow_info.get("jobs", []):
            # Job header
            job_comment = f"# Job: {job['name']} (runs-on: {job['runs-on']})"
            all_commands.append(job_comment)

            # Commands
            for command in job["commands"]:
                if command:
                    all_commands.append(command)

            all_commands.append("")  # Separator

        return all_commands

    def validate_commands(self, commands: List[str]) -> List[str]:
        """Validate command safety."""
        safe_commands = []
        dangerous_patterns = [
            r"rm\s+-rf\s+/",
            r"chmod\s+777\s+/",
            r"format\s+/dev/",
            r"dd\s+if=/dev/",
            r":\(\)\{:\|:&\}:",  # fork bomb
        ]

        for cmd in commands:
            if cmd.startswith("#"):
                safe_commands.append(cmd)
                continue

            is_dangerous = any(
                re.search(pattern, cmd) for pattern in dangerous_patterns
            )
            if is_dangerous:
                print(f"⚠️  Skipping dangerous command: {cmd}")
                safe_commands.append(f"# Skipped dangerous command: {cmd}")
            else:
                safe_commands.append(cmd)

        return safe_commands


class CICDConfigurator:
    """CI/CD configurator."""

    def __init__(self, repo_path: str, use_llm: bool = False, use_uv: bool = False):
        self.repo_path = repo_path
        self.parser = GitHubActionsParser(repo_path, use_uv=use_uv)
        self.use_llm = use_llm
        self.use_uv = use_uv

        if use_llm:
            self.llm = LLMChat("deepseek-chat")
        else:
            self.llm = None

    def analyze_workflows(self) -> List[Dict]:
        """Analyze all workflows."""
        workflows = self.parser.find_workflows()
        if not workflows:
            return []

        workflow_infos = []
        for wf in workflows:
            info = self.parser.parse_workflow(wf)
            if info:
                workflow_infos.append(info)

        return workflow_infos

    def generate_config_script(self, workflow_info: Dict) -> str:
        """Generate a configuration script."""
        commands = self.parser.get_all_commands(workflow_info)
        safe_commands = self.parser.validate_commands(commands)

        script_lines = [
            "#!/bin/bash",
            "# Auto-generated CI/CD configuration script",
            f"# Workflow: {workflow_info['name']}",
            f"# File: {workflow_info['file']}",
            "#",
            "set -e  # Exit on error",
            "echo '🚀 Applying CI/CD configuration...'",
            "",
        ]

        for cmd in safe_commands:
            if cmd.startswith("#"):
                script_lines.append(cmd)
            elif cmd.strip():
                script_lines.append(f"echo '▶️  Run: {cmd}'")
                script_lines.append(cmd)
            else:
                script_lines.append("")

        script_lines.extend(["", "echo '✅ CI/CD configuration complete'", ""])

        return "\n".join(script_lines)

    def execute_commands(self, commands: List[str]) -> bool:
        """Execute a list of commands."""
        print("🚀 Executing CI/CD configuration commands...")

        for i, cmd in enumerate(commands, 1):
            if not cmd.strip() or cmd.startswith("#"):
                continue

            print(f"\n[{i}/{len(commands)}] ▶️  {cmd}")
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 minute timeout
                )

                if result.returncode == 0:
                    print("   ✅ Success")
                    if result.stdout.strip():
                        print(f"   Output: {result.stdout[:200]}...")
                else:
                    print(f"   ❌ Failure (code: {result.returncode})")
                    print(f"   Error: {result.stderr[:200]}...")
                    # Whether to continue depends on error type
                    if (
                        "not found" in result.stderr
                        or "command not found" in result.stderr
                    ):
                        print("   ⚠️  Skipping remaining commands")
                        return False
            except subprocess.TimeoutExpired:
                print("   ⏰ Timeout")
                return False
            except Exception as e:
                print(f"   ❌ Error: {e}")
                return False

        print("\n✅ All commands completed")
        return True

    def llm_enhance_analysis(self, workflow_info: Dict) -> Dict:
        """Use LLM to enhance workflow analysis."""
        if not self.llm:
            return workflow_info

        try:
            prompt = f"""Analyze the following GitHub Actions workflow and suggest environment setup.

Workflow name: {workflow_info["name"]}
File: {workflow_info["file"]}

Jobs:
{json.dumps(workflow_info["jobs"], indent=2, ensure_ascii=False)}

Provide:
1. Primary tech stack
2. Key dependencies
3. Potential environment issues
4. Optimization suggestions

Return JSON:
{{
  "tech_stack": ["..."],
  "key_dependencies": ["..."],
  "potential_issues": ["..."],
  "optimization_suggestions": ["..."]
}}
"""
            response, _ = self.llm.chat(
                [
                    {
                        "role": "system",
                        "content": "You are a DevOps expert. Return JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )

            # Parse LLM response
            import re

            json_match = re.search(r"\{.*\}", str(response or ""), re.DOTALL)
            if json_match:
                llm_analysis = json.loads(json_match.group())
                workflow_info["llm_analysis"] = llm_analysis

        except Exception as e:
            print(f"⚠️  LLM analysis failed: {e}")

        return workflow_info


def main():
    parser = argparse.ArgumentParser(
        description="Configure environment from GitHub CI/CD workflows",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze workflows only
  python cicd_config.py --repo /path/to/repo

  # Analyze and execute commands
  python cicd_config.py --repo /path/to/repo --execute

  # Save analysis result to file
  python cicd_config.py --repo /path/to/repo --output analysis.json

  # Use LLM-enhanced analysis
  python cicd_config.py --repo /path/to/repo --llm

  # Select a specific workflow file
  python cicd_config.py --repo /path/to/repo --workflow test.yml
        """,
    )

    parser.add_argument(
        "--repo", type=str, default="/repo", help="Repository path (default: /repo)"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute commands (default: false; print only)",
    )
    parser.add_argument("--output", type=str, help="Save parsed result to JSON file")
    parser.add_argument(
        "--workflow", type=str, help="Workflow file name (default: all workflows)"
    )
    parser.add_argument(
        "--llm", action="store_true", help="Use LLM to enhance analysis"
    )
    parser.add_argument(
        "--use-uv", action="store_true", help="Use uv as Python package manager"
    )

    args = parser.parse_args()

    print("=" * 70)
    print("🔧 GitHub CI/CD configurator")
    print("=" * 70)

    # Validate repo path
    if not os.path.exists(args.repo):
        print(f"❌ Repository path does not exist: {args.repo}")
        return 1

    # Initialize configurator
    configurator = CICDConfigurator(args.repo, use_llm=args.llm, use_uv=args.use_uv)

    # Analyze workflows
    workflow_infos = configurator.analyze_workflows()
    if not workflow_infos:
        print("❌ No usable workflow files found")
        return 1

    print(f"✅ Analyzed {len(workflow_infos)} workflow(s)")

    # Process each workflow
    for workflow_info in workflow_infos:
        print(f"\n📋 Workflow: {workflow_info['name']}")
        print(f"📁 File: {workflow_info['file']}")

        # LLM-enhanced analysis
        if args.llm:
            workflow_info = configurator.llm_enhance_analysis(workflow_info)

        # Generate script
        script = configurator.generate_config_script(workflow_info)

        # Print script
        print("\n📜 Generated script:")
        print("-" * 50)
        print(script)
        print("-" * 50)

        # Collect commands for execution
        all_commands = []
        for job in workflow_info["jobs"]:
            all_commands.extend(job["commands"])

        # Execute
        if args.execute and all_commands:
            print(f"\n🚀 Ready to run {len(all_commands)} commands...")
            confirm = input("Continue? (y/N): ")
            if confirm.lower() == "y":
                success = configurator.execute_commands(all_commands)
                if not success:
                    print("❌ Execution failed")
                    return 1
            else:
                print("⏸️  Cancelled")

        # Save result
        if args.output:
            try:
                output_data = {
                    "workflow": workflow_info,
                    "script": script,
                    "commands": all_commands,
                    "repo": args.repo,
                    "timestamp": time.time(),
                }
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(output_data, f, indent=2, ensure_ascii=False)
                print(f"\n💾 Saved to: {args.output}")
            except Exception as e:
                print(f"❌ Save failed: {e}")

    print("\n" + "=" * 70)
    print("✅ Done")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    import time

    sys.exit(main())
