#!/usr/bin/env python3
"""Shared utility functions for evaluation scripts."""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


class TimeoutException(Exception):
    """Timeout exception."""

    pass


def load_repos_as_dataset(
    json_path: str, limit: Optional[int] = None, offset: int = 0
) -> List[Dict[str, Any]]:
    """
    Load a repo list from a JSON file and convert it to Weave dataset format.

    Args:
        json_path: Path to the JSON file.
        limit: Maximum number of repos to process.
        offset: Skip the first N repos.

    Returns:
        A list in Weave dataset format: [{"repo": {...}}, ...]
    """
    with open(json_path, "r", encoding="utf-8") as f:
        all_repos = json.load(f)

    # Apply offset/limit
    start = offset
    end = offset + limit if limit else len(all_repos)

    # Convert to Weave dataset format
    return [{"repo": repo} for repo in all_repos[start:end]]


def publish_if_changed(dataset_name: str, dataset: Any) -> None:
    """
    Publish the Weave dataset only when content changes.

    Args:
        dataset_name: Dataset name.
        rows: Dataset rows.
    """
    import weave

    ref_uri = f"{dataset_name}:latest"

    try:
        # Get a reference to the remote object
        latest_ref = weave.ref(ref_uri).get()

        # Compare local and remote digests
        if latest_ref.digest == dataset.digest:
            print(f"No changes detected (Digest: {dataset.digest}); skipping publish.")
            return latest_ref
        else:
            print("Changes detected; publishing a new version...")
    except Exception:
        print("No previous version found; publishing the first version...")

    # Publish
    weave.publish(dataset, dataset_name)


