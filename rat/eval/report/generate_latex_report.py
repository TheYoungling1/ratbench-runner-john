#!/usr/bin/env python3
"""
Generate a LaTeX evaluation report from a Weave CSV export.

This script reads a CSV exported from Weave, parses evaluation data, and generates a LaTeX report including:
- Summary stats (success rate, test pass rate, cost, token usage, etc.)
- Detailed per-repo results table
- Error type breakdown

Examples:
    # Generate from CSV (auto-compile PDF)
    python eval/generate_latex_report.py weave_export_rat-evaluation_2026-01-13.csv

    # Specify output directory
    python eval/generate_latex_report.py weave_export_rat-evaluation_2026-01-13.csv --output-dir output/reports

    # Generate LaTeX only (no PDF)
    python eval/generate_latex_report.py weave_export_rat-evaluation_2026-01-13.csv --no-compile

    # Also save parsed JSON (for debugging/other uses)
    python eval/generate_latex_report.py weave_export_rat-evaluation_2026-01-13.csv --save-json

Outputs:
    - <csv_basename>_report.tex: LaTeX source
    - <csv_basename>_report.pdf: compiled PDF (if enabled)
    - <csv_basename>_parsed.json: parsed JSON (if --save-json)
"""

import argparse
import csv
import json
import os
import sys
from typing import Any, Dict, List

from eval.report.latex_generator import LaTeXReportGenerator

# CSV column name patterns (for dynamic matching)
COLUMN_PATTERNS = {
    # These are the fixed key columns
    "pass_rate_exclude_code_issues": "output.scores.pytest_pass_rate_scorer.pass_rate_exclude_code_issues",
    "pytest_total_tests": "output.scores.pytest_pass_rate_scorer.pytest_total_tests",
    "pytest_pass_rate": "output.scores.pytest_pass_rate_scorer.pytest_pass_rate",
    "pytest_executed": "output.scores.pytest_pass_rate_scorer.pytest_executed",
    "pytest_passed": "output.scores.pytest_pass_rate_scorer.pytest_passed",
    "pytest_failed": "output.scores.pytest_pass_rate_scorer.pytest_failed",
    "pytest_errors": "output.scores.pytest_pass_rate_scorer.pytest_errors",
    "completion_tokens_total_cost": "summary.weave.costs.deepseek-chat.completion_tokens_total_cost",
    "prompt_tokens_total_cost": "summary.weave.costs.deepseek-chat.prompt_tokens_total_cost",
    "total_tokens": "summary.usage.deepseek-chat.total_tokens",
    "completion_tokens": "summary.usage.deepseek-chat.completion_tokens",
    "full_name": "inputs.example.repo.full_name",
    "success": "output.scores.success_scorer.success",
    "output_status": "output.output.status",
    "model_latency": "output.model_latency",
    "started_at": "started_at",
    "ended_at": "ended_at",
}

# Prefix for error breakdown columns
ERROR_BREAKDOWN_PREFIX = "output.scores.pytest_pass_rate_scorer.error_breakdown."


def safe_float(value: str, default: float = 0.0) -> float:
    """Safely convert a string to float."""
    if not value or value.strip() == "":
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def safe_int(value: str, default: int = 0) -> int:
    """Safely convert a string to int."""
    if not value or value.strip() == "":
        return default
    try:
        return int(float(value))  # float -> int to handle strings like "1.0"
    except (ValueError, TypeError):
        return default


def safe_bool(value: str, default: bool = False) -> bool:
    """Safely convert a string to bool."""
    if not value or value.strip() == "":
        return default
    value_lower = value.strip().lower()
    if value_lower in ("true", "1", "yes"):
        return True
    elif value_lower in ("false", "0", "no"):
        return False
    return default


def build_column_mapping(header: List[str]) -> Dict[str, Any]:
    """
    Build a column index mapping from the CSV header.

    Args:
        header: CSV header row.

    Returns:
        A mapping from field name to column index; error_columns is a list of (error_type, col_idx).
    """
    column_map: Dict[str, Any] = {}

    # Match predefined key columns
    for key, pattern in COLUMN_PATTERNS.items():
        try:
            column_map[key] = header.index(pattern)
        except ValueError:
            # Some columns may not exist
            pass

    # Dynamically identify all error_breakdown columns
    error_columns: List[tuple] = []
    for idx, col_name in enumerate(header):
        if col_name.startswith(ERROR_BREAKDOWN_PREFIX):
            error_type = col_name[len(ERROR_BREAKDOWN_PREFIX) :]
            error_columns.append((error_type, idx))

    column_map["error_columns"] = error_columns

    return column_map


