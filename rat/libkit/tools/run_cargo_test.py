#!/usr/bin/env python3
"""
run_cargo_test.py - Run Cargo tests in a container

Features:
1. Run `cargo test`
2. Parse test output and count passed/failed tests
3. Generate a detailed test report
4. Save results to a JSON file
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Tuple


def run_cargo_test(
    repo_path: str, timeout: int = 900, verbose: bool = True
) -> Tuple[Dict, int]:
    """
    Run Cargo tests.

    Args:
        repo_path: Repository path.
        timeout: Timeout in seconds.
        verbose: Whether to print verbose output.

    Returns:
        (result dict, return code)
    """
    # Use `cargo test`
    cmd = ["cargo", "test"]

    if verbose:
        print("🦀 Using: Cargo Test")
        print(f"🔧 Command: {' '.join(cmd)}")
        print(f"📁 Working directory: {repo_path}")
        print(f"⏱️  Timeout: {timeout}s")
        print("=" * 60)

    try:
        # Run Cargo tests
        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True, timeout=timeout
        )

        output = result.stdout + "\n" + result.stderr

        if verbose:
            print("\n📋 Cargo test output:")
            print(output)
            print("=" * 60)

        # Parse test result; use returncode to determine success
        parsed_result = parse_cargo_test_output(output, verbose, result.returncode)
        parsed_result["returncode"] = result.returncode
        parsed_result["raw_output"] = output if verbose else ""

        return parsed_result, result.returncode

    except subprocess.TimeoutExpired:
        error_result = {
            "command": " ".join(cmd),
            "success": False,
            "returncode": -1,
            "summary": {
                "status": "TIMEOUT",
                "total_tests": 0,
                "passed": 0,
                "failed": 0,
                "ignored": 0,
                "test_time": f"{timeout}s (timeout)",
            },
            "error_message": f"Cargo test timed out ({timeout}s)",
            "error_breakdown": {},
            "raw_output": "",
        }
        return error_result, -1

    except Exception as e:
        error_result = {
            "command": " ".join(cmd),
            "success": False,
            "returncode": -1,
            "summary": {
                "total_tests": 0,
                "passed": 0,
                "failed": 0,
                "ignored": 0,
            },
            "error_message": str(e),
            "error_breakdown": {},
            "raw_output": "",
        }
        return error_result, -1


def parse_cargo_test_output(output: str, verbose: bool, returncode: int) -> Dict:
    """
    Parse Cargo test output.

    Args:
        output: Output from the Cargo test command.
        verbose: Whether to print verbose output.
        returncode: Return code from `cargo test`.

    Returns:
        Parsed result dict.
    """
    result = {
        "command": "cargo test",
        "success": returncode == 0,  # Determine success by return code
        "summary": {
            "total_tests": 0,
            "passed": 0,
            "failed": 0,
            "ignored": 0,
            "status": "UNKNOWN",
        },
        "error_breakdown": {},
    }

    # Cargo test result formats:
    # "test result: ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out"
    # or
    # "test result: FAILED. 3 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out"

    result_match = re.search(
        r"test result: (\w+)\.\s+(\d+) passed;\s+(\d+) failed;\s+(\d+) ignored",
        output,
        re.MULTILINE,
    )

    if result_match:
        status = result_match.group(1).upper()
        passed = int(result_match.group(2))
        failed = int(result_match.group(3))
        ignored = int(result_match.group(4))

        total = passed + failed + ignored

        result["summary"]["status"] = status
        result["summary"]["total_tests"] = total
        result["summary"]["passed"] = passed
        result["summary"]["failed"] = failed
        result["summary"]["ignored"] = ignored
    else:
        # If no summary found, infer status from output
        if "test result: ok" in output.lower():
            result["summary"]["status"] = "OK"
        elif "test result: FAILED" in output or "error" in output.lower():
            result["summary"]["status"] = "FAILED"

    # Extract test time
    # Cargo format: "test result: ok. ... in 1.23s"
    time_match = re.search(r"Finished .+ in ([\d.]+)s", output)
    if not time_match:
        time_match = re.search(r"in ([\d.]+)s", output)
    if time_match:
        result["summary"]["test_time"] = f"{time_match.group(1)}s"
    else:
        result["summary"]["test_time"] = "N/A"

    # Collect error types (from failed tests)
    error_types = {}

    # Find panic messages
    panic_count = len(re.findall(r"thread .+ panicked at", output))
    if panic_count > 0:
        error_types["Panic"] = panic_count

    # Find assertion failures
    assertion_count = len(re.findall(r"assertion failed", output, re.IGNORECASE))
    if assertion_count > 0:
        error_types["AssertionError"] = assertion_count

    # Find other common errors
    if "no such file or directory" in output.lower():
        error_types["FileNotFoundError"] = error_types.get("FileNotFoundError", 0) + 1

    result["error_breakdown"] = error_types

    if verbose:
        print("\n📊 Parsed result:")
        print(f"  ✓ Success: {result['success']}")
        print(f"  📝 Total tests: {result['summary']['total_tests']}")
        print(f"  ✅ Passed: {result['summary']['passed']}")
        print(f"  ❌ Failed: {result['summary']['failed']}")
        print(f"  ⏭  Ignored: {result['summary']['ignored']}")
        if result["summary"].get("test_time"):
            print(f"  ⏱  Time: {result['summary']['test_time']}")
        if error_types:
            print(f"  🐛 Error types: {error_types}")

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
    output_file = os.path.join(repo_path, "logs/run_cargo_test_results.json")

    if not os.path.exists(repo_path):
        print(f"❌ Repository path does not exist: {repo_path}", file=sys.stderr)
        sys.exit(1)

    # Check Cargo.toml
    cargo_toml = os.path.join(repo_path, "Cargo.toml")
    if not os.path.exists(cargo_toml):
        print(f"❌ Cargo.toml not found: {cargo_toml}", file=sys.stderr)
        sys.exit(1)

    print("🦀 Starting Cargo tests...")

    results, returncode = run_cargo_test(repo_path, timeout=900, verbose=True)

    # Save results
    save_results(results, output_file)
    print(f"\n💾 Results saved to: {output_file}")

    # Print summary
    print("\n" + "=" * 60)
    print("📊 Test summary")
    print("=" * 60)
    print(f"Status: {'✅ Success' if results['success'] else '❌ Failure'}")
    print(f"Total tests: {results['summary']['total_tests']}")
    print(f"Passed: {results['summary']['passed']}")
    print(f"Failed: {results['summary']['failed']}")
    print(f"Ignored: {results['summary']['ignored']}")
    if results["summary"].get("test_time"):
        print(f"Test time: {results['summary']['test_time']}")
    print("=" * 60)

    # Exit code: 0 all passed, 1 some failed
    sys.exit(0 if results["success"] else 1)


if __name__ == "__main__":
    main()
