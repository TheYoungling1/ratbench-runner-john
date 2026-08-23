#!/usr/bin/env python3
"""RUN-oracle upgrade headline: does construction recall hold up when the oracle gates on
RUNNING pytest (freeze = env the tests execute in) instead of just --collect-only?

For each repo we have THREE package sets (all PEP503-canonicalized, project's own dist excluded):
  OURS     = construction-only PACKAGE closure  (outputs/.../ours_*)
  COLLECT  = collect-oracle pip_freeze          (import + pytest --collect-only green)
  RUN      = run-oracle    pip_freeze           (pytest actually RUNS green)

Per repo we report:
  recall_c  = |ours ∩ collect| / |collect|     (recall vs the CHEAP proxy)
  recall_r  = |ours ∩ run|     / |run|         (recall vs the TIGHT run-coverage ground truth)
  Δrecall   = recall_r - recall_c              (negative = the run pulled deps ours misses)
  run_only          = RUN - COLLECT            (deps that only appear once tests EXECUTE)
  run_only_MISSED   = run_only - OURS          (the REAL gap: run-time deps ours never captured)
RUN ⊇ COLLECT should hold (you must collect before you run); run_only is the run-time-only surface,
run_only_MISSED is what the cheap collect-oracle was hiding.
"""
import json
import pathlib
import sys

sys.path.insert(0, "src")
try:
    import tomllib
except ModuleNotFoundError:  # <3.11
    import tomli as tomllib  # type: ignore
from packaging.utils import canonicalize_name

GF = pathlib.Path("outputs/graph_fidelity")
OURS_DIRS = [GF / "ours_large", GF / "ours_services"]
COLLECT_DIRS = [GF / "oracle_large", GF / "oracle_services"]
RUN_DIRS = [GF / "oracle_run_large", GF / "oracle_run_services"]
SMOKE_ROOTS = [GF / "_smoke", GF / "_smoke_large", GF / "_smoke_services"]


def canon(n: str) -> str:
    try:
        return canonicalize_name(n)
    except Exception:
        return n.strip().lower().replace("_", "-").replace(".", "-")


def project_names(repo: str) -> set[str]:
    out = {canon(repo)}
    for root in SMOKE_ROOTS:
        pp = root / repo / "pyproject.toml"
        if not pp.exists():
            continue
        try:
            data = tomllib.loads(pp.read_text())
            for path in (("project", "name"), ("tool", "poetry", "name")):
                d = data
                for k in path:
                    d = (d or {}).get(k) if isinstance(d, dict) else None
                if d:
                    out.add(canon(d))
        except Exception:
            pass
    return out


def find(dirs, repo: str):
    for d in dirs:
        p = d / f"{repo}.json"
        if p.exists():
            return json.loads(p.read_text())
    return None


def pkgset(obj, key, proj):
    """key='packages' for ours, 'pip_freeze' for oracles."""
    raw = (obj or {}).get(key) or {}
    return {canon(n): v for n, v in raw.items() if canon(n) not in proj}


def rc(num, den):
    return num / den if den else None


def fmt(x):
    return f"{x:.2f}" if x is not None else "n/a"


def main() -> int:
    repos = sorted({p.stem for d in RUN_DIRS for p in d.glob("*.json")})
    rows = []
    pooled = {"c_num": 0, "c_den": 0, "r_num": 0, "r_den": 0,
              "run_only": 0, "run_only_missed": 0}
    for repo in repos:
        ours = find(OURS_DIRS, repo)
        collect = find(COLLECT_DIRS, repo)
        run = find(RUN_DIRS, repo)
        if not (ours and collect and run):
            rows.append({"repo": repo, "status": "missing one of ours/collect/run"})
            continue
        proj = project_names(repo)
        O = set(pkgset(ours, "packages", proj))
        C = set(pkgset(collect, "pip_freeze", proj))
        R = set(pkgset(run, "pip_freeze", proj))
        recall_c = rc(len(O & C), len(C))
        recall_r = rc(len(O & R), len(R))
        run_only = R - C
        run_only_missed = run_only - O
        pooled["c_num"] += len(O & C); pooled["c_den"] += len(C)
        pooled["r_num"] += len(O & R); pooled["r_den"] += len(R)
        pooled["run_only"] += len(run_only)
        pooled["run_only_missed"] += len(run_only_missed)
        rows.append({
            "repo": repo, "status": "ok",
            "collect_n": len(C), "run_n": len(R),
            "recall_c": recall_c, "recall_r": recall_r,
            "d_recall": (recall_r - recall_c) if (recall_c is not None and recall_r is not None) else None,
            "run_only": sorted(run_only),
            "run_only_missed": sorted(run_only_missed),
            "run_added_deps": run.get("run_added_deps", []),
            "run_pass_rate": run.get("run_pass_rate"),
            "run_ran": run.get("run_ran"),
        })

    print("=" * 104)
    print("RUN-ORACLE UPGRADE — construction recall vs COLLECT proxy vs RUN ground truth")
    print("=" * 104)
    print(f"{'repo':<16}{'coll_n':<8}{'run_n':<7}{'r_only':<8}{'recall_c':<10}{'recall_r':<10}{'Δrecall':<10}{'missed':<7}{'pass%':<6}")
    for r in rows:
        if r["status"] != "ok":
            print(f"{r['repo']:<16}{r['status']}"); continue
        dr = f"{r['d_recall']:+.2f}" if r["d_recall"] is not None else "n/a"
        pr = f"{r['run_pass_rate']:.2f}" if isinstance(r["run_pass_rate"], (int, float)) else "n/a"
        print(f"{r['repo']:<16}{r['collect_n']:<8}{r['run_n']:<7}{len(r['run_only']):<8}"
              f"{fmt(r['recall_c']):<10}{fmt(r['recall_r']):<10}{dr:<10}{len(r['run_only_missed']):<7}{pr:<6}")
    if pooled["c_den"] and pooled["r_den"]:
        rc_c = pooled["c_num"] / pooled["c_den"]
        rc_r = pooled["r_num"] / pooled["r_den"]
        print("-" * 104)
        print(f"POOLED recall_c = {rc_c:.3f} ({pooled['c_num']}/{pooled['c_den']})   "
              f"recall_r = {rc_r:.3f} ({pooled['r_num']}/{pooled['r_den']})   "
              f"Δ = {rc_r - rc_c:+.3f}")
        print(f"POOLED run-time-only deps = {pooled['run_only']}   of which ours MISSED = "
              f"{pooled['run_only_missed']}  "
              f"({'0 — recall was a sound proxy' if pooled['run_only_missed'] == 0 else 'the real under-coverage'})")
    print("\n" + "=" * 104)
    print("PER-REPO run-time-only surface (RUN minus COLLECT) — MISSED = ours never captured it")
    print("=" * 104)
    for r in rows:
        if r["status"] != "ok" or not r["run_only"]:
            continue
        print(f"\n## {r['repo']}  (recall_c {fmt(r['recall_c'])} -> recall_r {fmt(r['recall_r'])})")
        print(f"   run-time-only (RUN\\COLLECT): {r['run_only']}")
        if r["run_only_missed"]:
            print(f"   >> MISSED by ours: {r['run_only_missed']}")
        if r["run_added_deps"]:
            print(f"   (agent-declared run_added_deps: {r['run_added_deps']})")
    print("\n(repos with empty run-time-only surface: run needed nothing beyond collect — recall was exact there)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
