"""CLI for the e2e build-script eval.

  python3 -m src.eval.build_script_eval --fetch [--only a,b] [--stratum S_syslib]
  python3 -m src.eval.build_script_eval --run   [--only ...] [--stratum ...]
  python3 -m src.eval.build_script_eval --score            # re-aggregate existing scorecards
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.eval.build_script_eval.corpus import select  # noqa: E402
from src.eval.build_script_eval.fetch import fetch_repo, smoke_root  # noqa: E402
from src.eval.build_script_eval.report import aggregate, render_report_md  # noqa: E402
from src.eval.build_script_eval.scorecard import score_repo  # noqa: E402

_OUT = _REPO_ROOT / "outputs" / "build_script_eval"


def _csv(s: str) -> frozenset[str]:
    return frozenset(tok.strip() for tok in (s or "").split(",") if tok.strip())


def _repo_id(full_name: str) -> str:
    return full_name.replace("/", "__")


def main() -> int:
    ap = argparse.ArgumentParser(prog="build_script_eval")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--score", action="store_true", help="re-aggregate existing scorecards")
    ap.add_argument("--only", default="", help="comma-sep repo names")
    ap.add_argument("--stratum", default="", help="comma-sep strata (S_control,S_syslib)")
    args = ap.parse_args()

    specs = select(only=_csv(args.only), strata=_csv(args.stratum))
    _OUT.mkdir(parents=True, exist_ok=True)
    root = smoke_root()

    if args.fetch:
        fetched = 0
        for spec in specs:
            try:
                fetch_repo(spec, smoke_root=root)
                fetched += 1
            except Exception as e:  # noqa: BLE001 — one repo must not abort the corpus
                print(f"SKIP-FETCH {spec.name}: {e}")
                continue
        print(f"fetched {fetched} repos into {root}")

    if args.run:
        print(f"scoring {len(specs)} repos (selected)")
        for spec in specs:
            repo_dir = root / spec.name
            if not repo_dir.exists():
                print(f"SKIP {spec.name}: not fetched (run --fetch first)")
                continue
            try:
                card = score_repo(str(repo_dir), spec)
            except Exception as exc:  # noqa: BLE001 — one repo must not abort the corpus
                card = {"repo": spec.full_name, "stratum": spec.stratum, "feasible": spec.feasible,
                        "first_pass_env_works": False, "attribution": "unknown",
                        "execution_missing": [], "predicted_apt": [],
                        "error": f"{type(exc).__name__}: {exc}"}
            (_OUT / f"{_repo_id(spec.full_name)}.json").write_text(
                json.dumps(card, indent=2, sort_keys=True) + "\n")
            print(f"  {spec.name}: env_works={card.get('first_pass_env_works')} "
                  f"attribution={card.get('attribution')} rung={card.get('highest_rung')}")

    if args.run or args.score:
        cards = [json.loads(p.read_text()) for p in sorted(_OUT.glob("*__*.json"))]
        agg = aggregate(cards)
        (_OUT / "report.md").write_text(render_report_md(agg))
        print(json.dumps(agg["headline_env_works"], indent=2))
        print(f"wrote {_OUT / 'report.md'}")

    if not (args.fetch or args.run or args.score):
        ap.error("nothing to do: pass --fetch, --run, and/or --score")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
