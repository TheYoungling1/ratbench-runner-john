#!/usr/bin/env python3
"""Gold-anchored ESSR over envbench_python100: mean of per-repo passed/gold_test_count.

WHY THIS EXISTS
---------------
The headline ESSR (bench/bench/metrics.py:83) divides by the AGENT'S OWN pytest collection:

    pass_rate = passed / (total - skipped);  ESSR = mean(pass_rate) over EXECUTED repos

That denominator floats with the environment the agent built. A broken env that collects 12
tests and passes 11 scores 0.92 — identical to one that collects 1400 and passes 1288. Anchoring
the denominator to an independently certified count removes that.

    P_r  = min(passed_r / gold_r, 1.0)        per repo, capped
    ESSR_gold = mean(P_r) over the scored repos

MACRO, NOT MICRO, and deliberately so: a 1391-test repo counts exactly as much as a 16-test one,
so a single large suite cannot dilute or dominate the metric.

SCORING DECISIONS (set by the operator, 2026-09-12)
  * cap at 1.0        — passed can exceed gold when the agent collects tests outside the gold
                        manifest; a repo "scoring" 1.4 is a pairing bug, not a win. Capped repos
                        are counted and reported so the cap never hides silently.
  * exclude no-gold   — the 8 repos with neither a certificate nor a human count are DROPPED
                        (n=92), not scored 0. A gap in our ground truth is not an agent failure.
  * build failures 0  — a repo that never built scores 0.0 and STAYS IN the average. This is the
                        whole point of anchoring: collecting nothing must read as zero, not
                        vanish from the denominator the way ÷exec ESSR lets it.

GOLD SOURCES (92 repos, two tiers)
  85 CERTIFIED : gt100/corpus_results.jsonl -> manifest_size, verified == len(collected-nodeids)
   7 HUMAN     : human_manual_counts.json   -> human_collected (hand-verified after the harness
                 rejected them; count only, no node-ids, which is why a scalar ratio is used)

KNOWN BIAS: the gold harness runs `pytest --collect-only` over the whole source tree, overriding
`testpaths`, so gold_r includes dev scripts maintainers deliberately exclude. Every arm is biased
LOW, and unevenly (worse for repos with large excluded dev trees). Consistent across arms, so the
RANKING holds; the absolute value is not "fraction of the real suite passed".

RUN COVERAGE: no arm was ever run on envbench_python100 directly. envbench100 == 61 repos that
are also in envbench_python70 + the 39 of envbench_python100_minus70, and every arm ran both, so
all 100 are covered by pooling two runs per arm. Per-repo rows are independent, so this is sound —
but it IS a pool of two runs and is reported as such.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Gold lives outside the repo (it is certified separately). Override for another machine.
GT_DIR = os.environ.get("RATBENCH_GT_DIR", "/Users/john/john-v3-multi-lang/artifacts")

# (arm label, run directory relative to REPO_ROOT).
# envbench100 has no direct run: each arm is POOLED from its envbench70 and minus70 runs, which
# are disjoint and together cover all 100 (see module docstring). rat50 ran directly, one run each.
ENVBENCH100_RUNS = [
    ("setupx", "runs/setupx/envbench70-xpu-on-20260908-133913"),
    ("setupx", "runs/setupx/minus70-xpu-on-20260909-042630"),
    ("claudecode-dockerfile", "runs/claudecode-dockerfile/envbench70-20260908-203504"),
    ("claudecode-dockerfile", "runs/claudecode-dockerfile/minus70-20260909-083859"),
    ("sweagent_modern", "runs/sweagent_repo2run_modern/envbench70-modern-20260909-020413"),
    ("sweagent_modern", "runs/sweagent_repo2run_modern/minus70-modern-20260909-103702"),
]

# rat_python50. The sweagent arm here is sweagent_repo2run (the paper's thought_action scaffold),
# NOT sweagent_modern — that arm did not exist when these ran, so the two datasets' sweagent rows
# are DIFFERENT ARMS and must not be read as a same-arm comparison.
# run-20260903-161432 is chosen over run-20260903-152004 deliberately: 152004 predates the DSML
# fixes and lost 25 of 50 repos to parser format deaths, which inflates its ÷exec ESSR (the dead
# repos drop out) while depressing EBSR. 161432 is at 9c34439, the last DSML fix, exit_format 1.
RAT50_RUNS = [
    ("setupx", "runs/setupx/run-20260904-173735"),
    ("claudecode-dockerfile", "runs/claudecode-dockerfile/run-20260904-134239"),
    ("sweagent_repo2run", "runs/sweagent_repo2run/run-20260903-161432"),
]

RAT50_GOLD = os.environ.get(
    "RATBENCH_RAT50_GOLD",
    "/Users/john/john-v3-multi-lang/claude_gt_py_50/rat_python50_gold.json")

# Verbatim from bench/bench/metrics.py:12 — a status in this set never earns EBSR credit.
_DISQUALIFIED = ("non_conforming", "empty_testbed", "build_fail", "missing", "measure_error",
                 "error")


def repo_from_url(url: str) -> str:
    """`https://github.com/censys/censys-python.git` -> `censys/censys-python`."""
    s = url.strip().removesuffix(".git").rstrip("/")
    return "/".join(s.split("/")[-2:])


def load_gold_rat50() -> tuple[dict, dict]:
    """{full_name_lower: manifest_size} and tier, from rat_python50_gold.json.

    This is the pinned gold JSON bench/gold.py was written against and whose absence is the
    reason gold-anchored scoring was deprecated (metrics.py:91). Only CERTIFIED repos supply a
    denominator; the rejected ones are dropped, exactly as the 8 uncounted envbench100 repos are.
    Verified once at load: gold `sha` must equal the dataset's pinned commit, because a test count
    taken at a different commit is not ground truth for the pinned one.
    """
    with open(RAT50_GOLD, encoding="utf-8") as fh:
        doc = json.load(fh)
    gold, tier = {}, {}
    for name, rec in doc["repos"].items():
        if rec.get("status") != "CERTIFIED":
            continue
        size = rec.get("manifest_size")
        if not isinstance(size, int) or size <= 0:
            continue
        gold[name.lower()] = size
        tier[name.lower()] = "certified"
    return gold, tier


def check_shas(dataset: list, gold_doc_path: str) -> list:
    """Repos whose gold sha differs from the dataset's pinned commit. Empty list is the good case."""
    with open(gold_doc_path, encoding="utf-8") as fh:
        repos = json.load(fh)["repos"]
    pinned = {r["full_name"].lower(): r.get("commit") for r in dataset}
    bad = []
    for name, rec in repos.items():
        want = pinned.get(name.lower())
        if want and rec.get("sha") and rec["sha"] != want:
            bad.append((name, rec["sha"][:10], want[:10]))
    return bad


