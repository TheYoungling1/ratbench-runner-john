#!/usr/bin/env python3
"""Compare OUR Phase-1 PACKAGE closure vs the agent-configured working `pip freeze`
(ground truth), per repo. Python package layer only.

recall    = |ours ∩ oracle| / |oracle|   (of the working env's deps, how many did we capture; MISS = under-coverage)
precision = |ours ∩ oracle| / |ours|     (of our closure, how many the working env actually has; EXTRA = over-install)
Version agreement (exact + major.minor) on the intersection. Names PEP503-canonicalized.
The project's OWN dist is excluded from both sides (our PACKAGE tier never includes the project).
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

OURS = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path("/tmp/ours")
ORACLE = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else pathlib.Path("/tmp/oracle")
SMOKE = pathlib.Path("outputs/graph_fidelity/_smoke")


def canon(n: str) -> str:
    try:
        return canonicalize_name(n)
    except Exception:
        return n.strip().lower().replace("_", "-").replace(".", "-")


def project_names(repo: str) -> set[str]:
    """Canon names to treat as the project itself (excluded from both sides)."""
    out = {canon(repo)}
    d = SMOKE / repo
    pp = d / "pyproject.toml"
    if pp.exists():
        try:
            data = tomllib.loads(pp.read_text())
            nm = (data.get("project") or {}).get("name")
            if nm:
                out.add(canon(nm))
            # poetry
            nm2 = ((data.get("tool") or {}).get("poetry") or {}).get("name")
            if nm2:
                out.add(canon(nm2))
        except Exception:
            pass
    return out


def mm(v: str | None) -> str | None:
    if not v:
        return None
    parts = str(v).split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else parts[0]


def main() -> int:
    repos = sorted(p.stem for p in OURS.glob("*.json"))
    rows = []
    tot_recall_num = tot_recall_den = tot_prec_num = tot_prec_den = 0
    for repo in repos:
        o = json.loads((OURS / f"{repo}.json").read_text())
        orc_p = ORACLE / f"{repo}.json"
        if o.get("error"):
            rows.append({"repo": repo, "status": f"OURS-ERROR: {o['error'][:60]}"})
            continue
        if not orc_p.exists():
            rows.append({"repo": repo, "status": "oracle-missing (agent not done/failed)"})
            continue
        orc = json.loads(orc_p.read_text())
        proj = project_names(repo)
        ours_pkgs = {canon(n): v for n, v in (o.get("packages") or {}).items() if canon(n) not in proj}
        orc_freeze = orc.get("pip_freeze") or {}
        orc_pkgs = {canon(n): v for n, v in orc_freeze.items() if canon(n) not in proj}
        O, R = set(ours_pkgs), set(orc_pkgs)
        inter = O & R
        missing = sorted(R - O)   # oracle has, we DON'T (under-coverage)
        extra = sorted(O - R)     # we have, oracle DOESN'T (over-install)
        recall = len(inter) / len(R) if R else None
        precision = len(inter) / len(O) if O else None
        vexact = sum(1 for k in inter if ours_pkgs[k] and orc_pkgs[k] and str(ours_pkgs[k]) == str(orc_pkgs[k]))
        vmm = sum(1 for k in inter if mm(ours_pkgs[k]) and mm(orc_pkgs[k]) and mm(ours_pkgs[k]) == mm(orc_pkgs[k]))
        if R:
            tot_recall_num += len(inter); tot_recall_den += len(R)
        if O:
            tot_prec_num += len(inter); tot_prec_den += len(O)
        rows.append({
            "repo": repo, "status": "ok",
            "gate": orc.get("gate_passed"),
            "ours_n": len(O), "oracle_n": len(R), "inter": len(inter),
            "recall": recall, "precision": precision,
            "vexact": vexact, "vmm": vmm,
            "missing": missing, "extra": extra,
            "ours_unresolved": o.get("unresolved_imports", []),
            "ours_audit": o.get("audit_repaired", []),
        })

    print("=" * 100)
    print("PACKAGE-LAYER FIDELITY — our Phase-1 closure vs agent-configured working `pip freeze`")
    print("=" * 100)
    print(f"{'repo':<26}{'gate':<6}{'ours':<6}{'orcl':<6}{'∩':<5}{'recall':<8}{'prec':<7}{'v=':<5}{'v~':<5}")
    for r in rows:
        if r["status"] != "ok":
            print(f"{r['repo']:<26}{r['status']}"); continue
        rc = f"{r['recall']:.2f}" if r['recall'] is not None else "n/a"
        pr = f"{r['precision']:.2f}" if r['precision'] is not None else "n/a"
        print(f"{r['repo']:<26}{str(r['gate']):<6}{r['ours_n']:<6}{r['oracle_n']:<6}{r['inter']:<5}{rc:<8}{pr:<7}{r['vexact']:<5}{r['vmm']:<5}")
    if tot_recall_den and tot_prec_den:
        print("-" * 100)
        print(f"POOLED recall = {tot_recall_num/tot_recall_den:.3f}  ({tot_recall_num}/{tot_recall_den})   "
              f"POOLED precision = {tot_prec_num/tot_prec_den:.3f}  ({tot_prec_num}/{tot_prec_den})")
    print("\n" + "=" * 100)
    print("DIVERGENCES (missing = under-coverage we should worry about; extra = over-install)")
    print("=" * 100)
    for r in rows:
        if r["status"] != "ok":
            continue
        if r["missing"] or r["extra"]:
            print(f"\n## {r['repo']}  (recall {r['recall']:.2f} / prec {r['precision']:.2f})")
            if r["missing"]:
                print(f"   MISSING (oracle has, ours lacks): {r['missing']}")
            if r["extra"]:
                print(f"   EXTRA   (ours has, oracle lacks): {r['extra']}")
            if r["ours_unresolved"]:
                print(f"   (ours honestly flagged unresolved: {r['ours_unresolved']})")
            if r["ours_audit"]:
                print(f"   (ours AUDIT-repaired: {r['ours_audit']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
