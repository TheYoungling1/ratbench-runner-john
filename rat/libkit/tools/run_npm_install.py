#!/usr/bin/env python3
"""
run_npm_install.py - Run npm/yarn/pnpm dependency installation in a container

Features:
1. Auto-detect package manager (npm/yarn/pnpm)
2. Run install command
3. Parse install output and count errors/warnings
4. Generate a detailed install report
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


def run_npm_install(
    repo_path: str, timeout: int = 600, verbose: bool = True
) -> Tuple[Dict, int]:
    """
    Run npm/yarn/pnpm dependency installation.

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
        cmd = ["pnpm", "install"]
    elif pkg_manager == "yarn":
        cmd = ["yarn", "install"]
    else:
        cmd = ["npm", "install"]

    if verbose:
        print(f"📦 Package manager: {pkg_manager}")
        print(f"🔧 Command: {' '.join(cmd)}")
        print(f"📁 Working directory: {repo_path}")
        print(f"⏱️  Timeout: {timeout}s")
        print("=" * 60)

    try:
        # Run install command
        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "CI": "true"},  # Avoid interactive prompts
        )

        output = result.stdout + "\n" + result.stderr

        if verbose:
            print("\n📋 Install output:")
            print(output)
            print("=" * 60)

        # Parse install result
        parsed_result = parse_install_output(output, pkg_manager, verbose)
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
                "install_time": f"{timeout}s (timeout)",
            },
            "error_message": f"Dependency installation timed out ({timeout}s)",
            "errors": 1,
            "warnings": 0,
            "raw_output": "",
        }
        return error_result, -1

    except Exception as e:
        error_result = {
            "command": " ".join(cmd),
            "success": False,
            "returncode": -1,
            "package_manager": pkg_manager,
            "error_message": str(e),
            "errors": 1,
            "warnings": 0,
            "raw_output": "",
        }
        return error_result, -1


def parse_install_output(output: str, pkg_manager: str, verbose: bool) -> Dict:
    """
    Parse install output.

    Args:
        output: Output of the install command.
        pkg_manager: Package manager type.
        verbose: Whether to print verbose output.

    Returns:
        Parsed result dict.
    """
    result = {
        "command": f"{pkg_manager} install",
        "success": False,
        "summary": {},
        "errors": 0,
        "warnings": 0,
        "error_messages": [],
        "warning_messages": [],
    }

    # Check success (based on common success markers)
    if pkg_manager == "npm":
        # npm success markers
        if "added" in output.lower() or "up to date" in output.lower():
            result["success"] = True
            result["summary"]["status"] = "SUCCESS"
        # Count errors
        error_count = len(re.findall(r"npm ERR!", output))
        result["errors"] = error_count
        # Extract error messages
        error_matches = re.findall(r"npm ERR! (.+)", output)
        result["error_messages"] = error_matches[:10]  # Up to 10
    elif pkg_manager == "yarn":
        # yarn success markers
        if "Done in" in output or "Already up-to-date" in output:
            result["success"] = True
            result["summary"]["status"] = "SUCCESS"
        # Count errors
        error_count = len(re.findall(r"error ", output, re.IGNORECASE))
        result["errors"] = error_count
        # Extract error messages
        error_matches = re.findall(r"error (.+)", output, re.IGNORECASE)
        result["error_messages"] = error_matches[:10]
    elif pkg_manager == "pnpm":
        # pnpm success markers
        if "dependencies:" in output.lower() or "up to date" in output.lower():
            result["success"] = True
            result["summary"]["status"] = "SUCCESS"
        # Count errors
        error_count = len(re.findall(r"ERR_", output))
        result["errors"] = error_count
        # Extract error messages
        error_matches = re.findall(r"ERR_\w+ (.+)", output)
        result["error_messages"] = error_matches[:10]

    # Count warnings
    warning_count = len(re.findall(r"warn", output, re.IGNORECASE))
    result["warnings"] = warning_count
    warning_matches = re.findall(r"warn(?:ing)? (.+)", output, re.IGNORECASE)
    result["warning_messages"] = warning_matches[:10]

    # Extract install time
    time_match = re.search(r"Done in ([\d.]+)s", output)
    if time_match:
        result["summary"]["install_time"] = f"{time_match.group(1)}s"
    else:
        result["summary"]["install_time"] = "N/A"

    # If there are errors, force failure
    if result["errors"] > 0:
        result["success"] = False
        result["summary"]["status"] = "FAILURE"

    if verbose:
        print("\n📊 Parsed result:")
        print(f"  ✓ Success: {result['success']}")
        print(f"  ✗ Errors: {result['errors']}")
        print(f"  ⚠ Warnings: {result['warnings']}")
        if result.get("summary", {}).get("install_time"):
            print(f"  ⏱ Time: {result['summary']['install_time']}")

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
    output_file = os.path.join(output_dir, "run_npm_install_results.json")

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

    print("🚀 Starting dependency installation...")

    results, returncode = run_npm_install(repo_path, timeout=600, verbose=True)

    # Save results
    output_file = save_results(results)
    print(f"\n💾 Results saved to: {output_file}")

    # Print summary
    print("\n" + "=" * 60)
    print("📊 Install summary")
    print("=" * 60)
    print(f"Status: {'✅ Success' if results['success'] else '❌ Failure'}")
    print(f"Package manager: {results.get('package_manager', 'N/A')}")
    print(f"Errors: {results.get('errors', 0)}")
    print(f"Warnings: {results.get('warnings', 0)}")
    if results.get("summary", {}).get("install_time"):
        print(f"Install time: {results['summary']['install_time']}")
    print("=" * 60)

    # Exit code: 0 for success, 1 for error
    sys.exit(0 if results["success"] else 1)


if __name__ == "__main__":
    main()