def load_gold() -> tuple[dict, dict]:
    """{full_name_lower: count} and {full_name_lower: 'certified'|'human'}.

    corpus_results.jsonl holds one row per ATTEMPT (207 rows, 100 repos, 74 retried). Only the
    CERTIFIED rows carry a trustworthy manifest_size, and a repo's attempts are already resolved
    upstream via `selected_index`, so filtering on status is enough — but if a repo somehow has
    two CERTIFIED attempts with different sizes we must not pick one silently.
    """
    gold, tier, conflicts = {}, {}, []
    path = os.path.join(GT_DIR, "gt100", "corpus_results.jsonl")
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("status") != "CERTIFIED":
                continue
            name = repo_from_url(row["repo_url"]).lower()
            size = row.get("manifest_size")
            if not isinstance(size, int) or size <= 0:
                continue
            if name in gold and gold[name] != size:
                conflicts.append((name, gold[name], size))
            gold[name] = size
            tier[name] = "certified"

    hpath = os.path.join(GT_DIR, "human_manual_counts.json")
    with open(hpath, encoding="utf-8") as fh:
        human = json.load(fh)["repos"]
    for name, rec in human.items():
        key = name.lower()
        count = rec.get("human_collected")
        if not isinstance(count, int) or count <= 0:
            continue
        # A human count supersedes nothing: these repos were REJECTED by the harness, so they
        # should not already be present. Flag it rather than overwrite blindly.
        if key in gold:
            conflicts.append((key, gold[key], count))
        gold[key] = count
        tier[key] = "human"

    if conflicts:
        print("WARNING: conflicting gold counts (first wins was NOT assumed):", file=sys.stderr)
        for name, a, b in conflicts:
            print(f"  {name}: {a} vs {b}", file=sys.stderr)
    return gold, tier


