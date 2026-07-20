#!/usr/bin/env python3
"""
run_npm_test.py - Run npm/yarn/pnpm tests in a container

Features:
1. Auto-detect package manager (npm/yarn/pnpm)
2. Run test command
3. Parse test output (supports Jest, Mocha, etc.)
4. Count passed/failed tests
5. Save results to a JSON file
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Tuple


def detect_package_manager(repo_path: str) -> str:
    """
    Detect which package manager the project uses.

    Args:
        repo_path: Repository path.

    Returns:
        'pnpm', 'yarn', or 'npm'
    """
    if os.path.exists(os.path.join(repo_path, "pnpm-lock.yaml")):
        return "pnpm"
    elif os.path.exists(os.path.join(repo_path, "yarn.lock")):
        return "yarn"
    else:
        return "npm"


def run_npm_test(
    repo_path: str, timeout: int = 900, verbose: bool = True
) -> Tuple[Dict, int]:
    """
    Run npm/yarn/pnpm tests.

    Args:
        repo_path: Repository path.
        timeout: Timeout in seconds.
        verbose: Whether to print verbose output.

    Returns:
        (result dict, return code)
    """
    # Detect package manager
    pkg_manager = detect_package_manager(repo_path)

    if pkg_manager == "pnpm":
        cmd = ["pnpm", "test"]
    elif pkg_manager == "yarn":
        cmd = ["yarn", "test"]
    else:
        cmd = ["npm", "test"]

    if verbose:
        print(f"🧪 Package manager: {pkg_manager}")
        print(f"🔧 Command: {' '.join(cmd)}")
        print(f"📁 Working directory: {repo_path}")
        print(f"⏱️  Timeout: {timeout}s")
        print("=" * 60)

    try:
        # Run test command
        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "CI": "true"},
        )

        output = result.stdout + "\n" + result.stderr

        if verbose:
            print("\n📋 Test output:")
            print(output)
            print("=" * 60)

        # Parse test result
        parsed_result = parse_test_output(output, pkg_manager, verbose)
        parsed_result["returncode"] = result.returncode
        parsed_result["raw_output"] = output if verbose else ""
        parsed_result["package_manager"] = pkg_manager

        # Mark success based on return code
        parsed_result["success"] = result.returncode == 0

        return parsed_result, result.returncode

    except subprocess.TimeoutExpired:
        error_result = {
            "command": " ".join(cmd),
            "success": False,
            "returncode": -1,
            "package_manager": pkg_manager,
            "summary": {
                "status": "TIMEOUT",
                "total_tests": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
            },
            "error_message": f"Test execution timed out ({timeout}s)",
            "raw_output": "",
        }
        return error_result, -1

    except Exception as e:
        error_result = {
            "command": " ".join(cmd),
            "success": False,
            "returncode": -1,
            "package_manager": pkg_manager,
            "summary": {
                "total_tests": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
            },
            "error_message": str(e),
            "raw_output": "",
        }
        return error_result, -1


def parse_test_output(output: str, pkg_manager: str, verbose: bool) -> Dict:
    """
    Parse test output.

    Supports output formats from Jest, Mocha, and other common test frameworks.

    Args:
        output: Output of the test command.
        pkg_manager: Package manager type.
        verbose: Whether to print verbose output.

    Returns:
        Parsed result dict.
    """
    result = {
        "command": f"{pkg_manager} test",
        "success": False,
        "summary": {
            "total_tests": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "status": "UNKNOWN",
        },
        "error_breakdown": {},
    }

    # Jest format
    # Example: "Tests: 5 failed, 10 passed, 15 total"
    jest_match = re.search(
        r"Tests?:\s+(?:(\d+)\s+failed,?\s*)?(?:(\d+)\s+passed,?\s*)?(\d+)\s+total",
        output,
        re.IGNORECASE,
    )
    if jest_match:
        failed = int(jest_match.group(1) or 0)
        passed = int(jest_match.group(2) or 0)
        total = int(jest_match.group(3) or 0)

        result["summary"]["total_tests"] = total
        result["summary"]["passed"] = passed
        result["summary"]["failed"] = failed
        result["success"] = failed == 0 and total > 0
        result["summary"]["status"] = "SUCCESS" if result["success"] else "FAILURE"

        if verbose:
            print("\n🎯 Detected Jest test framework")

    # Mocha format
    # Example: "15 passing (2s)" or "5 failing"
    elif "passing" in output or "failing" in output:
        passing_match = re.search(r"(\d+)\s+passing", output)
        failing_match = re.search(r"(\d+)\s+failing", output)
        pending_match = re.search(r"(\d+)\s+pending", output)

        passed = int(passing_match.group(1)) if passing_match else 0
        failed = int(failing_match.group(1)) if failing_match else 0
        skipped = int(pending_match.group(1)) if pending_match else 0

        total = passed + failed + skipped

        result["summary"]["total_tests"] = total
        result["summary"]["passed"] = passed
        result["summary"]["failed"] = failed
        result["summary"]["skipped"] = skipped
        result["success"] = failed == 0 and total > 0
        result["summary"]["status"] = "SUCCESS" if result["success"] else "FAILURE"

        if verbose:
            print("\n🎯 Detected Mocha test framework")

    # Generic failure detection
    else:
        # Check for obvious error markers
        if "error" in output.lower() or "fail" in output.lower():
            result["summary"]["status"] = "FAILURE"
            result["success"] = False
        # Check for "all tests passed" or similar markers
        elif "all tests passed" in output.lower() or "ok" in output.lower():
            result["summary"]["status"] = "SUCCESS"
            result["success"] = True
        else:
            result["summary"]["status"] = "UNKNOWN"

    # Extract test time
    time_patterns = [
        r"Time:\s+([\d.]+)\s*s",  # Jest: "Time: 2.5s"
        r"\(([\d.]+)s\)",  # Mocha: "(2s)"
        r"in\s+([\d.]+)\s*s",  # Generic: "in 2.5s"
    ]
    for pattern in time_patterns:
        time_match = re.search(pattern, output)
        if time_match:
            result["summary"]["test_time"] = f"{time_match.group(1)}s"
            break
    else:
        result["summary"]["test_time"] = "N/A"

    # Collect error types
    error_types = {}
    # Common error patterns
    error_patterns = [
        (r"ReferenceError", "ReferenceError"),
        (r"TypeError", "TypeError"),
        (r"SyntaxError", "SyntaxError"),
        (r"AssertionError", "AssertionError"),
        (r"Error: Cannot find module", "ModuleNotFoundError"),
        (r"ENOENT", "FileNotFoundError"),
    ]

    for pattern, error_name in error_patterns:
        count = len(re.findall(pattern, output))
        if count > 0:
            error_types[error_name] = count

    result["error_breakdown"] = error_types

    if verbose:
        print("\n📊 Parsed result:")
        print(f"  ✓ Success: {result['success']}")
        print(f"  📝 Total tests: {result['summary']['total_tests']}")
        print(f"  ✅ Passed: {result['summary']['passed']}")
        print(f"  ❌ Failed: {result['summary']['failed']}")
        print(f"  ⏭  Skipped: {result['summary'].get('skipped', 0)}")
        if result["summary"].get("test_time"):
            print(f"  ⏱  Time: {result['summary']['test_time']}")
        if error_types:
            print(f"  🐛 Error types: {error_types}")

    return result


def save_results(results: Dict, output_dir: str = "/repo/logs") -> str:
    """
    Save results to a JSON file.

    Args:
        results: Result dict.
        output_dir: Output directory.

    Returns:
        Saved file path.
    """
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, "run_npm_test_results.json")

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    return output_file


def main():
    """Main entry point."""
    repo_path = os.getenv("REPO_PATH", "/repo")

    if not os.path.exists(repo_path):
        print(f"❌ Repository path does not exist: {repo_path}", file=sys.stderr)
        sys.exit(1)

    # Check package.json
    package_json = os.path.join(repo_path, "package.json")
    if not os.path.exists(package_json):
        print(f"❌ package.json not found: {package_json}", file=sys.stderr)
        sys.exit(1)

    print("🧪 Starting tests...")

    results, returncode = run_npm_test(repo_path, timeout=900, verbose=True)

    # Save results
    output_file = save_results(results)
    print(f"\n💾 Results saved to: {output_file}")

    # Print summary
    print("\n" + "=" * 60)
    print("📊 Test summary")
    print("=" * 60)
    print(f"Status: {'✅ Success' if results['success'] else '❌ Failure'}")
    print(f"Package manager: {results.get('package_manager', 'N/A')}")
    print(f"Total tests: {results['summary']['total_tests']}")
    print(f"Passed: {results['summary']['passed']}")
    print(f"Failed: {results['summary']['failed']}")
    print(f"Skipped: {results['summary'].get('skipped', 0)}")
    if results["summary"].get("test_time"):
        print(f"Test time: {results['summary']['test_time']}")
    print("=" * 60)

    # Exit code: 0 all passed, 1 some failed
    sys.exit(0 if results["success"] else 1)


if __name__ == "__main__":
    main()
