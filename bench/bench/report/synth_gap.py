#!/usr/bin/env python3
"""Classify every repo in a RAT run into A/B/C/D failure-attribution buckets.

Pinpoints SYNTHESIZER failures (bucket C): repos where the agent built AND verified real
tests in its own sandbox, but the eval replay did NOT reproduce a runnable env — i.e. the
synthesizer failed to turn a working environment into a reusable Dockerfile.

Reuses compute_essr.score_agent (the metric authority) for eval signals + attribution.

Usage (pass the run ROOT that contains output/, NOT output/ itself):
  python -m bench.report.synth_gap /opt/runs/radical/<run>
  python -m bench.report.synth_gap radical=/opt/runs/radical/<run> v1=/opt/runs/john-planner-v1/<run>
"""
from __future__ import annotations

import os
import sys

from bench.inline_score import score_agent
from bench.attribution import ALL_BUCKETS, LEGEND


def _print_one(name: str, root: str) -> dict:
    r = score_agent(root)
    att = r["attribution"]
    c = att["counts"]
    n = att["total"]
    print(f"\n=== {name}  ({root}) ===")
    print(f"  n={n}  coverage={r['coverage']}  EBSR={r['EBSR_build_execute']}  "
          f"ESSR_div_exec={r['ESSR_avg_pass_rate_official']}")
    for b in ALL_BUCKETS:
        pct = (100.0 * c[b] / n) if n else 0.0
        print(f"  {b:<32} {c[b]:>3}  ({pct:4.0f}%)   {LEGEND[b]}")
    if att["agent_signal_missing"]:
        print(f"  (agent_signal_missing on {att['agent_signal_missing']} repo(s) — D if reproduced, else U/unattributable)")

    by_name = {row["full_name"]: row for row in r["rows"]}
    if att["synthesizer_failures"]:
        print("\n  C — SYNTHESIZER FAILURES (agent had a verified-working env; eval did not reproduce it):")
        for fn in att["synthesizer_failures"]:
            row = by_name.get(fn, {})
            print(f"    - {fn}")
            print(f"        verified in-sandbox: {row.get('verified_test_command')!r}  "
                  f"(source={row.get('verification_source')})")
            print(f"        eval replay: executed={row.get('executed')} parse_method={row.get('parse_method')}")
            dropped = row.get("dropped_commands") or []
            if dropped:
                print(f"        synthesizer dropped: {dropped}")
    if att["weak_verification_reproduced"]:
        print("\n  D* — reproduced but agent only collect-only'd in-sandbox (hollow-ish): "
              + ", ".join(str(x) for x in att["weak_verification_reproduced"][:20]))
    return r


def main(argv) -> int:
    args = argv[1:]
    if not args:
        print(__doc__)
        return 1
    for a in args:
        if "=" in a:
            name, path = a.split("=", 1)
        else:
            name, path = os.path.basename(os.path.normpath(a)) or a, a
        _print_one(name, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
