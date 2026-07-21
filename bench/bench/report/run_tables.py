#!/usr/bin/env python3
"""Emit per_repo_table.json + in_sandbox_score.json for a benchmark run dir.

These two rollups are NOT produced by a model's predict() — they are derived, post-hoc, from the
single metric authority (``compute_essr.score_agent``, which recomputes every eval number from raw
``run_pytest_results.json``) plus each repo's in-sandbox / token telemetry in agent_run_summary.json.

The runner persists that summary into each ``output/<repo>/`` (see _persist_agent_summary) and calls
``write()`` from aggregate(), so every run ships these files. We read the PERSISTED per-run copy
first; the shared workplace is only a fallback because the next run of a repo overwrites it.

Schema (matches the hand-made baselines):
  per_repo_table.json  : [ {full, ib, cr, tok} ]   ib=in-build pass-rate (real run, not collect-only)
                                                    cr=eval pass-rate if executed else null
                                                    tok=total agent-loop tokens
  in_sandbox_score.json: { <run_name>: { root, n, executed, coverage, in_build_success,
                                         in_build_success_rate, essr_div_exec, essr_div_all,
                                         micro_pooled, full_pass, in_build_success_repos,
                                         partial_repos, not_executed_repos, rows } }

Usage:
  emit_run_tables.py <run_dir> [<run_dir> ...]   # write both files into each run_dir
  emit_run_tables.py --check <run_dir>           # compare against existing files, write nothing
"""
import json
import os
import sys

from bench import inline_score as compute_essr

_COLLECT = "--collect-only"


def _summary(run_dir, full_name):
    """agent_run_summary.json for a repo — ONLY the run's own persisted copy.

    We deliberately do NOT fall back to the shared workplace/ dir: it is keyed only by repo, so a
    PRIOR run's summary (e.g. a DockerAgent run before a later RAT run) sits at the same path and
    would contaminate this run's tokens / in-build metrics. The runner's _persist_agent_summary
    copies only THIS run's fresh (mtime-guarded) summary into output/<repo>/, so the persisted copy
    is the single authoritative source; its absence means there is no trustworthy summary for this
    run (-> {} -> null ib/tok), which is the honest state rather than a stale neighbour's number."""
    p = os.path.join(run_dir, "output", full_name, "agent_run_summary.json")
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {}


def _in_build(summ):
    """(success, pass_rate_or_None, command) — a REAL in-sandbox pass, not a collect-only check."""
    best = summ.get("best_in_sandbox_test_result") or {}
    cmd = best.get("command") or ""
    success = bool(best.get("success")) and _COLLECT not in cmd
    return success, (best.get("pass_rate") if success else None), cmd


def _tokens(summ):
    tu = summ.get("token_usage") or {}
    tot = tu.get("total")
    return tot.get("total_tokens") if isinstance(tot, dict) else tu.get("total_tokens")


def build(run_dir):
    """Return (per_repo_table_list, {run_name: in_sandbox_score_aggregate})."""
    res = compute_essr.score_agent(run_dir)
    rows = res["rows"]
    per_repo, iss_rows, ib_repos, partial, not_exec = [], [], [], [], []
    n_ib = 0
    for r in rows:
        fn = r["full_name"]
        ex = bool(r.get("executed"))
        pr = r.get("pass_rate")
        summ = _summary(run_dir, fn)
        ib_ok, ib, cmd = _in_build(summ)
        per_repo.append({"full": fn, "ib": ib, "cr": (pr if ex else None), "tok": _tokens(summ)})
        row = {"full_name": fn, "executed": ex, "pass_rate": pr or 0.0,
               "passed": r.get("passed") or 0, "eff_total": r.get("eff_total") or 0,
               "in_build_success": ib_ok}
        if cmd:
            row["command"] = cmd
        iss_rows.append(row)
        if ib_ok:
            n_ib += 1
            ib_repos.append(fn)
        if ex and (pr or 0) < 0.8:
            partial.append([fn, round(pr or 0.0, 4)])
        if not ex:
            not_exec.append(fn)
    n, nexec = res["n"], res["n_exec"]
    full_pass = sum(1 for r in rows if (r.get("pass_rate") or 0) >= 1.0)
    agg = {
        "root": os.path.abspath(run_dir), "n": n, "executed": nexec,
        "coverage": round(nexec / n, 4) if n else 0.0,
        "in_build_success": n_ib, "in_build_success_rate": round(n_ib / n, 4) if n else 0.0,
        "essr_div_exec": round(res["ESSR_avg_pass_rate_official"], 4),
        "essr_div_all": round(res["pass_rate_over_all"], 4),
        "micro_pooled": round(res["micro_pooled"], 4),
        "full_pass": full_pass, "in_build_success_repos": ib_repos,
        "partial_repos": partial, "not_executed_repos": not_exec, "rows": iss_rows,
    }
    return per_repo, {os.path.basename(run_dir.rstrip("/")): agg}


def write(run_dir):
    """Write per_repo_table.json + in_sandbox_score.json into run_dir; return their paths."""
    per_repo, iss = build(run_dir)
    pt = os.path.join(run_dir, "per_repo_table.json")
    isf = os.path.join(run_dir, "in_sandbox_score.json")
    with open(pt, "w") as f:
        json.dump(per_repo, f, indent=1)
    with open(isf, "w") as f:
        json.dump(iss, f, indent=2)
    return pt, isf


def _check(run_dir):
    """Compare freshly-built tables against any existing files (debugging; writes nothing)."""
    per_repo, iss = build(run_dir)
    agg = list(iss.values())[0]
    old_iss = list(json.load(open(os.path.join(run_dir, "in_sandbox_score.json"))).values())[0]
    old_pt = {r["full"]: r for r in json.load(open(os.path.join(run_dir, "per_repo_table.json")))}
    for k in ("n", "executed", "coverage", "in_build_success", "essr_div_exec", "essr_div_all",
              "micro_pooled", "full_pass"):
        mark = "OK " if agg.get(k) == old_iss.get(k) else "XX "
        print(f"  {mark}{k}: {agg.get(k)}  vs  {old_iss.get(k)}")
    mis = sum(1 for r in per_repo if r != {**r, **old_pt.get(r["full"], r)})
    print(f"=== per_repo mismatches: {mis}/{len(per_repo)} ===")


if __name__ == "__main__":
    _args = sys.argv[1:]
    if _args and _args[0] == "--check":
        _check(_args[1])
    elif _args:
        for _d in _args:
            print("wrote:", write(_d))
    else:
        print(__doc__)
