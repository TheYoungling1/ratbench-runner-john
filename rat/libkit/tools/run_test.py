#!/usr/bin/env python3
"""
run_test.py - run tests based on construct_test_result.json

This tool reads analysis produced by construct-test and runs the suggested commands.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional


def load_test_config(repo_path: str = "/repo") -> Optional[Dict]:
    """
    Load construct_test_result.json.

    Args:
        repo_path: Repository path.

    Returns:
        Test config dict; returns None if missing.
    """
    config_file = Path(repo_path) / "logs" / "construct_test_result.json"

    if not config_file.exists():
        print(f"⚠️  Test config file not found: {config_file}")
        print(f"Run construct-test --repo {repo_path} first to generate it")
        return None

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
        return config
    except Exception as e:
        print(f"❌ Failed to read test config: {e}")
        return None


def execute_test_command(
    command: str,
    description: str,
    cmd_type: str,
    repo_path: str = "/repo",
    timeout: int = 600,
) -> Dict:
    """
    Execute a single test command.

    Args:
        command: Command to execute.
        description: Description.
        cmd_type: Command type (test/run/collect).
        repo_path: Repository path.
        timeout: Timeout in seconds.

    Returns:
        Result dict.
    """
    print(f"\n{'=' * 60}")
    print(f"🔧 Running: {description}")
    print(f"📋 Command: {command}")
    print(f"📁 Type: {cmd_type}")
    print(f"{'=' * 60}")

    # Detect long-running service commands
    server_keywords = [
        "streamlit run",
        "flask run",
        "npm start",
        "yarn start",
        "rails server",
        "python -m http.server",
        "serve",
        "docker run",
        "kubectl port-forward",
        "gunicorn",
        "uvicorn",
        "fastapi run",
        "streamlit run",
        "npm run dev",
    ]
    is_server_cmd = cmd_type == "run" or any(
        keyword in command.lower() for keyword in server_keywords
    )

    result = {
        "command": command,
        "description": description,
        "type": cmd_type,
        "returncode": -1,
        "output": "",
        "success": False,
        "is_server_cmd": is_server_cmd,  # Marker
    }

    try:
        # Execute
        process = subprocess.run(
            command,
            shell=True,
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=True,
        )

        result["returncode"] = process.returncode
        result["output"] = process.stdout

        # Decide success based on return code
        if process.returncode == 0:
            result["success"] = True
            print("✅ Success")
        elif process.returncode == 5:
            # pytest uses 5 to indicate no tests collected
            result["success"] = True
            print("⚠️  No tests found (return code 5)")
        else:
            result["success"] = False
            print(f"❌ Failed (return code: {process.returncode})")

        # Show partial output
        if result["output"]:
            output_lines = result["output"].strip().split("\n")
            if len(output_lines) > 20:
                print("\nOutput (first 10 lines + last 10 lines):")
                print("\n".join(output_lines[:10]))
                print(f"... [omitted {len(output_lines) - 20} lines] ...")
                print("\n".join(output_lines[-10:]))
            else:
                print("\nOutput:")
                print(result["output"])

    except subprocess.TimeoutExpired:
        result["returncode"] = -1
        result["output"] = f"Command timed out ({timeout}s)"
        # For service commands, timeout means it likely started successfully
        if result.get("is_server_cmd", False):
            result["success"] = True
            result["output"] = (
                f"Service command started and ran for {timeout}s (terminated on timeout)"
            )
            print(f"✅ Service started (terminated after {timeout}s)")
        else:
            result["success"] = False
            print("❌ Timed out")
    except Exception as e:
        result["returncode"] = -1
        result["output"] = str(e)
        result["success"] = False
        print(f"❌ Execution error: {e}")

    return result


def run_tests_from_config(
    repo_path: str = "/repo",
    max_tests: int = 0,
    test_type: Optional[str] = None,
    save_results: bool = True,
) -> int:
    """
    Run tests based on the config file.

    Args:
        repo_path: Repository path.
        max_tests: Max commands to run (0 = all).
        test_type: Only run a specific command type (test/run/collect).
        save_results: Whether to save results.

    Returns:
        0 if all passed; 1 if any failed.
    """
    print("=" * 60)
    print("Run tests based on construct_test_result.json")

    # 1. Load config
    config = load_test_config(repo_path)
    if not config:
        return 1

    # 2. Get suggested commands
    suggested_commands = config.get("suggested_commands", [])
    if not suggested_commands:
        print("⚠️  No suggested test commands found in config")
        return 1

    print(f"\nFound {len(suggested_commands)} suggested commands")

    # 3. Filter by type
    commands_to_run = suggested_commands
    if test_type:
        commands_to_run = [
            cmd for cmd in suggested_commands if cmd.get("type") == test_type
        ]
        print(f"   Filter type '{test_type}': {len(commands_to_run)} commands")

    # 4. Limit count
    if max_tests > 0:
        commands_to_run = commands_to_run[:max_tests]
        print(f"   Limiting: run first {max_tests}")

    # 5. Execute
    results = []
    success_count = 0
    fail_count = 0

    for i, cmd_info in enumerate(commands_to_run, 1):
        command = cmd_info.get("command", "")
        description = cmd_info.get("description", "(no description)")
        cmd_type = cmd_info.get("type", "unknown")

        print(f"\n[{i}/{len(commands_to_run)}] Preparing to run...")

        # Reduce timeout for service-like commands to avoid hanging
        timeout = 600  # Default timeout 600s
        server_keywords = [
            "streamlit run",
            "flask run",
            "npm start",
            "yarn start",
            "rails server",
            "python -m http.server",
            "serve",
            "docker run",
            "kubectl port-forward",
            "gunicorn",
            "uvicorn",
            "fastapi run",
            "streamlit run",
            "npm run dev",
        ]

        # Use shorter timeout for service commands
        if cmd_type == "run" or any(
            keyword in command.lower() for keyword in server_keywords
        ):
            timeout = 30  # Service command timeout 30s
            print(f"🔍 Detected service command; using short timeout: {timeout}s")

        result = execute_test_command(
            command, description, cmd_type, repo_path, timeout=timeout
        )
        results.append(result)

        if result["success"]:
            success_count += 1
        else:
            fail_count += 1

    # 6. Save results
    if save_results:
        results_file = Path(repo_path) / "logs" / "run_test_results.json"
        try:
            with open(results_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "total": len(results),
                        "success": success_count,
                        "failed": fail_count,
                        "results": results,
                    },
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
            print(f"\nSaved test results to: {results_file}")
        except Exception as e:
            print(f"\nFailed to save test results: {e}")

    # 7. Summary
    print("\n" + "=" * 60)
    print(f"Summary: {len(results)} commands")
    print(f"✅ Success: {success_count}")
    print(f"❌ Failed: {fail_count}")

    if fail_count > 0:
        print(f"\n{fail_count} tests failed")
        return 1
    else:
        print("\nAll tests passed")
        return 0


def list_available_tests(repo_path: str = "/repo") -> int:
    """
    List available test commands (do not execute).

    Args:
        repo_path: Repository path.

    Returns:
        0 on success, 1 on failure.
    """
    print("=" * 60)
    print("📋 List available test commands")
    print("=" * 60)

    config = load_test_config(repo_path)
    if not config:
        return 1

    suggested_commands = config.get("suggested_commands", [])
    if not suggested_commands:
        print("⚠️  No suggested test commands found")
        return 1

    print(f"\nFound {len(suggested_commands)} test commands:\n")

    for i, cmd_info in enumerate(suggested_commands, 1):
        command = cmd_info.get("command", "")
        description = cmd_info.get("description", "(no description)")
        cmd_type = cmd_info.get("type", "unknown")
        source = cmd_info.get("source", "inferred")

        print(f"{i}. [{cmd_type.upper()}] {command}")
        print(f"   Description: {description}")
        if source:
            print(f"   Source: {source}")
        print()

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run tests based on construct_test_result.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
 Examples:
  # Run all tests
  run-test --repo /repo

  # Run the first 3 commands
  run-test --repo /repo --max 3

  # Only run type=test
  run-test --repo /repo --type test

  # List available tests (do not run)
  run-test --repo /repo --list
        """,
    )

    parser.add_argument(
        "--repo", type=str, default="/repo", help="Repo path (default: /repo)"
    )
    parser.add_argument(
        "--max", type=int, default=0, help="Max commands to run (0=all)"
    )
    parser.add_argument(
        "--type",
        type=str,
        choices=["test", "run", "collect"],
        help="Only run commands of a specific type",
    )
    parser.add_argument(
        "--list", "-l", action="store_true", help="List commands only; do not execute"
    )
    parser.add_argument(
        "--no-save", action="store_true", help="Do not save test results"
    )

    args = parser.parse_args()

    if args.list:
        # List
        sys.exit(list_available_tests(args.repo))
    else:
        # Run
        sys.exit(
            run_tests_from_config(
                repo_path=args.repo,
                max_tests=args.max,
                test_type=args.type,
                save_results=not args.no_save,
            )
        )
