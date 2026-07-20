"""
extract_ablation_metrics.py — per-§4.5 / §9.5 of the ablation experiment design.

Globs agent_run_summary.json + results/*.json under a given root, infers
(instance_id, arm, replicate) from the path layout produced by
run_repo2run_benchmark.py --arm {0,A,B,C} --output-root <root>/rep<N>,
extracts per-repo metrics, and emits clean raw CSV + JSON artifacts.

Directory convention (§9.2):
  <search_root>/
    arm0_bare_react/rep1/results/<id>.json
    arm0_bare_react/rep1/workplaces/<id>/agent_run_summary.json
    armA_fullstate/rep2/results/<id>.json
    ...

Arm labels are inferred from directory name components:
  arm0*  → arm "0"
  armA*  → arm "A"
  armB*  → arm "B"
  armC*  → arm "C"

Replicate is inferred from a "rep<N>" component in the path.

Usage:
    python scripts/extract_ablation_metrics.py --root outputs/ablation \
        --output-csv results_raw.csv --output-json results_raw.json
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Arm-label inference helpers
# ---------------------------------------------------------------------------

_ARM_LABEL_RE = re.compile(
    r"(?:^|[/\\])"
    r"(arm0[^/\\]*|armA[^/\\]*|armB[^/\\]*|armC[^/\\]*)",
    re.IGNORECASE,
)
_REP_RE = re.compile(r"(?:^|[/\\])rep(\d+)(?:[/\\]|$)", re.IGNORECASE)

_LABEL_TO_ARM: dict[str, str] = {
    "0": "0",
    "a": "A",
    "b": "B",
    "c": "C",
}


def _infer_arm(path: Path) -> str | None:
    """Return arm identifier {0,A,B,C} from a path, or None if not found."""
    path_str = str(path)
    m = _ARM_LABEL_RE.search(path_str)
    if not m:
        return None
    label = m.group(1).lower()  # e.g. "arm0_bare_react" or "arma_fullstate"
    for key, arm in _LABEL_TO_ARM.items():
        if label.startswith(f"arm{key}"):
            return arm
    return None


def _infer_replicate(path: Path) -> int | None:
    """Return replicate number from a path, or None if not found."""
    path_str = str(path)
    m = _REP_RE.search(path_str)
    if m:
        return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Token-bucket extraction helpers
# ---------------------------------------------------------------------------

def _token_bucket(usage: dict[str, Any] | None, bucket: str) -> int | None:
    """Return total_tokens for a named bucket, or None if absent."""
    if not isinstance(usage, dict):
        return None
    b = usage.get(bucket)
    if not isinstance(b, dict):
        return None
    return b.get("total_tokens")


def _token_total(usage: dict[str, Any] | None) -> int | None:
    """Return total_tokens from the 'total' bucket, or None."""
    return _token_bucket(usage, "total")


# ---------------------------------------------------------------------------
# Core extraction function (pure; unit-testable)
# ---------------------------------------------------------------------------

def extract_row(
    summary: dict[str, Any] | None,
    results: dict[str, Any] | None,
    arm: str,
    instance_id: str,
    replicate: int | None,
) -> dict[str, Any]:
    """
    Build one metrics row from (agent_run_summary, results_json, arm, id, rep).

    Rules per §4.5:
    - Missing / unparseable / error → success fields = 0, kept in denominator.
    - Arm 0 rows lack cleanroom / supervisor / worker / reflection / final_rev:
      those fields are null, not an error.
    - n_actions = len(successful_actions) + len(failed_actions), falling back
      to len(action_ledger).
    """
    row: dict[str, Any] = {
        "instance_id": instance_id,
        "arm": arm,
        "replicate": replicate,
        # --- primary outcome (from results JSON) ---
        "environment_build_success": 0,
        "dockerfile_generation_success": 0,
        "execution_status": None,
        # --- in-agent success claim ---
        "configuration_success": 0,
        # --- cleanroom (EnvState arms only; Arm 0 → null) ---
        "cleanroom_passed": None,
        # --- test commands ---
        "verified_test_commands": None,
        # --- token buckets ---
        "total_tokens": None,
        "supervisor_tokens": None,   # 0 by construction in Arm A; null in Arm 0
        "worker_tokens": None,       # null in Arm 0
        "reflection_tokens": None,   # null in Arm 0
        "planner_tokens": None,      # Arm 0 legacy bucket
        # --- action counts ---
        "n_actions": None,
        # --- orchestrator / supervisor fields (EnvState arms only) ---
        "no_more_tasks": None,
        "n_tasks_proxy": None,       # distinct task IDs in action_ledger
        "n_maintainer": None,
        # --- snapshot / revision (EnvState arms only) ---
        "final_rev": None,
        # --- timing ---
        "duration_seconds": None,
        # --- error flag ---
        "has_error": 0,
        # --- paper stratum ---
        "paper_build_success": None,
    }

    # ---- results JSON fields ----
    if isinstance(results, dict):
        row["environment_build_success"] = int(bool(results.get("environment_build_success")))
        row["dockerfile_generation_success"] = int(bool(results.get("dockerfile_generation_success")))
        row["execution_status"] = results.get("execution_status")
        row["paper_build_success"] = results.get("dataset_entry", {}).get("paper_build_success")
        # Duration from agent_run sub-dict in results
        agent_run = results.get("agent_run") or {}
        row["duration_seconds"] = agent_run.get("duration_seconds")

    # ---- summary JSON fields ----
    if not isinstance(summary, dict):
        # Treat as total failure; all success fields already 0.
        return row

    row["configuration_success"] = int(bool(summary.get("configuration_success")))
    row["has_error"] = int(bool(summary.get("error")))

    # Verified test commands
    vtc = summary.get("verified_test_commands")
    if vtc is None:
        vtc = summary.get("verified_test_command")
        if vtc is not None:
            vtc = [vtc] if vtc else []
    row["verified_test_commands"] = vtc if isinstance(vtc, list) else None

    # Token usage
    usage = summary.get("token_usage")
    row["total_tokens"] = _token_total(usage)
    row["supervisor_tokens"] = _token_bucket(usage, "supervisor")
    row["worker_tokens"] = _token_bucket(usage, "worker")
    row["reflection_tokens"] = _token_bucket(usage, "reflection")
    row["planner_tokens"] = _token_bucket(usage, "planner")

    # n_actions: prefer successful+failed sum; fall back to action_ledger length.
    successful = summary.get("successful_actions")
    failed = summary.get("failed_actions")
    if isinstance(successful, list) and isinstance(failed, list):
        row["n_actions"] = len(successful) + len(failed)
    else:
        ledger = summary.get("action_ledger")
        if isinstance(ledger, list):
            row["n_actions"] = len(ledger)

    # n_tasks_proxy: count distinct task_ids in action_ledger (EnvState arms).
    ledger = summary.get("action_ledger")
    if isinstance(ledger, list) and ledger:
        task_ids = {
            entry.get("task_id") for entry in ledger if isinstance(entry, dict)
        }
        task_ids.discard(None)
        row["n_tasks_proxy"] = len(task_ids) if task_ids else None

    # no_more_tasks: from orchestrator stop_reason block if present.
    envstate_block = summary.get("envstate") or {}
    stop_reason = envstate_block.get("stop_reason") or summary.get("stop_reason")
    if stop_reason is not None:
        row["no_more_tasks"] = int(stop_reason == "no_more_tasks")

    # n_maintainer: count action events where Maintainer was called.
    # Proxy: events in action_ledger with a maintainer_interpretation key.
    if isinstance(ledger, list):
        row["n_maintainer"] = sum(
            1
            for entry in ledger
            if isinstance(entry, dict) and entry.get("maintainer_interpretation") is not None
        )

    # Cleanroom block (EnvState arms only).
    cleanroom = summary.get("cleanroom")
    if isinstance(cleanroom, dict):
        passed = cleanroom.get("passed")
        row["cleanroom_passed"] = int(bool(passed)) if passed is not None else None

    # final_rev from envstate block or snapshot.
    if envstate_block:
        row["final_rev"] = envstate_block.get("final_revision")
    if row["final_rev"] is None:
        snapshot = summary.get("final_snapshot") or summary.get("env_snapshot")
        if isinstance(snapshot, dict):
            row["final_rev"] = snapshot.get("revision")

    return row


# ---------------------------------------------------------------------------
# Filesystem traversal
# ---------------------------------------------------------------------------

def _safe_load_json(path: Path) -> dict[str, Any] | None:
    """Load JSON from path; return None on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _collect_rows(root: Path) -> list[dict[str, Any]]:
    """
    Walk root, pair agent_run_summary.json files with their results/<id>.json
    sibling, and call extract_row for each.

    Expected layout (§9.2):
      <root>/<arm_dir>/<rep_dir>/results/<id>.json
      <root>/<arm_dir>/<rep_dir>/workplaces/<id>/agent_run_summary.json
    """
    rows: list[dict[str, Any]] = []

    for summary_path in sorted(root.rglob("agent_run_summary.json")):
        # The workplace dir is summary_path.parent; its name is the safe_instance_id.
        instance_id = summary_path.parent.name

        arm = _infer_arm(summary_path)
        replicate = _infer_replicate(summary_path)

        # Locate the sibling results JSON: go up from workplace dir to cell root,
        # then into results/<id>.json.
        # workplaces/<id>/agent_run_summary.json  → parent.parent = workplaces dir
        # workplaces dir → parent = cell root (arm+rep dir)
        cell_root = summary_path.parent.parent.parent  # cell_root/workplaces/<id>/summary
        results_path = cell_root / "results" / f"{instance_id}.json"

        summary = _safe_load_json(summary_path)
        results = _safe_load_json(results_path) if results_path.exists() else None

        row = extract_row(
            summary=summary,
            results=results,
            arm=arm if arm is not None else "unknown",
            instance_id=instance_id,
            replicate=replicate,
        )
        rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# CSV / JSON emission