def extract_pytest_result(output_dir: str) -> Optional[Dict[str, Any]]:
    """
    Extract pytest results from output/{full_name}/run_pytest_results.json.

    Args:
        output_dir: Output directory path, e.g. "output/271374667/VideoFusion".

    Returns:
        A result dict; returns None if the file does not exist.
    """
    result_path = Path(output_dir) / "run_pytest_results.json"

    if not result_path.exists():
        return None

    try:
        with open(result_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        summary = data.get("summary", {})
        total = summary.get("total_tests", 0)
        passed = summary.get("passed", 0)

        return {
            "total_tests": total,
            "passed": passed,
            "failed": summary.get("failed", 0),
            "skipped": summary.get("skipped", 0),
            "errors": summary.get("errors", 0),
            "xfailed": summary.get("xfailed", 0),
            "xpassed": summary.get("xpassed", 0),
            "pass_rate": passed / total if total > 0 else None,
            "returncode": data.get("returncode", -1),
        }
    except Exception as e:
        print(f"⚠️  Failed to parse pytest result ({result_path}): {e}")
        return None


def calculate_statistics(all_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compute summary statistics.

    Args:
        all_results: Per-repo results.

    Returns:
        Statistics dict.
    """
    total = len(all_results)
    successful = sum(1 for r in all_results if r["status"] == "success")
    failed = sum(1 for r in all_results if r["status"] in ["error", "timeout"])

    # Success rate
    success_rate = successful / total if total > 0 else 0

    # Execution time stats
    execution_times = [r["execution_time"] for r in all_results]
    avg_execution_time = (
        sum(execution_times) / len(execution_times) if execution_times else 0
    )
    median_execution_time = (
        sorted(execution_times)[len(execution_times) // 2] if execution_times else 0
    )

    # Test stats
    repos_with_tests = 0
    total_tests = 0
    total_passed = 0
    total_failed = 0
    total_skipped = 0
    total_errors = 0
    pass_rates = []

    for r in all_results:
        if r["status"] == "success" and r.get("pytest_result"):
            pytest = r["pytest_result"]
            if pytest["total_tests"] > 0:
                repos_with_tests += 1
                total_tests += pytest["total_tests"]
                total_passed += pytest["passed"]
                total_failed += pytest["failed"]
                total_skipped += pytest["skipped"]
                total_errors += pytest["errors"]
                if pytest["pass_rate"] is not None:
                    pass_rates.append(pytest["pass_rate"])

    repos_without_tests = successful - repos_with_tests
    avg_tests_per_repo = total_tests / repos_with_tests if repos_with_tests > 0 else 0
    avg_pass_rate = sum(pass_rates) / len(pass_rates) if pass_rates else 0

    # Error type breakdown
    error_breakdown = {}
    for r in all_results:
        if r["status"] in ["error", "timeout"]:
            error_type = r.get("error_type", "Unknown")
            error_breakdown[error_type] = error_breakdown.get(error_type, 0) + 1

    return {
        "success_rate": round(success_rate, 4),
        "avg_execution_time": round(avg_execution_time, 2),
        "median_execution_time": round(median_execution_time, 2),
        "repos_with_tests": repos_with_tests,
        "repos_without_tests": repos_without_tests,
        "avg_tests_per_repo": round(avg_tests_per_repo, 2),
        "avg_pass_rate": round(avg_pass_rate, 4),
        "total_tests": total_tests,
        "total_passed": total_passed,
        "total_failed": total_failed,
        "total_skipped": total_skipped,
        "total_errors": total_errors,
        "error_breakdown": error_breakdown,
    }


def format_time_duration(seconds: float) -> str:
    """
    Format a duration in seconds.

    Args:
        seconds: Number of seconds.

    Returns:
        Formatted duration string, e.g. "2h 15m 30s".
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")

    return " ".join(parts)


def generate_summary_report(
    all_results: List[Dict[str, Any]], metadata: Dict[str, Any], output_path: str
) -> None:
    """
    Generate and save a full summary report.

    Args:
        all_results: Per-repo results.
        metadata: Metadata.
        output_path: Output file path.
    """
    # Compute statistics
    statistics = calculate_statistics(all_results)

    # Build report
    report = {"metadata": metadata, "results": all_results, "statistics": statistics}

    # Ensure output dir exists
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    # Write to file
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n📊 Summary report saved: {output_path}")


def load_existing_results(
    output_path: str,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Load existing results (for incremental saving / resume).

    Args:
        output_path: Output file path.

    Returns:
        (results, metadata)
    """
    if not os.path.exists(output_path):
        return [], {}

    try:
        with open(output_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return data.get("results", []), data.get("metadata", {})
    except Exception as e:
        print(f"⚠️  Failed to load existing results: {e}")
        return [], {}


def save_incremental_result(
    result: Dict[str, Any], output_path: str, metadata: Dict[str, Any]
) -> None:
    """
    Incrementally save a single repo result.

    Args:
        result: Single repo result.
        output_path: Output file path.
        metadata: Metadata.
    """
    # Load existing
    existing_results, existing_metadata = load_existing_results(output_path)

    # Update metadata (preserve start_time)
    if existing_metadata.get("start_time"):
        metadata["start_time"] = existing_metadata["start_time"]

    # Append
    existing_results.append(result)

    # Update counters
    metadata["processed"] = len(existing_results)
    metadata["successful"] = sum(
        1 for r in existing_results if r["status"] == "success"
    )
    metadata["failed"] = sum(
        1 for r in existing_results if r["status"] in ["error", "timeout"]
    )

    # Save
    generate_summary_report(existing_results, metadata, output_path)


def print_final_summary(
    statistics: Dict[str, Any], total_repos: int, duration: float
) -> None:
    """
    Print a final statistics summary.

    Args:
        statistics: Statistics dict.
        total_repos: Total number of repos.
        duration: Total duration in seconds.
    """
    print("\n" + "=" * 60)
    print("📊 Evaluation completed: final stats")
    print("=" * 60)
    print(f"⏱️  Total time: {format_time_duration(duration)}")
    print(f"📦 Total repos: {total_repos}")
    print(
        f"✅ Success: {int(total_repos * statistics['success_rate'])} ({statistics['success_rate'] * 100:.1f}%)"
    )
    print(f"❌ Failed: {total_repos - int(total_repos * statistics['success_rate'])}")
    print(f"\n🧪 Test stats:")
    print(f"   Repos with tests: {statistics['repos_with_tests']}")
    print(f"   Repos without tests: {statistics['repos_without_tests']}")
    print(f"   Total tests: {statistics['total_tests']}")
    print(f"   Passed: {statistics['total_passed']}")
    print(f"   Failed: {statistics['total_failed']}")
    print(f"   Avg pass rate: {statistics['avg_pass_rate'] * 100:.1f}%")
    print(f"\n⏱️  Performance stats:")
    print(
        f"   Avg processing time: {format_time_duration(statistics['avg_execution_time'])}"
    )
    print(
        f"   Median processing time: {format_time_duration(statistics['median_execution_time'])}"
    )

    if statistics["error_breakdown"]:
        print(f"\n❌ Error breakdown:")
        for error_type, count in sorted(
            statistics["error_breakdown"].items(), key=lambda x: x[1], reverse=True
        ):
            print(f"   {error_type}: {count}")

    print("=" * 60)
