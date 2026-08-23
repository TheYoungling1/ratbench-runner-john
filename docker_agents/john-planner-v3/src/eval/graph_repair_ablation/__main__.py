"""CLI for the graph-repair-ablation pilot (Task 6).

  python3 -m src.eval.graph_repair_ablation --run [--only id,..] [--class NAME,..]
                                             [--arms C0,C1] [--model <slug>]

Builds ONE live OpenAI-compatible client for the whole batch -- mirrors
`scripts/run_v3_e2e.py::_run()`'s env-var fallback chain and default model
exactly (recon fact 4) -- then runs `run_one` for every selected-injection x
selected-arm cell, aggregates, and writes
`outputs/graph_repair_ablation/{results.json,report.md}`.

NOT run in CI -- requires Docker + a real LLM API key
(OPENROUTER_API_KEY / MINIMAX_API_KEY / OPENAI_API_KEY). Only the `--run` path
touches Docker/the network; building the argparser and importing this module
never does (mirrors `run_v3_e2e.py`'s test-safety property).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from httpx import Timeout  # noqa: E402
from openai import OpenAI  # noqa: E402
from src.eval.build_script_eval.fetch import smoke_root  # noqa: E402
from src.eval.graph_repair_ablation.oracle import select  # noqa: E402
from src.eval.graph_repair_ablation.run import ARMS, aggregate, render_report_md, run_one  # noqa: E402

_OUT = _REPO_ROOT / "outputs" / "graph_repair_ablation"


def _csv(s: str) -> frozenset[str]:
    return frozenset(tok.strip() for tok in (s or "").split(",") if tok.strip())


def _build_arg_parser() -> argparse.ArgumentParser:
    """Pure argparse construction -- safe to call/parse against with no Docker
    or LLM key present (only `_build_client` + the `--run` body touch either)."""
    ap = argparse.ArgumentParser(prog="graph_repair_ablation")
    ap.add_argument("--run", action="store_true", help="run the pilot (Docker + live LLM)")
    ap.add_argument("--only", default="", help="comma-sep injection ids")
    ap.add_argument("--class", dest="classes", default="", help="comma-sep failure classes")
    ap.add_argument("--arms", default="", help="comma-sep arms (default: all of run.ARMS)")
    ap.add_argument("--model", default=None, help="LLM model slug")
    return ap


def _build_client():
    """ONE live OpenAI-compatible client for the whole batch. Mirrors
    `scripts/run_v3_e2e.py::_run()`'s construction verbatim: same env-var
    fallback chain for `api_key`/`base_url` (OpenRouter -> MiniMax -> OpenAI),
    same `Timeout` shape, `max_retries=0`. Returns `None` (having already
    printed the error) if no API key is configured."""
    api_key = (os.getenv("OPENROUTER_API_KEY") or os.getenv("MINIMAX_API_KEY")
               or os.getenv("OPENAI_API_KEY"))
    base_url = (os.getenv("OPENROUTER_API_BASE") or os.getenv("MINIMAX_API_BASE")
                or os.getenv("OPENAI_API_BASE"))
    if not api_key:
        print("ERROR: set OPENROUTER_API_KEY / MINIMAX_API_KEY / OPENAI_API_KEY.",
              file=sys.stderr)
        return None
    return OpenAI(
        api_key=api_key, base_url=base_url or None, max_retries=0,
        timeout=Timeout(connect=10.0, read=float(os.getenv("LLM_READ_TIMEOUT", "120")),
                        write=30.0, pool=10.0),
    )


def main(argv=None) -> int:
    ap = _build_arg_parser()
    args = ap.parse_args(argv)

    if not args.run:
        ap.error("nothing to do: pass --run")

    only = _csv(args.only)
    classes = _csv(args.classes)
    requested_arms = _csv(args.arms)
    if requested_arms - set(ARMS):
        ap.error(f"unknown arm(s): {sorted(requested_arms - set(ARMS))}")
    arms = tuple(a for a in ARMS if a in requested_arms) if requested_arms else ARMS

    injections = select(only=only, classes=classes)  # raises on unknown --only/--class
    print(f"[graph_repair_ablation] {len(injections)} injection(s) x {len(arms)} arm(s) selected")

    client = _build_client()
    if client is None:
        return 2
    model = args.model or os.getenv("LLM_MODEL", "gpt-4o")

    root = smoke_root()
    results: list[dict] = []
    for inj in injections:
        for arm in arms:
            try:
                result = run_one(inj, arm, agent_client=client, model=model, smoke_root=root)
            except Exception as exc:  # noqa: BLE001 -- one failing cell must not abort the batch
                print(f"SKIP {inj.injection_id} [{arm}]: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            results.append(result)
            print(f"  {inj.injection_id} [{arm}]: install_failed={result['install_failed']} "
                  f"score={result['score']}")

    agg = aggregate(results)
    _OUT.mkdir(parents=True, exist_ok=True)
    (_OUT / "results.json").write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    report = render_report_md(agg)
    (_OUT / "report.md").write_text(report)
    print(report)
    print(f"wrote {_OUT / 'results.json'} and {_OUT / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
