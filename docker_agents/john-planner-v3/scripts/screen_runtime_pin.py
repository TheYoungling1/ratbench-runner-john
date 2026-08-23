#!/usr/bin/env python3
"""Pre-screen (no Docker): how many repos would the runtime pin actually change?
If fewer than 5 show base_would_change, the v1gsp A/B cannot produce signal on
this set — do NOT spend an e2e run on it.

Usage: python3 scripts/screen_runtime_pin.py <dir-of-cloned-repos>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.envstate.runtime_base import screen_runtime_pin  # noqa: E402


def main(root: str) -> None:
    rows = []
    for name in sorted(os.listdir(root)):
        repo = os.path.join(root, name)
        if os.path.isdir(repo):
            rows.append((name, screen_runtime_pin(repo)))
    changed = sum(1 for _, r in rows if r["base_would_change"])
    for name, r in rows:
        print(f"{name:40} requires={str(r['requires_python']):22} "
              f"-> {r['would_pin_to']:6} change={r['base_would_change']}")
    verdict = "OK to A/B" if changed >= 5 else "TOO FEW — A/B cannot produce signal; do not e2e"
    print(f"\n{changed}/{len(rows)} repos would get a different base. {verdict}")


if __name__ == "__main__":
    main(sys.argv[1])