def parse_csv_export(csv_path: str) -> Dict[str, Any]:
    """
    Parse evaluation data from a CSV export.

    Args:
        csv_path: Path to the CSV file.

    Returns:
        Dict containing all evaluation data.
    """
    print(f"📂 Reading CSV: {csv_path}")

    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    repos = []
    started_at = None
    ended_at = None

    # For computing summary statistics
    success_count = 0
    total_pass_rate = 0.0
    total_tests_sum = 0
    passed_sum = 0
    failed_sum = 0
    errors_sum = 0
    latency_sum = 0.0
    pytest_executed_count = 0
    total_completion_cost = 0.0
    total_prompt_cost = 0.0
    total_tokens_sum = 0
    total_completion_tokens_sum = 0

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)  # Read header

        # Build mapping
        col_map = build_column_mapping(header)
        error_columns = col_map.get("error_columns", [])

        print(f"   Found {len(error_columns)} error-type columns")

        for row_idx, row in enumerate(reader, start=2):  # Start counting from line 2
            if len(row) < len(header):  # Ensure enough columns
                print(
                    f"⚠️  Skipping row {row_idx}: not enough columns ({len(row)} < {len(header)})"
                )
                continue

            # Extract repo name
            full_name_idx = col_map.get("full_name")
            if full_name_idx is None:
                print("⚠️  CSV is missing the 'full_name' column; cannot parse")
                break

            full_name = row[full_name_idx]
            if not full_name:
                print(f"⚠️  Skipping row {row_idx}: missing repo name")
                continue

            # Extract status
            status = (
                row[col_map["output_status"]]
                if "output_status" in col_map
                else "unknown"
            )

            # Extract success flag
            success = safe_bool(
                row[col_map["success"]] if "success" in col_map else "", False
            )
            if success:
                success_count += 1

            # Extract test results
            pytest_executed = safe_bool(
                row[col_map["pytest_executed"]] if "pytest_executed" in col_map else "",
                False,
            )
            pytest_total_tests = safe_int(
                row[col_map["pytest_total_tests"]]
                if "pytest_total_tests" in col_map
                else "",
                0,
            )
            pytest_pass_rate = safe_float(
                row[col_map["pytest_pass_rate"]]
                if "pytest_pass_rate" in col_map
                else "",
                0.0,
            )
            pytest_passed = safe_int(
                row[col_map["pytest_passed"]] if "pytest_passed" in col_map else "", 0
            )
            pytest_failed = safe_int(
                row[col_map["pytest_failed"]] if "pytest_failed" in col_map else "", 0
            )
            pytest_errors = safe_int(
                row[col_map["pytest_errors"]] if "pytest_errors" in col_map else "", 0
            )
            pass_rate_exclude = safe_float(
                row[col_map["pass_rate_exclude_code_issues"]]
                if "pass_rate_exclude_code_issues" in col_map
                else "",
                0.0,
            )

            # Extract error breakdown
            error_breakdown = {}
            for error_name, col_idx in error_columns:
                error_count = safe_int(row[col_idx], 0)
                if error_count > 0:
                    error_breakdown[error_name] = error_count

            # Extract execution time
            model_latency = safe_float(
                row[col_map["model_latency"]] if "model_latency" in col_map else "", 0.0
            )

            # Extract costs and usage
            completion_cost = safe_float(
                row[col_map["completion_tokens_total_cost"]]
                if "completion_tokens_total_cost" in col_map
                else "",
                0.0,
            )
            prompt_cost = safe_float(
                row[col_map["prompt_tokens_total_cost"]]
                if "prompt_tokens_total_cost" in col_map
                else "",
                0.0,
            )
            total_tokens = safe_int(
                row[col_map["total_tokens"]] if "total_tokens" in col_map else "", 0
            )
            completion_tokens = safe_int(
                row[col_map["completion_tokens"]]
                if "completion_tokens" in col_map
                else "",
                0,
            )

            # Extract timestamps
            if (
                not started_at
                and "started_at" in col_map
                and row[col_map["started_at"]]
            ):
                started_at = row[col_map["started_at"]]
            if "ended_at" in col_map and row[col_map["ended_at"]]:
                ended_at = row[col_map["ended_at"]]

            # Aggregates
            if pytest_executed:
                pytest_executed_count += 1
                total_pass_rate += pytest_pass_rate
                total_tests_sum += pytest_total_tests
                passed_sum += pytest_passed
                failed_sum += pytest_failed
                errors_sum += pytest_errors

            latency_sum += model_latency
            total_completion_cost += completion_cost
            total_prompt_cost += prompt_cost
            total_tokens_sum += total_tokens
            total_completion_tokens_sum += completion_tokens

            # Build per-repo data
            repo_data = {
                "full_name": full_name,
                "status": status,
                "success": success,
                "pytest_executed": pytest_executed,
                "pytest_total_tests": pytest_total_tests,
                "pytest_pass_rate": pytest_pass_rate,
                "pytest_passed": pytest_passed,
                "pytest_failed": pytest_failed,
                "pytest_errors": pytest_errors,
                "pass_rate_exclude_code_issues": pass_rate_exclude,
                "error_breakdown": error_breakdown,
                "model_latency": model_latency,
                "completion_cost": completion_cost,
                "prompt_cost": prompt_cost,
                "total_cost": completion_cost + prompt_cost,
                "total_tokens": total_tokens,
                "completion_tokens": completion_tokens,
            }

            repos.append(repo_data)

    total_repos = len(repos)
    print(f"   Parsed {total_repos} repos\n")

    # Compute summary
    summary = {
        "success_count": success_count,
        "success_rate": success_count / total_repos if total_repos > 0 else 0.0,
        "avg_pass_rate": (
            total_pass_rate / pytest_executed_count
            if pytest_executed_count > 0
            else 0.0
        ),
        "avg_total_tests": (
            total_tests_sum / pytest_executed_count
            if pytest_executed_count > 0
            else 0.0
        ),
        "avg_passed": passed_sum / pytest_executed_count
        if pytest_executed_count > 0
        else 0.0,
        "avg_failed": failed_sum / pytest_executed_count
        if pytest_executed_count > 0
        else 0.0,
        "avg_errors": errors_sum / pytest_executed_count
        if pytest_executed_count > 0
        else 0.0,
        "avg_latency": latency_sum / total_repos if total_repos > 0 else 0.0,
        "total_completion_cost": total_completion_cost,
        "total_prompt_cost": total_prompt_cost,
        "total_cost": total_completion_cost + total_prompt_cost,
        "avg_cost_per_repo": (total_completion_cost + total_prompt_cost) / total_repos
        if total_repos > 0
        else 0.0,
        "total_tokens": total_tokens_sum,
        "total_completion_tokens": total_completion_tokens_sum,
        "avg_tokens_per_repo": total_tokens_sum / total_repos
        if total_repos > 0
        else 0.0,
    }

    return {
        "eval_id": "csv_export",
        "eval_name": os.path.basename(csv_path),
        "started_at": started_at or "N/A",
        "ended_at": ended_at or "N/A",
        "summary": summary,
        "repos": repos,
    }


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Generate a LaTeX evaluation report from a CSV export"
    )

    parser.add_argument("csv_file", type=str, help="Path to the CSV export")

    parser.add_argument(
        "--output-dir",
        type=str,
        default="output/eval_reports",
        help="Output directory (default: output/eval_reports)",
    )

    parser.add_argument("--no-compile", action="store_true", help="Do not compile PDF")

    parser.add_argument("--save-json", action="store_true", help="Save parsed JSON")

    return parser.parse_args()


