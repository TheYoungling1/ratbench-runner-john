"""Honest ground-truth labels from the 4 existing VM baseline runs.

Spec: docs/superpowers/loops/2026-07-02-graph-fidelity-eval-loop.md §2 (ground truth) and
§3 item 2. Two concerns, kept in separate functions:

  - `rescore()` — pure re-score of one repo's raw `run_pytest_results.json` into honest
    `pass_rate` / `honest_pass` (tau=0.8). Delegates to `scripts.compute_essr.official_pass_rate`
    as the SINGLE scoring authority (honest-scoring guardrail, §7) — never reimplements
    pytest scoring.
  - `fetch_baseline_raw()` / `fetch_dataset()` — thin, one-shot VM pull of the raw pytest jsons
    and the corpus dataset. Bootstrap-only: never call these in the hot improve loop (§2/§7).
  - `build_labels()` — aggregates the raw cache into the labels dict (schema in the module
    docstring of the CLI, and §3 item 2 of the loop doc).

RAT is the feasibility ceiling (§2): a repo RAT cannot honestly pass drops out of OUR recall
denominator, so each repo's `feasible` flag is exactly RAT's `honest_pass`.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.compute_essr import official_pass_rate

TAU = 0.8  # honest_pass threshold (§2)

VM_HOST = "root@167.233.64.96"
DATASET_REMOTE_PATH = "/opt/harness/datasets/rat_python_medlarge15.json"

# §2 baseline table — VM run dirs, one per baseline.
BASELINE_RUN_DIRS: Dict[str, str] = {
    "rat": "/opt/runs/baselines/rat_medlarge15-20260627-134444",
    "repo2run": "/opt/runs/baselines/repo2run_medlarge15-20260627-162444",
    "ccdf": "/opt/runs/baselines/ccdf_medlarge15-20260627-183205",
    "radical": "/opt/runs/radical/radical-medlarge15-20260628-025304-20260628-025305",
}

RAW_CACHE_DIR = _REPO_ROOT / "outputs" / "graph_fidelity" / "_raw"
DATASET_CACHE_PATH = _REPO_ROOT / "datasets" / "rat_python_medlarge15.json"
LABELS_OUTPUT_PATH = _REPO_ROOT / "outputs" / "graph_fidelity" / "baseline_labels.json"


# ---------------------------------------------------------------------------
# Concern 1: re-score (pure, unit-tested, no VM/network)
# ---------------------------------------------------------------------------

def rescore(results: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Honest pass_rate + honest_pass for one repo under one baseline.

    `results` is the parsed `run_pytest_results.json` content, or None when that baseline
    produced no results file for the repo. Absent is never scored as a fail — it is recorded
    honestly as `present=False` (the repo/baseline pair simply drops out of that baseline's
    coverage, per §3 item 2 / §6).
    """
    if results is None:
        return {"present": False, "pass_rate": 0.0, "honest_pass": False}
    try:
        pass_rate, _pr_excl, _passed, _eff_total = official_pass_rate(results)
    except (TypeError, AttributeError):
        # Malformed-but-present results (e.g. null summary fields): compute_essr's own scorer
        # treats this as not-executed. Mirror that rather than inventing a score.
        return {"present": True, "pass_rate": 0.0, "honest_pass": False}
    return {"present": True, "pass_rate": pass_rate, "honest_pass": pass_rate >= TAU}


# ---------------------------------------------------------------------------
# Concern 2: fetch (thin VM wrapper, bootstrap-only) + aggregate
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    """Load a JSON file, or None if it doesn't exist / fails to parse."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def fetch_baseline_raw(raw_dir: Path = RAW_CACHE_DIR) -> Dict[str, int]:
    """One-shot pull of each baseline's per-repo `run_pytest_results.json` files from the VM.

    Streams a tar of just the pytest-results files over ssh (not full run dirs — no
    containers/logs) so the fetch stays small. Bootstrap-only (§2/§7): never call this in the
    hot improve loop.
    """
    counts: Dict[str, int] = {}
    for baseline, run_dir in BASELINE_RUN_DIRS.items():
        dest = raw_dir / baseline
        dest.mkdir(parents=True, exist_ok=True)
        remote_cmd = (
            f"cd {run_dir}/output && "
            "find . -name run_pytest_results.json -print0 | tar --null -T - -cf -"
        )
        ssh = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", VM_HOST, remote_cmd],
            stdout=subprocess.PIPE,
        )
        subprocess.run(["tar", "-xf", "-", "-C", str(dest)], stdin=ssh.stdout, check=True)
        if ssh.stdout is not None:
            ssh.stdout.close()
        ssh.wait()
        if ssh.returncode != 0:
            raise RuntimeError(f"ssh fetch failed for baseline {baseline!r} (rc={ssh.returncode})")
        counts[baseline] = sum(1 for _ in dest.rglob("run_pytest_results.json"))
    return counts


def fetch_dataset(dest: Path = DATASET_CACHE_PATH) -> None:
    """One-shot pull of the 15-repo corpus dataset (org/name + SHAs) from the VM."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["scp", "-o", "BatchMode=yes", f"{VM_HOST}:{DATASET_REMOTE_PATH}", str(dest)],
        check=True,
    )


def _repo_id(full_name: str) -> str:
    org, name = full_name.split("/", 1)
    return f"{org}__{name}"


def build_labels(dataset_repos: List[Dict[str, Any]], raw_dir: Path = RAW_CACHE_DIR) -> Dict[str, Any]:
    """Re-score every repo x baseline from the raw cache and assemble the labels dict.

    Schema per repo: `{rat, repo2run, ccdf, radical}` each `{present, pass_rate, honest_pass}`,
    plus `feasible` = `rat.honest_pass` (RAT is the feasibility ceiling, §2).
    """
    labels: Dict[str, Any] = {}
    for repo in dataset_repos:
        org, name = repo["full_name"].split("/", 1)
        entry: Dict[str, Any] = {}
        for baseline in BASELINE_RUN_DIRS:
            results_path = raw_dir / baseline / org / name / "run_pytest_results.json"
            entry[baseline] = rescore(_load_json(results_path))
        entry["feasible"] = entry["rat"]["honest_pass"]
        labels[_repo_id(repo["full_name"])] = entry
    return labels


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true",
                         help="Pull raw pytest jsons + dataset from the VM before building labels.")
    parser.add_argument("--raw-dir", type=Path, default=RAW_CACHE_DIR)
    parser.add_argument("--dataset", type=Path, default=DATASET_CACHE_PATH)
    parser.add_argument("--out", type=Path, default=LABELS_OUTPUT_PATH)
    args = parser.parse_args(argv)

    if args.fetch:
        fetch_dataset(args.dataset)
        counts = fetch_baseline_raw(args.raw_dir)
        print(f"Fetched: {counts}")

    dataset = json.loads(args.dataset.read_text())
    labels = build_labels(dataset["repos"], args.raw_dir)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(labels, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {args.out} ({len(labels)} repos)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
