#!/usr/bin/env python3
"""RAT per-repo scorers, copied verbatim from rat/eval/common/scorers.py
(weave decorator stripped) so bench owns score math without a weave dep.
Keep in sync if the RAT scorers change.
"""

import json
import os


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
        "pytest_timeout_unverified": False,
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

        # CORRECTED VARIANT (RAT-bench fidelity fix B): detect the timeout-only,
        # zero-recorded-tests case. The paper-faithful baseline assigned this
        # pass_rate = 1.0 ("treat 'no error within timeout' as pass"), which rewards
        # suites too slow to finish and is the single largest source of optimism in
        # the headline ESSR. "No error within timeout" is NOT "all tests pass". Here
        # we score it 0.0 and surface a `pytest_timeout_unverified` flag so honest
        # scoreboards can report/exclude these separately. Applied to BOTH the
        # pytest_pass_rate and pass_rate_exclude_code_issues branches.
        timeout_only_no_tests = (
            total_tests == 0
            and len(error_breakdown) == 1
            and "TimeoutError" in error_breakdown
        )

        # Compute pass rate: passed / (total_tests - skipped)
        effective_total = total_tests - skipped
        if effective_total > 0:
            pass_rate = passed / effective_total
        elif timeout_only_no_tests:
            # All tests timed out with nothing recorded -> NOT a pass. Treat as 0.0
            # (unverified); reported separately via pytest_timeout_unverified.
            pass_rate = 0.0
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
        elif timeout_only_no_tests:
            # Same correction for the S2-style "verified" denominator branch:
            # a timeout with no recorded tests is unverified, not a perfect pass.
            pass_rate_exclude_code_issues = 0.0
        else:
            pass_rate_exclude_code_issues = 0.0

        return {
            "pytest_pass_rate": round(pass_rate, 4),
            "pytest_total_tests": total_tests,
            "pytest_passed": passed,
            "pytest_failed": failed,
            "pytest_errors": errors,
            "pytest_executed": True,
            "pytest_timeout_unverified": bool(timeout_only_no_tests),
            "error_breakdown": error_breakdown,
            "pass_rate_exclude_code_issues": round(pass_rate_exclude_code_issues, 4),
        }

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"⚠️  Failed to parse pytest results ({full_name}): {e}")
        return default_result


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