def main() -> int:
    """Main entrypoint."""
    args = parse_args()

    print("=" * 60)
    print("📊 LaTeX Evaluation Report Generator")
    print("=" * 60)
    print(f"CSV file: {args.csv_file}")
    print("=" * 60 + "\n")

    try:
        # 1. Parse CSV
        print("🔍 Parsing CSV...\n")
        data = parse_csv_export(args.csv_file)

        # 2. Optionally save parsed JSON
        if args.save_json:
            os.makedirs(args.output_dir, exist_ok=True)
            csv_basename = os.path.splitext(os.path.basename(args.csv_file))[0]
            json_path = os.path.join(args.output_dir, f"{csv_basename}_parsed.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"✅ Parsed data saved: {json_path}\n")

        # 3. Generate LaTeX report
        print("📝 Generating LaTeX report...")
        generator = LaTeXReportGenerator(data)

        os.makedirs(args.output_dir, exist_ok=True)

        # Output name based on CSV basename
        csv_basename = os.path.splitext(os.path.basename(args.csv_file))[0]
        tex_path = os.path.join(args.output_dir, f"{csv_basename}_report.tex")

        pdf_path = generator.generate_full_report(
            output_path=tex_path, auto_compile=not args.no_compile
        )

        print(f"✅ LaTeX report generated: {tex_path}")

        if pdf_path and os.path.exists(pdf_path):
            print(f"✅ PDF report generated: {pdf_path}")
        elif not args.no_compile:
            print("⚠️  PDF compilation failed; compile manually:")
            print(f"   cd {args.output_dir} && pdflatex {os.path.basename(tex_path)}")

        print("\n" + "=" * 60)
        print("✅ Report generation completed")
        print("=" * 60)

        return 0

    except Exception as e:
        print(f"\n❌ Failed to generate report: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
