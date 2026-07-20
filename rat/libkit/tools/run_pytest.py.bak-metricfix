#!/usr/bin/env python3
"""
run_pytest.py - run pytest in a container and summarize results

Features:
1. Auto-discover and run pytest tests in the repo
2. Summarize total/passed/failed counts
3. Classify common error types (ModuleNotFoundError, ImportError, etc.)
4. Generate a detailed test report
"""

import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def discover_test_files(repo_path: str) -> List[str]:
    """
    Discover test files in the repository.

    Args:
        repo_path: Repository path.

    Returns:
        List of test file paths.
    """
    test_files = []
    repo_path_obj = Path(repo_path)

    # Recursively find test_*.py and *_test.py
    for root, dirs, files in os.walk(repo_path):
        # Skip common non-test directories
        dirs[:] = [
            d
            for d in dirs
            if d
            not in {
                ".git",
                ".tox",
                ".pytest_cache",
                "__pycache__",
                "node_modules",
                ".venv",
                "venv",
                "env",
                ".eggs",
                "build",
                "dist",
                ".mypy_cache",
            }
        ]

        for file in files:
            if (
                file.startswith("test_") or file.endswith("_test.py")
            ) and file.endswith(".py"):
                test_files.append(os.path.join(root, file))

    return test_files


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
            timeout=10,
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


def categorize_error(error_text: str) -> str:
    """
    Identify an error type from error text.

    Args:
        error_text: Error message text.

    Returns:
        Error type string.
    """
    # Common Python exception types
    error_patterns = [
        (r"ModuleNotFoundError:", "ModuleNotFoundError"),
        (r"ImportError:", "ImportError"),
        (r"AttributeError:", "AttributeError"),
        (r"AssertionError", "AssertionError"),
        (r"TypeError:", "TypeError"),
        (r"ValueError:", "ValueError"),
        (r"KeyError:", "KeyError"),
        (r"IndexError:", "IndexError"),
        (r"NameError:", "NameError"),
        (r"FileNotFoundError:", "FileNotFoundError"),
        (r"RuntimeError:", "RuntimeError"),
        (r"OSError:", "OSError"),
        (r"IOError:", "IOError"),
        (r"ZeroDivisionError:", "ZeroDivisionError"),
        (r"SyntaxError:", "SyntaxError"),
        (r"IndentationError:", "IndentationError"),
        (r"MemoryError:", "MemoryError"),
        (r"RecursionError:", "RecursionError"),
        (r"TimeoutError:", "TimeoutError"),
        (r"ConnectionError:", "ConnectionError"),
        (r"PermissionError:", "PermissionError"),
    ]

    for pattern, error_type in error_patterns:
        if re.search(pattern, error_text):
            return error_type

    # If nothing matches, return a generic error
    return "OtherError"


def parse_junit_xml(xml_path: str, verbose: bool = False) -> Dict:
    """
    Parse pytest results from a JUnit XML file.

    This is more robust than regex parsing:
    1. Structured data from the XML DOM (not confused by stack traces)
    2. Higher accuracy (avoids matching redundant error text)
    3. Easier to maintain (JUnit XML is a stable format)

    Args:
        xml_path: Path to the JUnit XML report.
        verbose: Whether to include detailed information.

    Returns:
        A dict of statistics (compatible with parse_pytest_output).
    """
    result = {
        "summary": {
            "total_tests": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "errors": 0,
            "xfailed": 0,
            "xpassed": 0,
        },
        "error_breakdown": defaultdict(int),
        "failed_tests": [],
        "error_tests": [],
        "raw_output": "",
    }

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        # Parse testsuite/testsuites tags
        testsuites = (
            root.findall(".//testsuite") if root.tag == "testsuites" else [root]
        )

        for testsuite in testsuites:
            # Summary from testsuite attributes
            result["summary"]["total_tests"] += int(testsuite.get("tests", 0))
            result["summary"]["failed"] += int(testsuite.get("failures", 0))
            result["summary"]["errors"] += int(testsuite.get("errors", 0))
            result["summary"]["skipped"] += int(testsuite.get("skipped", 0))

            # Parse each testcase
            for testcase in testsuite.findall("testcase"):
                classname = testcase.get("classname", "")
                name = testcase.get("name", "")
                time = testcase.get("time", "0")

                # Build test_id (similar to pytest nodeid)
                test_id = f"{classname}::{name}" if classname else name

                # Determine outcome
                failure = testcase.find("failure")
                error = testcase.find("error")
                skipped = testcase.find("skipped")

                if failure is not None:
                    # Failed test
                    error_msg = failure.get("message", "")
                    error_text = failure.text or ""

                    # Extract error type from message/text
                    error_type = categorize_error(error_msg + "\n" + error_text)

                    result["failed_tests"].append(
                        {
                            "test_id": test_id,
                            "error_type": error_type,
                            "error_message": error_msg[:200],
                        }
                    )
                    result["error_breakdown"][error_type] += 1

                elif error is not None:
                    # Error (often setup/teardown)
                    error_msg = error.get("message", "")
                    error_text = error.text or ""

                    error_type = categorize_error(error_msg + "\n" + error_text)

                    result["error_tests"].append(
                        {
                            "test_id": test_id,
                            "error_type": error_type,
                            "error_message": error_msg[:200],
                        }
                    )
                    result["error_breakdown"][error_type] += 1

                elif skipped is not None:
                    # Skipped test
                    pass  # Already counted in summary
                else:
                    # Passed test
                    result["summary"]["passed"] += 1

        # Convert defaultdict to dict
        result["error_breakdown"] = dict(result["error_breakdown"])

        return result

    except FileNotFoundError:
        raise FileNotFoundError(f"JUnit XML file not found: {xml_path}")
    except ET.ParseError as e:
        raise ValueError(f"Failed to parse JUnit XML: {e}")
    except Exception as e:
        raise RuntimeError(f"Error parsing JUnit XML: {e}")


