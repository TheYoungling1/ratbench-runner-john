#!/usr/bin/env python3
"""
run_pytest_collect.py - run pytest collection and detect errors

Features:
1. Run `python -m pytest --co -q` to collect tests
2. Detect collection-stage errors (ImportError, syntax errors, etc.)
3. Decide success based on return code
4. Save basic results to JSON (for debugging)

Note: test counting is handled by run-pytest; this script focuses on error detection.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Tuple, Optional


def check_pytest_installed() -> bool:
    """
    Check whether pytest is installed.

    Returns:
        True if pytest is installed, otherwise False.
    """
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "--version"],
            capture_output=True,
            text=True,
            timeout=100,
        )
        return result.returncode == 0
    except Exception:
        return False


def install_pytest() -> bool:
    """
    Try to install pytest.

    Returns:
        True if installation succeeds, otherwise False.
    """
    print("⚠️  pytest is not installed; attempting to install...")
    try:
        result = subprocess.run(
            ["pip", "install", "pytest"], capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            print("✅ pytest installed")
            return True
        else:
            print(f"❌ pytest install failed: {result.stderr}")
            return False
    except Exception as e:
        print(f"❌ pytest install error: {e}")
        return False


def detect_errors(output: str) -> list:
    """
    Detect key error lines from output.

    Args:
        output: pytest output text.

    Returns:
        A list of error lines.
    """
    errors = []
    # Match real error lines (not test names)
    # ERROR keywords are typically at line start or have explicit prefixes
    error_pattern = re.compile(
        r"^(ERROR|FAILED|ERRORS|E\s+)|"  # Line-start ERROR/FAILED
        r"(ImportError|ModuleNotFoundError|SyntaxError|AttributeError):|"  # Exception type + ':'
        r":\s*(ERROR|FAILED)\s*$",  # Line-end ERROR/FAILED
        re.IGNORECASE,
    )

    for line in output.split("\n"):
        line = line.strip()
        if error_pattern.search(line):
            errors.append(line)

    return errors


def run_pytest_collect(
    repo_path: Optional[str] = None, timeout: int = 60
) -> Tuple[Dict, int]:
    """
    Run `pytest --co -q` and collect results.

    Args:
        repo_path: Repo path; if None use CWD.
        timeout: Timeout in seconds.

    Returns:
        (result_dict, return_code)
    """
    # Default to CWD
    if repo_path is None:
        repo_path = os.getcwd()
    cmd = ["python", "-m", "pytest", "--co", "-q", repo_path]

    print(f"🔧 Command: {' '.join(cmd)}")
    print(f"📁 Working directory: {repo_path}")
    print("=" * 60)

    try:
        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True, timeout=timeout
        )

        output = result.stdout + "\n" + result.stderr

        print("\n📋 Pytest Collect output:")
        print(output)
        print("=" * 60)

        # Detect errors
        errors = detect_errors(output)

        # Determine success
        # 0: collection succeeded
        # 5: no tests collected (but collection itself succeeded)
        # other: collection failed
        success = result.returncode in [0, 5]

        collect_result = {
            "success": success,
            "returncode": result.returncode,
            "errors": errors,
            "raw_output": output,
        }

        return collect_result, result.returncode

    except subprocess.TimeoutExpired:
        error_result = {
            "success": False,
            "returncode": -1,
            "errors": [f"Pytest collect timed out ({timeout}s)"],
            "raw_output": "",
        }
        return error_result, -1

    except Exception as e:
        error_result = {
            "success": False,
            "returncode": -1,
            "errors": [f"Execution error: {str(e)}"],
            "raw_output": "",
        }
        return error_result, -1


def save_results(result: Dict, output_file: str):
    """
    Save basic results to a JSON file.

    Args:
        result: Result dict.
        output_file: Output file path.
    """
    try:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        print(f"💾 Results saved to: {output_file}")
    except Exception as e:
        print(f"⚠️  Failed to save results: {e}")


def main():
    """Main entrypoint - auto-detect working directory."""
    # Auto-detect current working directory
    repo_path = os.getcwd()
    timeout = 60
    output_file = os.path.join(repo_path, "logs/run_pytest_collect_results.json")

    print("=" * 60)
    print("🧪 Pytest Collect - collection error detection")
    print("=" * 60)

    # Validate repo path
    if not os.path.exists(repo_path):
        print(f"❌ Error: repo path does not exist: {repo_path}")
        return 1

    # Ensure pytest installed
    if not check_pytest_installed():
        if not install_pytest():
            print("❌ Error: failed to install pytest; install it manually")
            return 1

    # Run pytest collect
    print(f"\n🚀 Collecting tests...\n")
    result, returncode = run_pytest_collect(repo_path=repo_path, timeout=timeout)

    # Output
    print("\n" + "=" * 60)
    print("📊 Test collection result")
    print("=" * 60)

    if result["success"]:
        print("✅ Status: success")
    else:
        print("❌ Status: failed")
        if result["errors"]:
            print(f"\n⚠️  Detected {len(result['errors'])} errors:")
            for i, error in enumerate(result["errors"][:10], 1):
                print(f"  {i}. {error}")
            if len(result["errors"]) > 10:
                print(f"  ... and {len(result['errors']) - 10} more")

    print(f"\nReturn code: {returncode}")
    print("=" * 60)

    # Save
    save_results(result, output_file)

    # Return appropriate exit code
    if returncode == -1:
        print("\n❌ Collection timed out or errored")
        return -1
    elif returncode == 5:
        print("\n⚠️  No tests were collected")
        return 5
    elif not result["success"]:
        print("\n❌ Collection failed with errors")
        return 1
    else:
        print("\n✅ Test collection succeeded")
        return 0


if __name__ == "__main__":
    sys.exit(main())
