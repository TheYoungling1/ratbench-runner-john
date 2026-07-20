#!/usr/bin/env python3
"""Scorer functions for evaluating repository processing results."""

import json
import os
from typing import Dict, Any, List

import weave


def get_scorers_for_language(language: str) -> List:
    """
    Return the scorers for the given language.

    Args:
        language: Language name

    Returns:
        List of scorer functions
    """
    scorers_map = {
        "python": [success_scorer, pytest_pass_rate_scorer, pytest_collect_scorer],
        "java": [success_scorer, java_build_scorer],
        "nodejs": [success_scorer, npm_install_scorer, npm_test_pass_rate_scorer],
        "rust": [success_scorer, cargo_build_scorer, cargo_test_pass_rate_scorer],
    }

    # Return the scorers for the language; fall back to the base scorer if missing.
    return scorers_map.get(language.lower(), [success_scorer])


@weave.op
def success_scorer(output: dict) -> dict:
    """
    Scorer: whether the repository processing succeeded.

    Args:
        repo: Input repository info {"repo": {...}}
        output: Result returned by predict() {"status": "success|error|timeout"}

    Returns:
        {"success": True/False}
    """
    return {"success": output.get("status") == "success"}


@weave.op
def pytest_pass_rate_scorer(output: dict) -> dict:
    """
    Scorer: compute pytest pass rate.

    Args:
        repo: Input repository info {"repo": {...}}
        output: Result returned by predict(), including root_path and full_name

    Returns:
        {
            "pytest_pass_rate": Pass rate (0.0-1.0); 0.0 if not executed,
            "pytest_total_tests": Total test count,
            "pytest_passed": Passed test count,
            "pytest_failed": Failed test count,
            "pytest_errors": Error count,
            "pytest_executed": Whether pytest was executed,
            "error_breakdown": Dict of error type counts,
            "pass_rate_exclude_code_issues": Pass rate excluding code issues
        }
    """
    # Default return value
    default_result = {
        "pytest_pass_rate": 0.0,
        "pytest_total_tests": 0,
        "pytest_passed": 0,
        "pytest_failed": 0,
        "pytest_errors": 0,
        "pytest_executed": False,
        "error_breakdown": {},
        "pass_rate_exclude_code_issues": 0.0,
    }

    # Get root_path and full_name
    root_path = output.get("root_path")
    full_name = output.get("full_name")

    if not root_path or not full_name:
        return default_result

    # Build pytest results file path
    results_path = f"{root_path}/output/{full_name}/run_pytest_results.json"

    # Check if file exists
    if not os.path.exists(results_path):
        return default_result

    try:
        # Read pytest results
        with open(results_path, "r", encoding="utf-8") as f:
            results = json.load(f)

        summary = results.get("summary", {})
        error_breakdown = results.get("error_breakdown", {})

        total_tests = summary.get("total_tests", 0)
        passed = summary.get("passed", 0)
        failed = summary.get("failed", 0)
        errors = summary.get("errors", 0)
        skipped = summary.get("skipped", 0)

        # Compute pass rate: passed / (total_tests - skipped)
        effective_total = total_tests - skipped
        if effective_total > 0:
            pass_rate = passed / effective_total
        elif (
            effective_total == 0
            and total_tests == 0
            and len(error_breakdown) == 1
            and "TimeoutError" in error_breakdown
        ):
            # All tests timed out; likely tests run forever or take a very long time.
            # Treat "no error within timeout" as pass.
            pass_rate = 1.0
        else:
            pass_rate = 0.0

        # Compute pass rate excluding code issues.
        # Only exclude ModuleNotFoundError and ImportError (dependency/environment issues).
        module_not_found_count = error_breakdown.get("ModuleNotFoundError", 0)
        import_error_count = error_breakdown.get("ImportError", 0)
        code_issue_count = module_not_found_count + import_error_count

        effective_tests_excluding_code_issues = passed + code_issue_count
        if effective_tests_excluding_code_issues > 0:
            pass_rate_exclude_code_issues = (
                passed / effective_tests_excluding_code_issues
            )
        elif (
            effective_tests_excluding_code_issues == 0
            and total_tests == 0
            and len(error_breakdown) == 1
            and "TimeoutError" in error_breakdown
        ):
            pass_rate_exclude_code_issues = 1.0
        else:
            pass_rate_exclude_code_issues = 0.0

        return {
            "pytest_pass_rate": round(pass_rate, 4),
            "pytest_total_tests": total_tests,
            "pytest_passed": passed,
            "pytest_failed": failed,
            "pytest_errors": errors,
            "pytest_executed": True,
            "error_breakdown": error_breakdown,
            "pass_rate_exclude_code_issues": round(pass_rate_exclude_code_issues, 4),
        }

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"⚠️  Failed to parse pytest results ({full_name}): {e}")
        return default_result


