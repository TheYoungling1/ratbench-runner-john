#!/usr/bin/env python3
"""Node package-fidelity comparer — OURS (filtered lockfile closure) vs the
ORACLE (agent-configured ``npm ci`` installed tree). The LOCK-mode mirror of
``compare_pkg.py``.

recall    = |ours ∩ installed| / |installed|   (under-coverage; expect ~1.0 in LOCK mode)
precision = |ours ∩ installed| / |ours|         (over-install; the number this eval moves)
Names: npm-canonical (keep @scope, NO PEP 503 '_'/'.'→'-' collapse). Own package excluded.
A residual ``extra`` that carries an os/cpu/libc constraint is a filter escape and is
bucketed as ``platform_optional_extra`` so it never hides in the headline (§8).
"""
from __future__ import annotations

import json
import pathlib
import sys


def canon(name: str) -> str:
    """npm names are registry-lowercased and scope-preserving; '-', '_', '.' are
    DISTINCT (unlike PEP 503). Canon = strip only, keep the '@scope/' prefix."""
    return name.strip()


def mm(v: str | None) -> str | None:
    if not v:
        return None
    parts = str(v).split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else parts[0]


def score_repo(ours_rec: dict, oracle_rec: dict) -> dict:
    """Pure per-repo scorecard from an OURS record and an ORACLE record."""
    own = {canon(n) for n in (ours_rec.get("project"), oracle_rec.get("repo")) if n}
    ours = {canon(n): v for n, v in (ours_rec.get("packages") or {}).items() if canon(n) not in own}
    inst = {canon(n): v for n, v in (oracle_rec.get("installed") or {}).items() if canon(n) not in own}
    O, I = set(ours), set(inst)
    inter = O & I
    missing = sorted(I - O)          # installed, ours lacks -> under-coverage (bug signal)
    extra = sorted(O - I)            # ours has, not installed -> over-install
    constraints = ours_rec.get("constraints") or {}
    plat_extra = sorted(e for e in extra if constraints.get(e))  # filter escape
    plain_extra = sorted(e for e in extra if not constraints.get(e))
    recall = len(inter) / len(I) if I else None
    precision = len(inter) / len(O) if O else None
    vexact = sum(1 for k in inter if ours[k] and inst[k] and str(ours[k]) == str(inst[k]))
    vmm = sum(1 for k in inter if mm(ours[k]) and mm(inst[k]) and mm(ours[k]) == mm(inst[k]))
    return {
        "ours_n": len(O), "installed_n": len(I), "inter": len(inter),
        "recall": recall, "precision": precision, "vexact": vexact, "vmm": vmm,
        "missing": missing, "extra": plain_extra, "platform_optional_extra": plat_extra,
        # unfiltered diagnostic (the avoided precision hit — the axios number)
        "unfiltered_n": ours_rec.get("unfiltered_count"),
        "platform_dropped_n": ours_rec.get("platform_dropped_count"),
    }


def main() -> int:
    ours_dir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path("/tmp/ours_node")
    oracle_dir = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else pathlib.Path("/tmp/oracle_node")
    repos = sorted(p.stem for p in ours_dir.glob("*.json"))
    rn = rd = pn = pd = 0
    rows = []
    for repo in repos:
        ours = json.loads((ours_dir / f"{repo}.json").read_text())
        orc_p = oracle_dir / f"{repo}.json"
        if ours.get("error"):
            rows.append((repo, f"OURS-ERROR: {ours['error'][:60]}", None)); continue
        if not orc_p.exists():
            rows.append((repo, "oracle-missing", None)); continue
        s = score_repo(ours, json.loads(orc_p.read_text()))
        rows.append((repo, "ok", s))
        if s["installed_n"]:
            rn += s["inter"]; rd += s["installed_n"]
        if s["ours_n"]:
            pn += s["inter"]; pd += s["ours_n"]

    print("=" * 100)
    print("NODE PACKAGE-LAYER FIDELITY — filtered lockfile closure vs npm-ci installed tree")
    print("=" * 100)
    print(f"{'repo':<22}{'ours':<6}{'inst':<6}{'∩':<5}{'recall':<8}{'prec':<7}{'unfilt':<8}{'dropped':<8}")
    for repo, status, s in rows:
        if status != "ok":
            print(f"{repo:<22}{status}"); continue
        rc = f"{s['recall']:.2f}" if s["recall"] is not None else "n/a"
        pr = f"{s['precision']:.2f}" if s["precision"] is not None else "n/a"
        print(f"{repo:<22}{s['ours_n']:<6}{s['installed_n']:<6}{s['inter']:<5}{rc:<8}{pr:<7}"
              f"{str(s['unfiltered_n']):<8}{str(s['platform_dropped_n']):<8}")
    if rd and pd:
        print("-" * 100)
        print(f"POOLED recall = {rn/rd:.3f} ({rn}/{rd})   precision = {pn/pd:.3f} ({pn}/{pd})")
    print("\nDIVERGENCES (missing = under-coverage BUG signal; platform_optional_extra = filter escape)")
    for repo, status, s in rows:
        if status != "ok" or not (s["missing"] or s["extra"] or s["platform_optional_extra"]):
            continue
        rc = f"{s['recall']:.2f}" if s["recall"] is not None else "n/a"
        pr = f"{s['precision']:.2f}" if s["precision"] is not None else "n/a"
        print(f"\n## {repo} (recall {rc} / prec {pr})")
        if s["missing"]:
            print(f"   MISSING (installed, ours lacks): {s['missing']}")
        if s["extra"]:
            print(f"   EXTRA   (ours, not installed):   {s['extra']}")
        if s["platform_optional_extra"]:
            print(f"   PLATFORM_OPTIONAL_EXTRA (filter escape!): {s['platform_optional_extra']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