def parse_pytest_output(output: str, verbose: bool = False) -> Dict:
    """
    Parse pytest output and extract statistics.

    Args:
        output: pytest output text.
        verbose: Whether to include detailed info.

    Returns:
        A dict of statistics.
    """
    result = {
        "summary": {
            "total_tests": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "errors": 0,
            "xfailed": 0,
            "xpassed": 0,
        },
        "error_breakdown": defaultdict(int),
        "failed_tests": [],
        "error_tests": [],
        "raw_output": output if verbose else "",
    }

    # Parse the summary line
    # e.g. "5 passed, 2 failed, 1 skipped in 2.34s"
    # or "===== 5 passed, 2 failed in 2.34s ====="
    summary_patterns = [
        r"(\d+)\s+passed",
        r"(\d+)\s+failed",
        r"(\d+)\s+error",
        r"(\d+)\s+skipped",
        r"(\d+)\s+xfailed",
        r"(\d+)\s+xpassed",
    ]

    summary_keys = ["passed", "failed", "errors", "skipped", "xfailed", "xpassed"]

    for pattern, key in zip(summary_patterns, summary_keys):
        match = re.search(pattern, output)
        if match:
            result["summary"][key] = int(match.group(1))

    # Total tests
    result["summary"]["total_tests"] = sum(
        [
            result["summary"]["passed"],
            result["summary"]["failed"],
            result["summary"]["errors"],
            result["summary"]["skipped"],
            result["summary"]["xfailed"],
            result["summary"]["xpassed"],
        ]
    )

    # Failed test details (extract from FAILURES section)
    failures_section = re.search(
        r"={3,}\s*FAILURES\s*={3,}(.*?)(?:={3,}\s*short test summary|={3,}\s*\d+\s+failed|$)",
        output,
        re.DOTALL,
    )

    test_failures = {}
    if failures_section:
        failures_text = failures_section.group(1)
        # Each failure block: ______ test_name ______
        test_sections = re.split(r"_{10,}\s+([\w/\._:-]+)\s+_{10,}", failures_text)

        for i in range(1, len(test_sections), 2):
            if i + 1 < len(test_sections):
                test_name = test_sections[i].strip()
                test_error = test_sections[i + 1].strip()
                test_failures[test_name] = test_error

    # FAILED tests/test_example.py::test_function - AssertionError: ...
    failed_pattern = r"FAILED\s+([\w/\._:-]+)\s*-?\s*(.*?)(?=\n|$)"
    for match in re.finditer(failed_pattern, output, re.MULTILINE):
        test_id = match.group(1)
        error_msg = match.group(2).strip() if match.group(2) else ""

        # Try extracting detailed error from failures section
        detailed_error = ""
        test_name = test_id.split("::")[-1] if "::" in test_id else test_id
        if test_name in test_failures:
            detailed_error = test_failures[test_name]

        error_type = categorize_error(error_msg + "\n" + detailed_error)

        result["failed_tests"].append(
            {
                "test_id": test_id,
                "error_type": error_type,
                "error_message": error_msg[:200],  # Limit length
            }
        )

        result["error_breakdown"][error_type] += 1

    # ERROR tests (often collection/setup errors)
    # ERROR tests/test_example.py::test_function - ImportError: ...
    error_pattern = r"ERROR\s+([\w/\._:-]+)\s*-?\s*(.*?)(?=\n|$)"
    for match in re.finditer(error_pattern, output, re.MULTILINE):
        test_id = match.group(1)
        error_msg = match.group(2).strip() if match.group(2) else ""

        # Extract detailed error
        detailed_error = ""
        test_section = re.search(
            rf"{re.escape(test_id)}.*?(?=FAILED|PASSED|ERROR|={(3,)}|$)",
            output,
            re.DOTALL,
        )
        if test_section:
            detailed_error = test_section.group(0)

        error_type = categorize_error(error_msg + "\n" + detailed_error)

        result["error_tests"].append(
            {
                "test_id": test_id,
                "error_type": error_type,
                "error_message": error_msg[:200],
            }
        )

        result["error_breakdown"][error_type] += 1

    # Convert defaultdict to dict
    result["error_breakdown"] = dict(result["error_breakdown"])

    return result


