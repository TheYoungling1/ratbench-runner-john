# bench/unified_bench.py
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields
from glob import glob

REPO_ROOT = os.environ.get("REPO_ROOT") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bench.docker_client import SubprocessDocker  # noqa: E402
from bench.metrics import load_gold_counts
from bench.harvest import discover
from bench.measure import measure
from bench.metrics import compute_metrics
from bench.report.error_report import arm_report, delta as arm_delta
from bench.schema import MeasureRow


def _row_path(out_root: str, agent: str, repo: str) -> str:
    return os.path.join(out_root, agent, *repo.split("/"), "row.json")


def _error_detail(e: BaseException) -> str:
    """A failure string that still says WHY.

    `repr(CalledProcessError)` prints the exit code and argv and NOTHING else, so a subprocess run
    with capture_output=True loses the message entirely: ten repo2run repos recorded
    `CalledProcessError(125, ['docker','run',...])` and the cause was undiagnosable from the rows.
    docker_client runs its commands captured, so the daemon's explanation is sitting in .stderr --
    this pulls it back out. Truncated, because a failed build log can be megabytes and this string
    is persisted in every affected row.
    """
    parts = [repr(e)]
    for stream in ("stderr", "output"):
        raw = getattr(e, stream, None)
        if not raw:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")       # docker output is not guaranteed UTF-8
        text = str(raw).strip()
        if text:
            parts.append(f"{stream}: {text[-2000:]}")
            break                                       # stderr wins; stdout is the fallback
    return " | ".join(parts)


def run_one(env, out_root: str, *, docker) -> str:
    out = _row_path(out_root, env.agent, env.repo.full_name)
    if os.path.exists(out):
        return out                                     # resume
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        row = measure(env, docker=docker)
    except Exception as e:                             # anti-vanish: infra crash still yields a row
        # Carry the PRODUCER's economy across. A crash here says nothing about produce, which has
        # already run and already been paid for: mlflow/mlflow in ccdf-full50-20260727-023223 kept
        # an intact _meta.json (cost_usd=1.8553) while its row read None, silently shrinking
        # total_cost_usd. `env.meta or {}` because non-producer agents harvest with no meta at all.
        m = env.meta or {}
        row = MeasureRow(agent=env.agent, repo=env.repo.full_name, env_status=env.status,
                         build_ok=False, executed=False, ebsr=False, status="measure_error",
                         tokens_in=m.get("tokens_in"), tokens_out=m.get("tokens_out"),
                         llm_calls=m.get("llm_calls"), turns_used=m.get("turns_used"),
                         cost_usd=m.get("cost_usd"), produce_s=m.get("produce_s"),
                         meta=dict(m, error=_error_detail(e)))
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
            # Record WHO invented this status. `missing` is also a status measure.py genuinely
            # assigns, so downstream cannot tell a backfilled one from a measured one by value.
            d["status_backfilled"] = True
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


def _econ(e: dict) -> str:
    """The economy line. Medians, with the telemetry count — a median over 89% of an arm's repos
    is not the same claim as one over all of them, so the n travels with the number."""
    if not e or not e["n_token_reporting"]:
        return "no telemetry on any row"
    tok = f"{e['median_tokens'] / 1000:,.1f}K median"
    cov = "" if e["n_token_reporting"] == e["n_rows"] else f" over {e['n_token_reporting']}/{e['n_rows']} rows"
    turns = f"   turns {e['median_turns']:.0f}" if e["median_turns"] is not None else ""
    cost = f"   ${e['median_cost_usd']:.4f}" if e["median_cost_usd"] is not None else "   cost n/a"
    return tok + cov + turns + cost


def _report_agent(a, ap) -> int:
    """Score one arm across every dataset it has run. Read-only: nothing is measured or rebuilt."""
    from bench.campaign import score_agent

    gold = a.gold
    if gold and not os.path.isfile(gold):
        print(f"--gold not found: {gold}", file=sys.stderr)
        return 2
    try:
        res = score_agent(a.agent_dir, gold_path=gold,
                          datasets_dir=os.path.join(REPO_ROOT, "datasets"))
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2

    print(f"agent      {res['agent']}   ({res['runs_found']} run dirs found)")
    if not res["groups"]:
        print("  no run matched a known dataset — nothing to score", file=sys.stderr)
        for p, ds in res["ignored"]:
            print(f"    ignored {os.path.basename(p)} (dataset={ds})", file=sys.stderr)
        return 2
    if not gold:
        print("  no --gold: ESSR is null (EBSR is unaffected)")

    for group, b in res["groups"].items():
        m = b["metrics"]
        essr = "null" if m["ESSR"] is None else f"{m['ESSR']:.4f}"
        cov = f"{b['n_measured']}/{b['n_expected']}" if b["n_expected"] else str(b["n_measured"])
        print(f"\n  {group}")
        print(f"    source(s)   {', '.join(b['sources'])}"
              + ("   [POOLED]" if b["pooled"] else ""))
        print(f"    repos       {cov} measured   ESSR scored over {m['n_gold_scored']}")
        print(f"    EBSR        {m['EBSR']:.4f}   ({m['n_ebsr']}/{m['n']})")
        print(f"    ESSR        {essr}")
        print(f"    tokens      {_econ(b['economy'])}")
        for flag, msg in (("incomplete", "POOL INCOMPLETE — a part run is missing"),
                          ("ambiguous", "tie on row count broken by name; check `rejected`")):
            if b[flag]:
                print(f"    !! {msg}")
        if b["duplicate_repos"]:
            print(f"    !! {len(b['duplicate_repos'])} repo(s) in more than one source: "
                  f"{', '.join(b['duplicate_repos'][:4])}")
        if b["n_expected"] and b["n_measured"] != b["n_expected"]:
            print(f"    !! {b['n_expected'] - b['n_measured']} repo(s) of the dataset have no row")
        if b["rejected"]:
            print(f"    not used    {', '.join(b['rejected'])}")

    p = res.get("pooled")
    if p:
        essr = "null" if p["ESSR"] is None else f"{p['ESSR']:.4f}"
        print(f"\n  POOLED — the official cross-dataset figure: one mean over one pile of repos")
        print(f"    EBSR        {p['EBSR']:.4f}   ({p['n_ebsr']}/{p['n']} repos)")
        print(f"    ESSR        {essr}   (over the {p['n_gold_scored']} repos with a gold manifest)")
        print(f"    tokens      {_econ(p['economy'])}")
        if p["n_gold_scored"] != p["n"]:
            print(f"    note        the two denominators differ on purpose: every repo has an "
                  f"EBSR,\n                only {p['n_gold_scored']} have a certified ESSR denominator")
    else:
        print("\n  POOLED      not computable — no dataset scored")

    if res["ignored"]:
        print(f"\n  ignored     {', '.join(os.path.basename(p) for p, _ in res['ignored'])}")
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        dest = os.path.join(a.out, "campaign.json")
        with open(dest, "w") as f:
            json.dump(res, f, indent=2, default=str)
        print(f"\nwrote {dest}")
    return 0