@weave.op
def pytest_collect_scorer(output: dict) -> dict:
    """
    Scorer: whether pytest collection succeeded.

    Args:
        repo: Input repository info {"repo": {...}}
        output: Result returned by predict(), including root_path and full_name

    Returns:
        {
            "pytest_collect_success": Whether collection succeeded (True/False)
        }
    """
    # Default return value
    default_result = {"pytest_collect_success": False}

    # Get root_path and full_name
    root_path = output.get("root_path")
    full_name = output.get("full_name")

    if not root_path or not full_name:
        return default_result

    # Build pytest collection results file path
    results_path = f"{root_path}/output/{full_name}/run_pytest_collect_results.json"

    # Check if file exists
    if not os.path.exists(results_path):
        return default_result

    try:
        # Read pytest collection results
        with open(results_path, "r", encoding="utf-8") as f:
            results = json.load(f)

        # Extract success field
        success = results.get("success", False)

        return {"pytest_collect_success": success}

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"⚠️  Failed to parse pytest collect results ({full_name}): {e}")
        return default_result


@weave.op
def npm_install_scorer(output: dict) -> dict:
    """
    Node.js npm install scorer.

    Parse npm install results to evaluate whether dependency installation succeeded.

    Args:
        repo: Input repository info {"repo": {...}}
        output: Result returned by predict(), including root_path, full_name, language

    Returns:
        {
            "npm_install_success": Whether dependency installation succeeded (True/False),
            "npm_install_errors": Error count,
            "npm_install_warnings": Warning count,
        }
    """
    # Default return value
    default_result = {
        "npm_install_success": False,
        "npm_install_errors": 0,
        "npm_install_warnings": 0,
    }

    # Get required information
    root_path = output.get("root_path")
    full_name = output.get("full_name")

    if not root_path or not full_name:
        return default_result

    # Find npm install results file
    results_path = f"{root_path}/output/{full_name}/run_npm_install_results.json"

    if not os.path.exists(results_path):
        return default_result

    try:
        # Read npm install results
        with open(results_path, "r", encoding="utf-8") as f:
            results = json.load(f)

        # Parse install results
        success = results.get("success", False)
        errors = results.get("errors", 0)
        warnings = results.get("warnings", 0)

        return {
            "npm_install_success": success,
            "npm_install_errors": errors,
            "npm_install_warnings": warnings,
        }

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"⚠️  Failed to parse npm install results ({full_name}): {e}")
        return default_result