def run_pytest(
    repo_path: Optional[str] = None,
    test_path: Optional[str] = None,
    max_tests: int = 0,
    timeout: int = 600,
    verbose: bool = False,
    extra_args: Optional[List[str]] = None,
) -> Tuple[Dict, int]:
    """
    Run pytest and collect results.

    Generates both:
    1. JUnit XML report (accurate parsing)
    2. Text output (logs + fallback)

    Args:
        repo_path: Repo path; if None uses current working directory.
        test_path: Test path (file/dir); if None runs all tests.
        max_tests: Max tests to run; 0 means unlimited.
        timeout: Timeout in seconds.
        verbose: Whether to include detailed output.
        extra_args: Extra pytest args.
    Returns:
        (result_dict, return_code)
    """
    # Default to current working directory
    if repo_path is None:
        repo_path = os.getcwd()

    # Build pytest command
    cmd = ["python", "-m", "pytest"]

    # JUnit XML output path
    junit_xml_path = os.path.join(repo_path, "logs/junit_report.xml")

    # Add args
    cmd.extend(
        [
            "-v",  # Verbose
            "--tb=short",  # Short traceback
            "--continue-on-collection-errors",  # Continue on collection errors
            f"--junit-xml={junit_xml_path}",  # Generate JUnit XML
        ]
    )

    if max_tests > 0:
        cmd.extend(["--maxfail", str(max_tests)])  # Stop after N failures

    if extra_args:
        cmd.extend(extra_args)

    # Test path
    if test_path:
        cmd.append(test_path)
    else:
        cmd.append(repo_path)

    if verbose:
        print(f"🔧 Command: {' '.join(cmd)}")
        print(f"📁 Working directory: {repo_path}")
        print(f"📄 JUnit XML: {junit_xml_path}")
        print(f"⏱️  Timeout: {timeout}s")
        print("=" * 60)

    try:
        # Run pytest
        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True, timeout=timeout
        )

        output = result.stdout + "\n" + result.stderr

        if verbose:
            print("\n📋 Pytest output:")
            print(output)
            print("=" * 60)

        # Prefer XML parsing
        try:
            parsed_result = parse_junit_xml(junit_xml_path, verbose)
            parsed_result["returncode"] = result.returncode
            parsed_result["raw_output"] = output if verbose else ""
            parsed_result["parse_method"] = "junit_xml"  # Parse method marker

            if verbose:
                print("✅ Parsed successfully via JUnit XML")

        except Exception as xml_error:
            # XML parse failed; fall back to regex
            if verbose:
                print(f"⚠️  Failed to parse JUnit XML: {xml_error}")
                print("📝 Falling back to text output parsing...")

            parsed_result = parse_pytest_output(output, verbose)
            parsed_result["returncode"] = result.returncode
            parsed_result["parse_method"] = "regex_fallback"

        return parsed_result, result.returncode

    except subprocess.TimeoutExpired:
        error_result = {
            "summary": {
                "total_tests": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "errors": 0,
            },
            "error_breakdown": {"TimeoutError": 1},
            "failed_tests": [],
            "error_tests": [
                {
                    "test_id": "N/A",
                    "error_type": "TimeoutError",
                    "error_message": f"Pytest timed out ({timeout}s)",
                }
            ],
            "returncode": -1,
        }
        return error_result, -1

    except Exception as e:
        error_result = {
            "summary": {
                "total_tests": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "errors": 0,
            },
            "error_breakdown": {"ExecutionError": 1},
            "failed_tests": [],
            "error_tests": [
                {
                    "test_id": "N/A",
                    "error_type": "ExecutionError",
                    "error_message": str(e),
                }
            ],
            "returncode": -1,
        }
        return error_result, -1


