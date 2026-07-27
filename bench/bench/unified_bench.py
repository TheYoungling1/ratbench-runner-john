# bench/unified_bench.py
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields
from glob import glob

from bench.docker_client import SubprocessDocker
# from bench.gold import load_gold  # golden-set calc disabled for now (no gold JSON)
from bench.harvest import discover
from bench.measure import measure
from bench.metrics import compute_metrics
from bench.report.error_report import arm_report, delta as arm_delta
from bench.schema import MeasureRow
from bench.verdict import STATUS_BACKFILL_MARKER


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
                         build_ok=False, executed=False, ebsr=False, status="measure_error",
                         meta={"error": repr(e)})
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(asdict(row), f, indent=2, default=list)
    os.replace(tmp, out)
    return out


_ROW_FIELDS = {f.name for f in fields(MeasureRow)}
SOURCES = ("collect", "run")


def load_rows(out_root: str) -> dict:
    by_agent: dict = {}
    for p in glob(os.path.join(out_root, "*", "**", "row.json"), recursive=True):
        with open(p) as f:
            d = json.load(f)
        agent = os.path.relpath(p, out_root).split(os.sep)[0]
        d.pop("agent", None)
        # Legacy row.json (pre-taxonomy) lacks `status`/`py_test_files`. Default `status` safely so an
        # old run re-aggregates without crashing and isn't force-fit into a credited/denied bucket
        # (design §2.5): legacy_ok if it shows a build/execution signal, else missing.
        if "status" not in d:
            d["status"] = "legacy_ok" if (d.get("build_ok") or d.get("executed")) else "missing"
            # Stamp WHO invented this status. `missing` is also a status measure.py genuinely
            # assigns, so downstream cannot tell a backfilled one from a measured one by value.
            # In-memory only — load_rows never rewrites row.json, and metrics.py ignores meta.
            d["meta"] = dict(d.get("meta") or {}, **{STATUS_BACKFILL_MARKER: True})
        # Filter to KNOWN fields: a row.json written by a newer checkout otherwise crashes an
        # older one with TypeError. Costs one line, prevents a cross-branch collision.
        row = MeasureRow(agent=agent, **{k: (tuple(v) if isinstance(v, list) else v)
                                         for k, v in d.items() if k in _ROW_FIELDS})
        by_agent.setdefault(agent, []).append(row)
    return by_agent


def aggregate(out_root: str, gold: dict | None = None) -> dict:
    return {a: compute_metrics(rows, gold=gold) for a, rows in load_rows(out_root).items()}


def aggregate_errors(out_root: str, *, turn_cap=None) -> dict:
    """{agent: {source: report}} — never pooled across sources (spec section 8)."""
    return {a: {s: asdict(arm_report(rows, source=s, turn_cap=turn_cap)) for s in SOURCES}
            for a, rows in load_rows(out_root).items()}


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
    ap.add_argument("--turn-cap", type=int, default=None,
                    help="repair-loop turn cap; required for the unconverged flag")
    ap.add_argument("--delta", help="AGENT_A:AGENT_B — arm-vs-arm interval delta")
    a = ap.parse_args(argv)

    if not a.aggregate_only and not a.harvest:
        ap.error("--harvest is required unless --aggregate-only")

    # `--delta` is validated BEFORE anything is produced, not just before the aggregate write.
    # In the full path the agent names come from `--harvest`, so they are knowable without
    # measuring anything: a typo fails in milliseconds instead of after a full docker measure
    # pass has already written every row.json. The post-measure membership check below still
    # runs — it is the one that covers --aggregate-only and catches an agent that harvested but
    # produced no rows.
    if a.delta:
        _name_a, _sep, _name_b = a.delta.partition(":")
        if not (_sep and _name_a and _name_b):
            print(f"--delta must be AGENT_A:AGENT_B, got {a.delta!r}", file=sys.stderr)
            return 2
        if not a.aggregate_only:
            _known = set(_parse_harvest(a.harvest))
            if _name_a not in _known or _name_b not in _known:
                print(f"unknown agent in --delta: {a.delta} (--harvest has {sorted(_known)})",
                      file=sys.stderr)
                return 2

    if not a.aggregate_only:
        envs = discover(_parse_harvest(a.harvest))
        docker = SubprocessDocker()
        with ThreadPoolExecutor(max_workers=max(1, a.concurrency)) as ex:
            list(ex.map(lambda e: run_one(e, a.out, docker=docker), envs))

    # Gold-anchored scoring is DEPRECATED — force None so no gold JSON is ever read (a missing
    # gold file can't error). `--gold` is accepted but IGNORED. See bench/gold.py (dormant).
    gold = None
    # gold = load_gold(a.gold) if a.gold else None

    # validate BEFORE any write — a bad --delta must not leave a half-written output dir
    pair = None
    if a.delta:
        name_a, _, name_b = a.delta.partition(":")
        by_agent = load_rows(a.out)
        if name_a not in by_agent or name_b not in by_agent:
            print(f"unknown agent in --delta: {a.delta} (have {sorted(by_agent)})", file=sys.stderr)
            return 2
        pair = (by_agent[name_a], by_agent[name_b])

    out = aggregate(a.out, gold=gold)
    with open(os.path.join(a.out, "metrics.json"), "w") as f:   # EXISTING deliverable, first
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))

    errors_path = os.path.join(a.out, "errors.json")
    try:
        errs = aggregate_errors(a.out, turn_cap=a.turn_cap)
        if pair:
            errs["_delta"] = {s: arm_delta(pair[0], pair[1], source=s, turn_cap=a.turn_cap)
                              for s in SOURCES}
        with open(errors_path, "w") as f:
            json.dump(errs, f, indent=2)
    except Exception as e:                       # classification is additive — never fatal
        # ...but a STALE errors.json is worse than none. metrics.json has just been rewritten,
        # so leaving a previous run's errors.json beside it presents two artifacts from
        # different runs as one consistent report — and the exit code is 0, so nothing signals
        # it. Silence about the current run beats confident numbers about an older one.
        if os.path.exists(errors_path):
            os.remove(errors_path)
            print(f"removed stale {errors_path} — it predates this run", file=sys.stderr)
        print(f"error classification failed: {e!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
