#!/usr/bin/env python3
"""
run_cargo_build.py - Run Cargo build in a container (skip tests)

Features:
1. Run `cargo build`
2. Parse build output and count compilation errors
3. Generate a detailed build report
4. Save results to a JSON file
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Tuple


def run_cargo_build(
    repo_path: str, timeout: int = 900, verbose: bool = True
) -> Tuple[Dict, int]:
    """
    Run Cargo build (without running tests).

    Args:
        repo_path: Repository path.
        timeout: Timeout in seconds.
        verbose: Whether to print verbose output.

    Returns:
        (result dict, return code)
    """
    # Use `cargo build` (no tests)
    cmd = ["cargo", "build"]

    if verbose:
        print("🦀 Using: Cargo")
        print(f"🔧 Command: {' '.join(cmd)}")
        print(f"📁 Working directory: {repo_path}")
        print(f"⏱️  Timeout: {timeout}s")
        print("=" * 60)

    try:
        # Run Cargo build
        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True, timeout=timeout
        )

        output = result.stdout + "\n" + result.stderr

        if verbose:
            print("\n📋 Cargo output:")
            print(output)
            print("=" * 60)

        # Parse build result
        parsed_result = parse_cargo_output(output, verbose)
        parsed_result["returncode"] = result.returncode
        parsed_result["raw_output"] = output if verbose else ""

        # Mark success based on return code
        parsed_result["success"] = result.returncode == 0

        return parsed_result, result.returncode

    except subprocess.TimeoutExpired:
        error_result = {
            "command": " ".join(cmd),
            "success": False,
            "returncode": -1,
            "summary": {
                "status": "TIMEOUT",
                "build_time": f"{timeout}s (timeout)",
            },
            "error_message": f"Cargo build timed out ({timeout}s)",
            "raw_output": "",
        }
        return error_result, -1

    except Exception as e:
        error_result = {
            "command": " ".join(cmd),
            "success": False,
            "returncode": -1,
            "error_message": str(e),
            "raw_output": "",
        }
        return error_result, -1


def parse_cargo_output(output: str, verbose: bool) -> Dict:
    """
    Parse Cargo build output.

    Args:
        output: Output from the Cargo command.
        verbose: Whether to print verbose output.

    Returns:
        Parsed result dict.
    """
    result = {
        "command": "cargo build",
        "success": False,
        "summary": {},
        "error_count": 0,
        "warning_count": 0,
        "error_messages": [],
        "warning_messages": [],
    }

    # Check success (based on a "Finished" marker)
    if "Finished" in output and "error" not in output.lower():
        result["success"] = True
        result["summary"]["status"] = "SUCCESS"
    elif "error" in output.lower():
        result["summary"]["status"] = "FAILURE"
    else:
        result["summary"]["status"] = "UNKNOWN"

    # Count errors
    # Cargo error formats: "error[E0425]:" or "error:"
    error_matches = re.findall(r"error(?:\[E\d+\])?:", output)
    result["error_count"] = len(error_matches)

    # Extract error messages (up to 10)
    error_detail_matches = re.findall(
        r"error(?:\[E\d+\])?: ([^\n]+)", output, re.MULTILINE
    )
    result["error_messages"] = error_detail_matches[:10]

    # Count warnings
    warning_matches = re.findall(r"warning:", output)
    result["warning_count"] = len(warning_matches)

    # Extract warning messages (up to 10)
    warning_detail_matches = re.findall(r"warning: ([^\n]+)", output, re.MULTILINE)
    result["warning_messages"] = warning_detail_matches[:10]

    # Extract build time
    # Cargo format: "Finished dev [unoptimized + debuginfo] target(s) in 2.34s"
    time_match = re.search(r"Finished .+ in ([\d.]+)s", output)
    if time_match:
        result["summary"]["build_time"] = f"{time_match.group(1)}s"
    else:
        result["summary"]["build_time"] = "N/A"

    # Extract build target info
    target_match = re.search(r"Finished (.+) target", output)
    if target_match:
        result["summary"]["build_target"] = target_match.group(1).strip()

    # If there are errors, force failure
    if result["error_count"] > 0:
        result["success"] = False
        result["summary"]["status"] = "FAILURE"
        if not result.get("error_message"):
            result["error_message"] = (
                f"Compilation failed: {result['error_count']} errors"
            )

    if verbose:
        print("\n📊 Parsed result:")
        print(f"  ✓ Success: {result['success']}")
        print(f"  ✗ Errors: {result['error_count']}")
        print(f"  ⚠ Warnings: {result['warning_count']}")
        if result.get("summary", {}).get("build_time"):
            print(f"  ⏱ Time: {result['summary']['build_time']}")
        if result.get("summary", {}).get("build_target"):
            print(f"  🎯 Target: {result['summary']['build_target']}")

    return result


def save_results(result: Dict, output_file: str):
    """
    Save results to a JSON file.

    Args:
        result: Result dict.
        output_file: Output file path.
    """
    try:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        print(f"\n💾 Results saved to: {output_file}")
    except Exception as e:
        print(f"\n⚠️  Failed to save results: {e}")


def main():
    """Main entry point."""
    repo_path = os.getcwd()
    output_file = os.path.join(repo_path, "logs/run_cargo_build_results.json")

    if not os.path.exists(repo_path):
        print(f"❌ Repository path does not exist: {repo_path}", file=sys.stderr)
        sys.exit(1)

    # Check Cargo.toml
    cargo_toml = os.path.join(repo_path, "Cargo.toml")
    if not os.path.exists(cargo_toml):
        print(f"❌ Cargo.toml not found: {cargo_toml}", file=sys.stderr)
        sys.exit(1)

    print("🦀 Starting Cargo build...")

    results, returncode = run_cargo_build(repo_path, timeout=900, verbose=True)

    # Save results
    save_results(results, output_file)
    print(f"\n💾 Results saved to: {output_file}")

    # Print summary
    print("\n" + "=" * 60)
    print("📊 Build summary")
    print("=" * 60)
    print(f"Status: {'✅ Success' if results['success'] else '❌ Failure'}")
    print(f"Errors: {results.get('error_count', 0)}")
    print(f"Warnings: {results.get('warning_count', 0)}")
    if results.get("summary", {}).get("build_time"):
        print(f"Build time: {results['summary']['build_time']}")
    if results.get("summary", {}).get("build_target"):
        print(f"Build target: {results['summary']['build_target']}")
    print("=" * 60)

    # Exit code: 0 for success, 1 for failure
    sys.exit(0 if results["success"] else 1)


if __name__ == "__main__":
    main()
