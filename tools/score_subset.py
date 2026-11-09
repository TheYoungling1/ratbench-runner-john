#!/usr/bin/env python3
"""Score one arm over an arbitrary SUBSET of the campaign's repos.

campaign.py scores whole datasets — it filters each group to its canonical dataset file. An
ablation asks a different question: take N repos drawn from across those datasets and score them
as one pile, with the same pooled definition the paper uses. That is this script.

The subset's repos are matched against every run dir the arm has, so a repo drawn from rat50new is
scored from the rat50new run and one drawn from envbench100 from the envbench run, without the
caller naming any directory.

  python tools/score_subset.py --agent-dir runs/<arm> --gold datasets/gold_ablation40.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bench"))

from bench.campaign import discover_runs, economy, select_runs          # noqa: E402
from bench.metrics import compute_metrics, load_gold_counts             # noqa: E402
from bench.unified_bench import load_rows                               # noqa: E402


def subset_names(gold_doc: dict) -> set:
    """Every repo the gold file covers, plus the ones it explicitly records as having NO gold.

    `repos_without_gold` matters: a repo with no certified denominator still has an EBSR, so
    dropping it here would silently shrink the subset and the EBSR denominator with it.
    """
    names = set()
    for blk in (gold_doc.get("corpora") or {}).values():
        for rec in (blk.get("repos") or {}).values():
            if rec.get("full_name"):
                names.add(rec["full_name"].lower())
    for n in (gold_doc.get("totals") or {}).get("repos_without_gold") or []:
        names.add(n.lower())
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-dir", required=True)
    ap.add_argument("--gold", required=True)
    ap.add_argument("--label", default="subset")
    a = ap.parse_args()

    with open(a.gold, encoding="utf-8") as fh:
        gold_doc = json.load(fh)
    want = subset_names(gold_doc)
    gold = load_gold_counts(a.gold)                     # corpus=None -> all corpora merged

    # Only the run dirs campaign.py would have chosen: never a smoke, never a superseded duplicate.
    sel = select_runs(discover_runs(a.agent_dir))
    chosen = [p for g, s in sel.items() if g != "ignored" for p in s.paths]

    found, origin, dupes = {}, {}, []
    for p in chosen:
        for rows in load_rows(os.path.join(p, "measure")).values():
            for r in rows:
                k = r.repo.lower()
                if k not in want:
                    continue
                if k in found:
                    dupes.append(r.repo)
                    continue
                found[k], origin[k] = r, os.path.basename(p)

    rows = list(found.values())
    m = compute_metrics(rows, gold=gold or None)
    missing = sorted(want - set(found))

    print(f"agent       {os.path.basename(os.path.abspath(a.agent_dir.rstrip('/')))}")
    print(f"subset      {a.label}: {len(want)} repos, {len(rows)} with a measured row")
    print(f"gold        {len(gold)} denominators from {os.path.basename(a.gold)}")
    by_src: dict = {}
    for k in found:
        by_src.setdefault(origin[k], []).append(k)
    for src, ks in sorted(by_src.items()):
        print(f"   from {src}: {len(ks)}")
    if missing:
        print(f"   !! {len(missing)} subset repo(s) have NO row: {', '.join(missing)}")
    if dupes:
        print(f"   !! {len(dupes)} repo(s) in more than one run dir: {', '.join(dupes[:4])}")

    print(f"\n  POOLED over the {a.label}")
    print(f"    EBSR        {m['EBSR']:.4f}   ({m['n_ebsr']}/{m['n']})")
    essr = "null" if m["ESSR"] is None else f"{m['ESSR']:.4f}"
    print(f"    ESSR        {essr}   (over the {m['n_gold_scored']} repos with a gold manifest)")
    e = economy(rows)
    if e["n_token_reporting"]:
        print(f"    tokens      {e['median_tokens']/1000:,.1f}K median over "
              f"{e['n_token_reporting']}/{e['n_rows']} rows   turns {e['median_turns']:.0f}"
              f"   ${e['median_cost_usd']:.4f}" if e["median_cost_usd"] is not None else "")
    print(f"    census      {m['status_census']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