# ---------------------------------------------------------------------------

_CSV_FIELDS = [
    "instance_id",
    "arm",
    "replicate",
    "paper_build_success",
    "environment_build_success",
    "dockerfile_generation_success",
    "configuration_success",
    "cleanroom_passed",
    "verified_test_commands",
    "total_tokens",
    "supervisor_tokens",
    "worker_tokens",
    "reflection_tokens",
    "planner_tokens",
    "n_actions",
    "no_more_tasks",
    "n_tasks_proxy",
    "n_maintainer",
    "final_rev",
    "duration_seconds",
    "execution_status",
    "has_error",
]


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            # Stringify list fields for CSV readability.
            csv_row = dict(row)
            vtc = csv_row.get("verified_test_commands")
            if isinstance(vtc, list):
                csv_row["verified_test_commands"] = json.dumps(vtc)
            writer.writerow(csv_row)


def write_json(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# ESSR summary helper (÷all, paper-faithful per §4.1)
# ---------------------------------------------------------------------------

def compute_essr(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return per-arm ESSR ÷ all-assigned rows (failures count as 0)."""
    from collections import defaultdict

    arm_total: dict[str, int] = defaultdict(int)
    arm_success: dict[str, int] = defaultdict(int)
    for row in rows:
        arm = row["arm"]
        arm_total[arm] += 1
        arm_success[arm] += row.get("environment_build_success") or 0

    summary = {}
    for arm in sorted(arm_total):
        total = arm_total[arm]
        success = arm_success[arm]
        summary[arm] = {
            "assigned": total,
            "success": success,
            "essr_div_all": round(success / total, 4) if total else None,
        }
    return summary


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract per-repo ablation metrics from a benchmark output tree."
    )
    parser.add_argument(
        "--root",
        required=True,
        help="Root directory to search (e.g. outputs/ablation).",
    )
    parser.add_argument(
        "--output-csv",
        default="ablation_metrics.csv",
        help="Output CSV path. Defaults to ablation_metrics.csv.",
    )
    parser.add_argument(
        "--output-json",
        default="ablation_metrics.json",
        help="Output JSON path. Defaults to ablation_metrics.json.",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.exists():
        print(f"ERROR: root directory does not exist: {root}", file=sys.stderr)
        return 1

    rows = _collect_rows(root)
    print(f"Collected {len(rows)} rows from {root}", file=sys.stderr)

    essr = compute_essr(rows)
    print("ESSR ÷ all:", json.dumps(essr, indent=2), file=sys.stderr)

    write_csv(rows, Path(args.output_csv))
    write_json(rows, Path(args.output_json))
    print(f"Wrote {args.output_csv} and {args.output_json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
