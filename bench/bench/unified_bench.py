# bench/unified_bench.py
from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from glob import glob

from bench.docker_client import SubprocessDocker
# from bench.gold import load_gold  # golden-set calc disabled for now (no gold JSON)
from bench.harvest import discover
from bench.measure import measure
from bench.metrics import compute_metrics
from bench.schema import MeasureRow


def _row_path(out_root: str, agent: str, repo: str) -> str:
    return os.path.join(out_root, agent, *repo.split("/"), "row.json")


def run_one(env, out_root: str, *, docker) -> str:
    out = _row_path(out_root, env.agent, env.repo.full_name)
    if os.path.exists(out):
        return out                                     # resume
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        row = measure(env, docker=docker)
    except Exception as e:                             # anti-vanish: infra crash still yields a row
        row = MeasureRow(agent=env.agent, repo=env.repo.full_name, env_status=env.status,
                         build_ok=False, executed=False, ebsr=False, meta={"error": repr(e)})
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(asdict(row), f, indent=2, default=list)
    os.replace(tmp, out)
    return out


def aggregate(out_root: str, gold: dict | None = None) -> dict:
    by_agent: dict = {}
    for p in glob(os.path.join(out_root, "*", "**", "row.json"), recursive=True):
        with open(p) as f:
            d = json.load(f)
        agent = os.path.relpath(p, out_root).split(os.sep)[0]
        d.pop("agent", None)
        row = MeasureRow(agent=agent, **{k: (tuple(v) if isinstance(v, list) else v)
                                         for k, v in d.items()})
        by_agent.setdefault(agent, []).append(row)
    return {a: compute_metrics(rows, gold=gold) for a, rows in by_agent.items()}


def _parse_harvest(arg: str) -> dict:
    out = {}
    for pair in arg.split(","):
        name, _, path = pair.partition("=")
        out[name.strip()] = path.strip()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harvest", help="agent=run_dir,agent2=run_dir2")
    ap.add_argument("--out", required=True)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--aggregate-only", action="store_true")
    ap.add_argument("--gold")
    a = ap.parse_args(argv)

    if not a.aggregate_only and not a.harvest:
        ap.error("--harvest is required unless --aggregate-only")

    if not a.aggregate_only:
        envs = discover(_parse_harvest(a.harvest))
        docker = SubprocessDocker()
        with ThreadPoolExecutor(max_workers=max(1, a.concurrency)) as ex:
            list(ex.map(lambda e: run_one(e, a.out, docker=docker), envs))

    # Gold-anchored scoring is DEPRECATED — force None so no gold JSON is ever read (a missing
    # gold file can't error). `--gold` is accepted but IGNORED. See bench/gold.py (dormant).
    gold = None
    # gold = load_gold(a.gold) if a.gold else None
    out = aggregate(a.out, gold=gold)
    with open(os.path.join(a.out, "metrics.json"), "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
