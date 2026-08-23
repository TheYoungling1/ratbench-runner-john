"""run_v3_proof — the e2e proof-harness reporting driver (Task 8d).

Runs ``scripts/run_v3_e2e.py`` once per repo (shelling out — each repo gets
its own Sandbox/container lifecycle, exactly like invoking ``run_v3_e2e.py``
by hand), collects each repo's ``RunTrace`` JSON (``--trace-out``) + rendered
``setup.sh`` (``--out``), and prints:

  * the per-repo table (``repo_row`` + the composite ``canonical_success``
    predicate), and
  * the aggregate counters (``aggregate``) — ``legacy_path_violations``,
    ``manual_block_artifact_mismatches``, and
    ``local_import_false_package_attempts`` MUST all be 0 for a clean proof
    run.

All actual computation (row shape, aggregation, ``canonical_success``) is
PURE and lives in ``src/envstate/proof.py`` — this module is I/O plumbing
(subprocess + file reads) and formatting only, so it has no unit tests of its
own beyond an import + argparse smoke test; the pure logic is exhaustively
covered by ``tests/envstate/test_proof.py``.

NOT run in CI — requires Docker + a real LLM API key, same as run_v3_e2e.py.

Usage:
  python scripts/run_v3_proof.py --repos <dir> [<dir> ...] [--model <slug>]
         [--base-image python:3.11-slim] [--out-dir proof_out]
  python scripts/run_v3_proof.py --manifest <path/to/repos.txt> [...]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


def _build_arg_parser() -> argparse.ArgumentParser:
    """Pure argparse construction — no ``src.*``/``python_deps.*`` imports and
    no subprocess calls, so this is safe to call in a test process with no
    Docker or LLM key (Task 8d smoke test: parses ``--repos``).
    """
    ap = argparse.ArgumentParser(
        description="v3 e2e proof harness — per-repo table + aggregate counters."
    )
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--repos", nargs="+", default=None,
                        help="Repo directories to run the e2e driver over.")
    group.add_argument("--manifest", default=None,
                        help="Path to a newline-delimited file of repo directories "
                             "(blank lines and lines starting with '#' are skipped).")
    ap.add_argument("--model", default=None, help="LLM model slug (forwarded to run_v3_e2e).")
    ap.add_argument("--base-image", default="python:3.11-slim", dest="base_image")
    ap.add_argument("--out-dir", default="proof_out", dest="out_dir",
                     help="Directory to write each repo's setup.sh + trace JSON into.")
    return ap


def _load_repos(args: argparse.Namespace) -> list[str]:
    if args.repos:
        return list(args.repos)
    with open(args.manifest) as fh:
        return [line.strip() for line in fh if line.strip() and not line.strip().startswith("#")]


def _run_one_e2e(
    repo: str, *, model: str | None, base_image: str, out_dir: str,
) -> tuple[int, str, str]:
    """Shell out to run_v3_e2e.py for ONE repo. Returns (returncode, script_path, trace_path)."""
    os.makedirs(out_dir, exist_ok=True)
    name = os.path.basename(os.path.normpath(repo)) or "repo"
    script_path = os.path.join(out_dir, f"{name}.setup.sh")
    trace_path = os.path.join(out_dir, f"{name}.trace.json")
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = [
        sys.executable, os.path.join(_root, "scripts", "run_v3_e2e.py"), repo,
        "--base-image", base_image, "--out", script_path, "--trace-out", trace_path,
    ]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd)
    return proc.returncode, script_path, trace_path


def _print_table(rows: list[dict]) -> None:
    cols = [
        "repo", "result", "legacy_used", "graph_nodes_added", "patchgate_accepts",
        "manual_blocks", "fresh_replay", "tests_pass", "residual_reason",
        "canonical_success",
    ]
    print(" | ".join(cols))
    for row in rows:
        print(" | ".join(str(row.get(c, "")) for c in cols))


def _print_aggregate(agg: dict) -> None:
    for key, value in agg.items():
        print(f"[proof] {key} = {value}")


def main() -> int:
    args = _build_arg_parser().parse_args()
    repos = _load_repos(args)

    # `src.*` imports deferred (mirrors run_v3_e2e.py): repo root + src/ go on
    # sys.path here, not at module import time, so this file stays importable
    # standalone (Task 8d smoke test) with no Docker/LLM dependency.
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for _p in (_root, os.path.join(_root, "src")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    from src.envstate.proof import aggregate, canonical_success, repo_row, trace_from_dict

    pairs: list[tuple] = []  # (RunTrace, script_text), one per repo that produced a trace
    rows: list[dict] = []
    for repo in repos:
        rc, script_path, trace_path = _run_one_e2e(
            repo, model=args.model, base_image=args.base_image, out_dir=args.out_dir,
        )
        if not os.path.exists(trace_path):
            print(f"[proof] {repo}: e2e driver exited rc={rc} with no trace written — skipping row")
            continue

        script_text = ""
        if os.path.exists(script_path):
            with open(script_path) as fh:
                script_text = fh.read()
        with open(trace_path) as fh:
            trace = trace_from_dict(json.load(fh))

        pairs.append((trace, script_text))
        row = repo_row(trace)
        row["canonical_success"] = canonical_success(trace, script_text)
        rows.append(row)

    _print_table(rows)
    agg = aggregate(pairs)
    _print_aggregate(agg)

    clean = (
        agg["legacy_path_violations"] == 0
        and agg["manual_block_artifact_mismatches"] == 0
        and agg["local_import_false_package_attempts"] == 0
    )
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