def load_rows(run_dir: str) -> dict:
    """{full_name_lower: row} for one run, from measure/<arm>/<owner>/<repo>/row.json."""
    rows = {}
    measure = os.path.join(REPO_ROOT, run_dir, "measure")
    for dirpath, _dirnames, filenames in os.walk(measure):
        if "row.json" not in filenames:
            continue
        try:
            with open(os.path.join(dirpath, "row.json"), encoding="utf-8") as fh:
                row = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        name = (row.get("repo") or "").strip()
        if name:
            rows[name.lower()] = row
    return rows


def score(gold: dict, tier: dict, dataset: list, runs: list) -> dict:
    """Per-arm gold-anchored ESSR over the dataset's repos that have a gold count."""
    scored_names = [r["full_name"].lower() for r in dataset if r["full_name"].lower() in gold]
    missing = [r["full_name"] for r in dataset if r["full_name"].lower() not in gold]

    per_arm_rows = defaultdict(dict)
    for arm, run_dir in runs:
        for name, row in load_rows(run_dir).items():
            # Two runs per arm never overlap (envbench70 and minus70 are disjoint), so a
            # collision would mean the run list is wrong. Surface it rather than absorb it.
            if name in per_arm_rows[arm]:
                print(f"WARNING: {arm} has duplicate rows for {name}", file=sys.stderr)
            per_arm_rows[arm][name] = row

    out = {}
    for arm in sorted(per_arm_rows):
        rows = per_arm_rows[arm]
        per_repo, capped, zeroed, no_row = {}, [], 0, []
        for name in scored_names:
            row = rows.get(name)
            if row is None:
                no_row.append(name)
                per_repo[name] = 0.0        # never measured by this arm -> 0, stays in the mean
                zeroed += 1
                continue
            passed = row.get("passed") or 0
            raw = passed / gold[name]
            if raw > 1.0:
                capped.append((name, round(raw, 3), passed, gold[name]))
            p = min(raw, 1.0)
            per_repo[name] = p
            if p == 0.0:
                zeroed += 1
        vals = list(per_repo.values())
        out[arm] = {
            "n_scored": len(vals),
            "ESSR_gold": round(statistics.fmean(vals), 4) if vals else 0.0,
            "median": round(statistics.median(vals), 4) if vals else 0.0,
            "n_zero": zeroed,
            "n_no_row": len(no_row),
            "n_capped": len(capped),
            "capped": capped,
            "per_repo": per_repo,
        }
    return {"arms": out, "scored": scored_names, "missing_gold": missing, "tier": tier}


def economy(dataset: list, runs: list) -> dict:
    """Mean/median tokens and turns per arm, over the DATASET'S repos only.

    The scoping matters and is easy to get wrong: envbench70 (70) + minus70 (39) is 109 unique
    repos, but envbench100 is 100 — 9 repos live in the union WITHOUT being in envbench100. An
    unfiltered mean over each arm's run dirs would silently include those 9 and not be a number
    about envbench100 at all.

    Rows missing telemetry are EXCLUDED from the denominator rather than counted as zero (the
    same rule bench/bench/metrics.py applies to cost: a producer that reports nothing is not
    free). `n` is reported next to every mean so a thin denominator is never invisible — setupx
    carries telemetry on only 86 of 100.
    """
    names = {r["full_name"].lower() for r in dataset}
    per_arm = defaultdict(dict)
    for arm, run_dir in runs:
        for name, row in load_rows(run_dir).items():
            if name in names:                      # scope to the dataset, drop union-only repos
                per_arm[arm][name] = row

    fields = ("tokens_in", "tokens_out", "total_tokens", "turns_used", "llm_calls", "cost_usd")
    out = {}
    for arm in sorted(per_arm):
        rows = list(per_arm[arm].values())
        stats = {"n_rows": len(rows)}
        for f in fields:
            if f == "total_tokens":
                vals = [(r.get("tokens_in") or 0) + (r.get("tokens_out") or 0) for r in rows
                        if r.get("tokens_in") is not None and r.get("tokens_out") is not None]
            else:
                vals = [r[f] for r in rows if r.get(f) is not None]
            stats[f] = {
                "n": len(vals),
                "mean": round(statistics.fmean(vals), 2) if vals else None,
                "median": round(statistics.median(vals), 2) if vals else None,
                "total": round(sum(vals), 2) if vals else None,
            }
        out[arm] = stats
    return out