@weave.op
def npm_test_pass_rate_scorer(output: dict) -> dict:
    """
    Node.js npm test pass rate scorer.

    Parse npm test results to evaluate test pass rate.

    Args:
        output: Result returned by predict(), including root_path, full_name, language

    Returns:
        {
            "npm_test_pass_rate": Test pass rate (0.0-1.0),
            "npm_test_total": Total test count,
            "npm_test_passed": Passed test count,
            "npm_test_failed": Failed test count,
        }
    """
    # Default return value
    default_result = {
        "npm_test_pass_rate": 0.0,
        "npm_test_total": 0,
        "npm_test_passed": 0,
        "npm_test_failed": 0,
    }

    # Get required information
    root_path = output.get("root_path")
    full_name = output.get("full_name")

    if not root_path or not full_name:
        return default_result

    # Find npm test results file
    results_path = f"{root_path}/output/{full_name}/run_npm_test_results.json"

    if not os.path.exists(results_path):
        return default_result

    try:
        # Read npm test results
        with open(results_path, "r", encoding="utf-8") as f:
            results = json.load(f)

        success = results.get("success", False)
        summary = results.get("summary", {})

        total_tests = summary.get("total_tests", 0)
        passed = summary.get("passed", 0)
        failed = summary.get("failed", 0)

        # Compute pass rate
        if total_tests > 0:
            pass_rate = passed / total_tests
        elif success:
            pass_rate = 1.0
        else:
            pass_rate = 0.0

        return {
            "npm_test_pass_rate": round(pass_rate, 4),
            "npm_test_total": total_tests,
            "npm_test_passed": passed,
            "npm_test_failed": failed,
        }

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"⚠️  Failed to parse npm test results ({full_name}): {e}")
        return default_result


@weave.op
def cargo_build_scorer(output: dict) -> dict:
    """
    Rust cargo build scorer.

    Parse cargo build results to evaluate whether the build succeeded.

    Args:
        output: Result returned by predict(), including root_path, full_name, language

    Returns:
        {
            "cargo_build_success": Whether the build succeeded (True/False),
            "cargo_build_pass_rate": Build pass rate (1.0=success, 0.0=failure),
            "cargo_build_time": Build duration,
            "cargo_build_status": Build status (SUCCESS/FAILURE/ERROR),
            "cargo_build_error_message": Error message (if failed)
        }
    """
    # Default return value
    default_result = {
        "cargo_build_success": False,
        "cargo_build_pass_rate": 0.0,
        "cargo_build_time": "N/A",
        "cargo_build_status": "NOT_EXECUTED",
        "cargo_build_error_message": "",
    }

    # Get required information
    root_path = output.get("root_path")
    full_name = output.get("full_name")

    if not root_path or not full_name:
        return default_result

    # Find cargo build results file
    results_path = f"{root_path}/output/{full_name}/run_cargo_build_results.json"

    if not os.path.exists(results_path):
        return default_result

    try:
        # Read cargo build results
        with open(results_path, "r", encoding="utf-8") as f:
            results = json.load(f)

        # Parse build results
        success = results.get("success", False)
        summary = results.get("summary", {})

        return {
            "cargo_build_success": success,
            "cargo_build_pass_rate": 1.0 if success else 0.0,
            "cargo_build_time": summary.get("build_time", "N/A"),
            "cargo_build_status": summary.get("status", "UNKNOWN"),
            "cargo_build_error_message": results.get("error_message", ""),
        }

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"⚠️  Failed to parse cargo build results ({full_name}): {e}")
        return default_result


@weave.op
def cargo_test_pass_rate_scorer(output: dict) -> dict:
    """
    Rust cargo test pass rate scorer.

    Parse cargo test results to evaluate test pass rate.

    Args:
        output: Result returned by predict(), including root_path, full_name, language

    Returns:
        {
            "cargo_test_pass_rate": Test pass rate (0.0-1.0),
            "cargo_test_total": Total test count,
            "cargo_test_passed": Passed test count,
            "cargo_test_failed": Failed test count,
        }
    """
    # Default return value
    default_result = {
        "cargo_test_pass_rate": 0.0,
        "cargo_test_total": 0,
        "cargo_test_passed": 0,
        "cargo_test_failed": 0,
    }

    # Get required information
    root_path = output.get("root_path")
    full_name = output.get("full_name")

    if not root_path or not full_name:
        return default_result

    # Find cargo test results file
    results_path = f"{root_path}/output/{full_name}/run_cargo_test_results.json"

    if not os.path.exists(results_path):
        return default_result

    try:
        # Read cargo test results
        with open(results_path, "r", encoding="utf-8") as f:
            results = json.load(f)

        success = results.get("success", False)
        summary = results.get("summary", {})

        total_tests = summary.get("total_tests", 0)
        passed = summary.get("passed", 0)
        failed = summary.get("failed", 0)

        # Compute pass rate
        if total_tests > 0:
            pass_rate = passed / total_tests
        elif success:
            pass_rate = 1.0
        else:
            pass_rate = 0.0

        return {
            "cargo_test_pass_rate": round(pass_rate, 4),
            "cargo_test_total": total_tests,
            "cargo_test_passed": passed,
            "cargo_test_failed": failed,
        }

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"⚠️  Failed to parse cargo test results ({full_name}): {e}")
        return default_result


