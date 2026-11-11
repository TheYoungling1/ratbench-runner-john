"""Recompute the paper's Table 1 (EBSR / ESSR) from runs_results/ and check it against the paper.

Stdlib only. Reads the original measure rows in runs_results/<arm>/<run>/measure/ and the gold test
counts in datasets/gold_manifests_combined_test_steps.json (one count for each of the 200 repos),
verifies every file against runs_results/SHA256SUMS first, then scores:

  EBSR  repo built, conforming, and pytest collected cleanly (pypa/distutils never counts)
  ESSR  min(passed / gold test count, 1) per repo
  Both are averaged over the arm's own rows: 200 for every arm except RAT (193 rows).

  python3 tools/reproduce_paper_table.py            # exits 1 on any checksum or table mismatch
"""
import glob
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "runs_results")
GOLD = os.path.join(ROOT, "datasets", "gold_manifests_combined_test_steps.json")
DISQUALIFIED = {"non_conforming", "empty_testbed", "build_fail", "missing", "measure_error", "error",
                "config_restore_failed"}
UNTRUSTED = {"pypa/distutils"}          # dropped from the corpus: its pinned commit is unreachable
# Submitted paper, Table 1 (Graph2Env-Paper 06c0b08): arm -> (label, rows, EBSR %, ESSR %).
PAPER = {"pipreqs": ("pipreqs", 200, 10.0, 11.6), "repo2run": ("Repo2Run", 200, 42.5, 34.7),
         "rat": ("RAT", 193, 44.0, 40.4), "setupx": ("SetupX", 200, 52.5, 45.9),
         "sweagent": ("SWE-agent", 200, 50.0, 35.6), "claudecode": ("Claude Code", 200, 71.5, 50.3),
         "graph2env": ("Graph2Env", 200, 81.0, 59.3)}
TOLERANCE = 0.06    # one-decimal rounding; SWE-agent's ESSR is 35.65 against the printed 35.6


def verify_checksums() -> int:
    bad = 0
    with open(os.path.join(RESULTS, "SHA256SUMS")) as f:
        for line in f:
            digest, rel = line.rstrip("\n").split("  ", 1)
            try:
                with open(os.path.join(RESULTS, rel), "rb") as fh:
                    actual = hashlib.sha256(fh.read()).hexdigest()
            except FileNotFoundError:
                actual = None
            if actual != digest:
                print(f"{'MISSING' if actual is None else 'CHECKSUM MISMATCH'} {rel}")
                bad += 1
    return bad


def load_rows(arm: str) -> dict:
    rows = {}
    for p in glob.glob(os.path.join(RESULTS, arm, "*", "measure", "**", "row.json"), recursive=True):
        with open(p) as f:
            r = json.load(f)
        name = r["repo"].lower()
        if name in rows:
            raise ValueError(f"duplicate repo {name} in {arm}")
        rows[name] = r
    return rows


def score(rows: dict, gold: dict) -> tuple:
    ebsr = sum(n not in UNTRUSTED and r.get("status") not in DISQUALIFIED and bool(r.get("collect_clean"))
               for n, r in rows.items())
    essr = sum(min((r.get("passed") or 0) / gold[n], 1.0) for n, r in rows.items())
    return 100 * ebsr / len(rows), 100 * essr / len(rows)


def main() -> int:
    failures = verify_checksums()
    with open(GOLD) as f:
        gold = {r["full_name"].lower(): r["test_count"] for r in json.load(f)["repos"]}
    print(f"{'Method':12} {'rows':>4}  {'EBSR':>7} {'paper':>6}  {'ESSR':>7} {'paper':>6}")
    for arm, (label, n_expected, p_ebsr, p_essr) in PAPER.items():
        rows = load_rows(arm)
        ebsr, essr = score(rows, gold)
        ok = (len(rows) == n_expected and abs(ebsr - p_ebsr) < TOLERANCE
              and abs(essr - p_essr) < TOLERANCE)
        failures += not ok
        print(f"{label:12} {len(rows):>4}  {ebsr:7.2f} {p_ebsr:6.1f}  {essr:7.2f} {p_essr:6.1f}"
              f"  {'ok' if ok else 'MISMATCH'}")
    print("reproduced" if not failures else f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