def ebsr(dataset: list, gold: dict, runs: list) -> dict:
    """EBSR over the dataset's repos, plus a gold-anchored hollow-pass diagnostic.

    EBSR is per-repo boolean (the env built AND `pytest --collect-only` exited clean), already
    decided by bench and stored as `row['ebsr']`; this only re-scopes the denominator to the
    dataset. Reported over all 100 AND over the 92 that carry a gold count, so it lines up with
    ESSR_gold without silently changing n between the two tables.

    The diagnostic is the part gold makes newly possible. `n_ebsr_zero_collected` in metrics.py
    can only catch the degenerate case of EBSR credit with ZERO tests collected. With a certified
    denominator we can measure the whole spectrum: collection_coverage = collected / gold, over
    the repos that took EBSR credit. A repo that built, collected clean, and found 12 of 450
    tests passes EBSR today and is indistinguishable from one that found all 450.
    """
    names = [r["full_name"].lower() for r in dataset]
    scored = [n for n in names if n in gold]
    per_arm = defaultdict(dict)
    for arm, run_dir in runs:
        for name, row in load_rows(run_dir).items():
            if name in set(names):
                per_arm[arm][name] = row

    def credited(row: dict | None) -> bool:
        """EBSR credit, replicating bench/bench/metrics.py:50 exactly.

        NOT `row['ebsr']`. That stored flag is LOOSER than the metric and disagrees badly: on the
        rat50 setupx run it is True for 38 of 50 rows while the published EBSR is 0.36 (18/50).
        The metric requires BOTH a non-disqualified status AND collect_clean; rows excluded from
        every denominator upstream (`unmeasurable`, `legacy_missing`) are dropped, not failed.
        """
        if row is None:
            return False
        if row.get("status") in ("unmeasurable", "legacy_missing"):
            return False
        return row.get("status") not in _DISQUALIFIED and bool(row.get("collect_clean"))

    out = {}
    for arm in sorted(per_arm):
        rows = per_arm[arm]
        flag = lambda n: credited(rows.get(n))                     # noqa: E731 - local alias
        n_all = sum(1 for n in names if flag(n))
        n_sc = sum(1 for n in scored if flag(n))
        # Collection coverage over EBSR-credited repos that have a gold denominator.
        cov, hollow = [], []
        for n in scored:
            if not flag(n):
                continue
            collected = len((rows.get(n) or {}).get("collected_node_ids") or [])
            c = min(collected / gold[n], 1.0)
            cov.append(c)
            if c < 0.5:
                hollow.append((n, collected, gold[n], round(c, 3)))
        out[arm] = {
            "EBSR_100": round(n_all / len(names), 4) if names else 0.0,
            "n_ebsr_100": n_all,
            "EBSR_92": round(n_sc / len(scored), 4) if scored else 0.0,
            "n_ebsr_92": n_sc,
            "mean_collection_coverage": round(statistics.fmean(cov), 4) if cov else None,
            "median_collection_coverage": round(statistics.median(cov), 4) if cov else None,
            "n_hollow_lt50pct": len(hollow),
            "hollow": sorted(hollow, key=lambda x: x[3]),
        }
    return out


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dataset", default="datasets/envbench_python100.json")
    ap.add_argument("--json-out", default=None, help="write the full per-repo breakdown here")
    args = ap.parse_args(argv)

    with open(os.path.join(REPO_ROOT, args.dataset), encoding="utf-8") as fh:
        dataset = json.load(fh)

    base = os.path.basename(args.dataset)
    if base.startswith("rat_python50"):
        gold, tier = load_gold_rat50()
        runs = RAT50_RUNS
        bad = check_shas(dataset, RAT50_GOLD)
        if bad:
            print(f"WARNING: {len(bad)} gold sha(s) differ from the dataset pin — a count taken at "
                  "a different commit is not ground truth for the pinned one:", file=sys.stderr)
            for name, got, want in bad[:10]:
                print(f"  {name}: gold {got} vs pinned {want}", file=sys.stderr)
    else:
        gold, tier = load_gold()
        runs = ENVBENCH100_RUNS
    res = score(gold, tier, dataset, runs)

    n_cert = sum(1 for n in res["scored"] if tier.get(n) == "certified")
    n_hum = sum(1 for n in res["scored"] if tier.get(n) == "human")
    print(f"dataset       {args.dataset}  ({len(dataset)} repos)")
    print(f"gold counts   {len(res['scored'])} scored  ({n_cert} certified + {n_hum} human)")
    print(f"excluded      {len(res['missing_gold'])} with no gold count")
    for name in sorted(res["missing_gold"]):
        print(f"                {name}")
    print()
    print(f"{'arm':<24} {'ESSR_gold':>10} {'median':>8} {'n':>4} {'zero':>5} {'no_row':>7} {'capped':>7}")
    for arm, m in res["arms"].items():
        print(f"{arm:<24} {m['ESSR_gold']:>10.4f} {m['median']:>8.4f} {m['n_scored']:>4} "
              f"{m['n_zero']:>5} {m['n_no_row']:>7} {m['n_capped']:>7}")
    for arm, m in res["arms"].items():
        if m["capped"]:
            print(f"\ncapped (passed > gold) in {arm}:")
            for name, raw, passed, g in m["capped"]:
                print(f"  {name}: {passed}/{g} = {raw}")

    eb = ebsr(dataset, gold, runs)
    print(f"\n--- EBSR over {os.path.basename(args.dataset)} ---")
    n_all_lbl, n_sc_lbl = f"EBSR({len(dataset)})", f"EBSR({len(res['scored'])})"
    print(f"{'arm':<24} {n_all_lbl:>10} {'n':>4} {n_sc_lbl:>9} {'n':>4} "
          f"{'collect_cov':>12} {'median':>8} {'hollow<50%':>11}")
    for arm, s in eb.items():
        print(f"{arm:<24} {s['EBSR_100']:>10.4f} {s['n_ebsr_100']:>4} "
              f"{s['EBSR_92']:>9.4f} {s['n_ebsr_92']:>4} "
              f"{s['mean_collection_coverage'] or 0:>12.4f} "
              f"{s['median_collection_coverage'] or 0:>8.4f} {s['n_hollow_lt50pct']:>11}")
    for arm, s in eb.items():
        if s["hollow"]:
            print(f"\n  {arm}: EBSR-credited but collected <50% of gold")
            for n, c, g, frac in s["hollow"][:8]:
                print(f"    {n:<48} {c:>6}/{g:<6} = {frac}")
    res["ebsr"] = eb

    eco = economy(dataset, runs)
    print(f"\n--- economy over the {len(dataset)} repos of {os.path.basename(args.dataset)} "
          f"(union-only repos excluded; n = rows carrying that field) ---")
    hdr = f"{'arm':<24} {'rows':>5} {'turns':>8} {'n':>4} {'llm_calls':>10} {'tok_in':>12} {'tok_out':>10} {'tok_tot':>12} {'n':>4}"
    print(hdr)
    for arm, s in eco.items():
        print(f"{arm:<24} {s['n_rows']:>5} "
              f"{s['turns_used']['mean'] or 0:>8.1f} {s['turns_used']['n']:>4} "
              f"{s['llm_calls']['mean'] or 0:>10.1f} "
              f"{s['tokens_in']['mean'] or 0:>12,.0f} {s['tokens_out']['mean'] or 0:>10,.0f} "
              f"{s['total_tokens']['mean'] or 0:>12,.0f} {s['total_tokens']['n']:>4}")
    print("\nmedians (the mean is skewed by a few very heavy repos):")
    for arm, s in eco.items():
        cost_mean = s["cost_usd"]["mean"]
        cost_txt = "n/a" if cost_mean is None else f"${cost_mean:.4f}"
        print(f"  {arm:<24} turns {s['turns_used']['median'] or 0:>6.1f}   "
              f"tok_tot {s['total_tokens']['median'] or 0:>12,.0f}   "
              f"mean cost {cost_txt}")
    res["economy"] = eco

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=2)
            fh.write("\n")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