def format_output(result: Dict, verbose: bool = False) -> str:
    """
    Format output.

    Args:
        result: Parsed result dict.
        verbose: Whether to include details.

    Returns:
        Formatted output string.
    """
    output = []
    output.append("\n" + "=" * 60)
    output.append("📊 Pytest test summary")
    output.append("=" * 60)

    summary = result["summary"]
    output.append(f"\nTotal tests: {summary['total_tests']}")
    output.append(f"✅ Passed: {summary['passed']}")
    output.append(f"❌ Failed: {summary['failed']}")
    output.append(f"⚠️  Errors: {summary['errors']}")
    output.append(f"⏭️  Skipped: {summary['skipped']}")

    if summary.get("xfailed", 0) > 0:
        output.append(f"🔶 Xfailed: {summary['xfailed']}")
    if summary.get("xpassed", 0) > 0:
        output.append(f"🔷 Xpassed: {summary['xpassed']}")

    # Error breakdown
    if result["error_breakdown"]:
        output.append("\n" + "-" * 60)
        output.append("📋 Error breakdown:")
        output.append("-" * 60)

        # Sort by count
        sorted_errors = sorted(
            result["error_breakdown"].items(), key=lambda x: x[1], reverse=True
        )

        for error_type, count in sorted_errors:
            output.append(f"  • {error_type}: {count}")

    # Failed test details
    if verbose and (result["failed_tests"] or result["error_tests"]):
        output.append("\n" + "-" * 60)
        output.append("❌ Failed test details:")
        output.append("-" * 60)

        for test in result["failed_tests"]:
            output.append(f"\n🔴 {test['test_id']}")
            output.append(f"   Type: {test['error_type']}")
            output.append(f"   Message: {test['error_message']}")

        for test in result["error_tests"]:
            output.append(f"\n🔴 {test['test_id']}")
            output.append(f"   Type: {test['error_type']}")
            output.append(f"   Message: {test['error_message']}")

    output.append("\n" + "=" * 60)

    return "\n".join(output)


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
    """Main entrypoint - auto-detect working directory."""
    # Auto-detect current working directory
    repo_path = os.getcwd()
    timeout = 180
    verbose = True  # Always verbose

    # Output path (based on working directory)
    output_file = os.path.join(repo_path, "logs/run_pytest_results.json")

    print("=" * 60)
    print("🧪 Run Pytest - automatic test runner")
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

    # Discover test files
    test_files = discover_test_files(repo_path)
    print(f"\n📁 Found {len(test_files)} test files under {repo_path}")

    if test_files:
        print("\nTest file list:")
        for i, file in enumerate(test_files[:10], 1):  # Show first 10
            rel_path = os.path.relpath(file, repo_path)
            print(f"  {i}. {rel_path}")
        if len(test_files) > 10:
            print(f"  ... and {len(test_files) - 10} more")

    if not test_files:
        print(
            "\n⚠️  Warning: no test files found; running pytest anyway (may collect other tests)"
        )

    # Run tests
    print("\n🚀 Running tests...")
    result, returncode = run_pytest(
        repo_path=repo_path,
        test_path=None,  # Run all tests
        max_tests=0,  # No limit
        timeout=timeout,
        verbose=verbose,
    )

    # Print output
    print(format_output(result, verbose))

    # Save results
    save_results(result, output_file)

    # Return codes: -1=timeout/exec error, 0=all passed, 1=some failed, 5=no tests collected
    # Check exec errors first to avoid misreporting as "no tests collected"
    if result.get("returncode") == -1:
        print("\n❌ Test execution error (timeout or other failure)")
        return -1
    elif result["summary"]["total_tests"] == 0:
        print("\n⚠️  No tests were collected")
        return 5
    elif result["summary"]["failed"] > 0 or result["summary"]["errors"] > 0:
        print(
            f"\n❌ {result['summary']['failed'] + result['summary']['errors']} tests failed/errored"
        )
        return 1
    else:
        print("\n✅ All tests passed")
        return 0


if __name__ == "__main__":
    sys.exit(main())