def _parse_harvest(arg: str) -> dict:
    out = {}
    for pair in arg.split(","):
        name, _, path = pair.partition("=")
        out[name.strip()] = path.strip()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harvest", help="agent=run_dir,agent2=run_dir2")
    ap.add_argument("--agent-dir",
                    help="score a whole arm from its run root (e.g. runs/claudecode-dockerfile): "
                         "finds the rat50 and envbench100 runs from each run's own manifest, "
                         "scores both, and reports the unified figures. Measures nothing.")
    ap.add_argument("--out", required=False)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--only", help="file of repo full_names (one per line, # comments): measure "
                                   "only these — a targeted re-measure")
    ap.add_argument("--aggregate-only", action="store_true")
    ap.add_argument("--gold", help="gold manifest JSON; enables the gold-anchored ESSR block")
    ap.add_argument("--gold-corpus", default=None,
                    help="corpus inside a combined gold file (e.g. rat_python50, "
                         "envbench_python100). Omitted, every corpus is merged.")
    ap.add_argument("--turn-cap", type=int, default=None,
                    help="repair-loop turn cap; required for the unconverged flag")
    ap.add_argument("--delta", help="AGENT_A:AGENT_B — arm-vs-arm interval delta")
    a = ap.parse_args(argv)

    if a.agent_dir:
        return _report_agent(a, ap)
    if not a.out:
        ap.error("--out is required unless --agent-dir")
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
            # UNION of "about to be measured" and "already in --out". run_one resumes, so a
            # perfectly valid run measures one new arm while --delta compares two arms that were
            # measured earlier: requiring both names in --harvest would reject it. This early
            # check exists only to fail fast, so it must be permissive — the authoritative
            # membership check is the load_rows one below, after the rows exist.
            _known = set(_parse_harvest(a.harvest))
            if os.path.isdir(a.out):
                _known |= {n for n in os.listdir(a.out)
                           if os.path.isdir(os.path.join(a.out, n))}
            if _name_a not in _known or _name_b not in _known:
                print(f"unknown agent in --delta: {a.delta} (know {sorted(_known)})",
                      file=sys.stderr)
                return 2

    if not a.aggregate_only:
        envs = discover(_parse_harvest(a.harvest))
        if a.only:
            with open(a.only, encoding="utf-8") as fh:
                keep = {ln.strip().lower() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")}
            envs = [e for e in envs if e.repo.full_name.lower() in keep]
        docker = SubprocessDocker()
        with ThreadPoolExecutor(max_workers=max(1, a.concurrency)) as ex:
            list(ex.map(lambda e: run_one(e, a.out, docker=docker), envs))

    # Gold-anchored ESSR (bench.metrics.gold_essr). Opt-in via --gold: without it the block is
    # still written, with ESSR_gold=None, so "no gold supplied" stays distinguishable from "this
    # field was never computed". A BAD --gold path is fatal rather than silently unanchored — a
    # table published with no anchor and no warning is the failure this guards.
    # (The node-id-intersection metric in bench/gold.py remains dormant and separate.)
    gold = None
    if a.gold:
        try:
            gold = load_gold_counts(a.gold, corpus=a.gold_corpus)
        except (FileNotFoundError, ValueError, KeyError) as e:
            print(f"--gold could not be loaded: {e}", file=sys.stderr)
            return 2
        if not gold:
            print(f"--gold loaded 0 usable denominators from {a.gold}"
                  + (f" (corpus {a.gold_corpus})" if a.gold_corpus else ""), file=sys.stderr)
            return 2
        print(f"[gold] {len(gold)} denominators from {os.path.basename(a.gold)}"
              + (f" :: {a.gold_corpus}" if a.gold_corpus else ""), file=sys.stderr)

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