@weave.op
def java_build_scorer(output: dict) -> dict:
    """
    Java Maven/Gradle build scorer.

    Parse Maven or Gradle build results to evaluate whether the build succeeded.

    Args:
        output: Result returned by predict(), including root_path, full_name, language

    Returns:
        {
            "build_success": Whether the build succeeded (True/False),
            "build_pass_rate": Build pass rate (1.0=success, 0.0=failure),
            "build_time": Build duration,
            "build_status": Build status (SUCCESS/FAILURE/TIMEOUT/ERROR),
            "build_type": Build tool type (Maven/Gradle),
            "build_error_message": Error message (if failed)
        }
    """
    # Default return value
    default_result = {
        "build_success": False,
        "build_pass_rate": 0.0,
        "build_time": "N/A",
        "build_status": "NOT_EXECUTED",
        "build_type": "unknown",
        "build_error_message": "",
    }

    # Get required information
    root_path = output.get("root_path")
    full_name = output.get("full_name")

    if not root_path or not full_name:
        return default_result

    # Try to find Maven or Gradle build results
    build_result_files = [
        ("run_maven_install_results.json", "Maven"),
        ("run_gradle_build_results.json", "Gradle"),
    ]

    for filename, build_type in build_result_files:
        results_path = f"{root_path}/output/{full_name}/{filename}"

        if not os.path.exists(results_path):
            continue

        try:
            # Read build results
            with open(results_path, "r", encoding="utf-8") as f:
                results = json.load(f)

            # Parse build results
            success = results.get("success", False)
            summary = results.get("summary", {})

            return {
                "build_success": success,
                "build_pass_rate": 1.0 if success else 0.0,
                "build_time": summary.get("build_time", "N/A"),
                "build_status": summary.get("status", "UNKNOWN"),
                "build_type": build_type,
                "build_error_message": results.get("error_message", ""),
            }

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"⚠️  Failed to parse {build_type} build results ({full_name}): {e}")
            continue

    # No build results file was found
    return default_result


@weave.op
def stage_timing_scorer(output: dict) -> dict:
    """Scorer: record per-stage timings."""
    stage_timings = output.get("stage_timings", {})
    result = {}

    # Expand stage timings into individual metrics
    for stage_name, stage_time in stage_timings.items():
        # Normalize stage name to make it a valid metric name
        metric_name = stage_name.replace(":", "_").replace(" ", "_")
        result[f"timing_{metric_name}"] = stage_time

    # Add total time
    result["timing_total"] = output.get("execution_time", 0)

    return result


@weave.op
def tool_usage_scorer(output: dict) -> dict:
    """Scorer: record tool call statistics."""
    tool_stats = output.get("tool_stats", {})
    result = {}

    # Expand tool stats into individual metrics
    for tool_name, count in tool_stats.items():
        # Normalize tool name to make it a valid metric name
        metric_name = tool_name.replace("-", "_").replace(" ", "_")
        result[f"tool_{metric_name}"] = count

    # Add total tool call count
    result["tool_total_calls"] = sum(tool_stats.values())

    return result
