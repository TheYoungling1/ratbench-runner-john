# tools/verify_run.py — Tier 0 of DATA_VERIFICATION.md: mechanical audit of one run directory.
#
# Every check here is a field extraction or a count. Nothing in this file needs judgement, which is
# exactly why no agent runs it. Its job is to produce the *flagged subset* that Tier-1 Sonnet agents
# then read trajectories for — so the failure mode that matters is under-flagging, not noise.
#
#   python -m tools.verify_run runs/setupx/envbench100-merged-20260915
#   python -m tools.verify_run runs/*/envbench100-merged-20260915 --json out.json
import argparse
import glob
import hashlib
import json
import os
import re
import sys

# flag -> (severity, one-line meaning). HIGH = the number itself may be wrong or unattributable.
# MED = the run is real but a human/agent has to read it before it counts.
FLAGS = {
    "head_sha_mismatch": ("HIGH", "produced commit != dataset pinned commit"),
    "model_mismatch": ("HIGH", "assistant turns served by a model other than the requested one"),
    "cost_provenance": ("HIGH", "cost_usd present but usage_source != 'computed'"),
    "token_blackhole": ("HIGH", "llm_calls>0 with no usage at all — provider omitted usage"),
    "total_tokens_underived": ("MED", "tokens_in/out present but total_tokens never rolled up"),
    "missing_dockerfile": ("HIGH", "status=produced but no eval_build/Dockerfile on disk"),
    "status_error": ("MED", "status=error — confirm it is exhaustion, not harness failure"),
    "unreplayed": ("MED", "replay fell back to atom-rendered commands; trajectory incomplete"),
    "phase2_incomplete": ("MED", "setupx phase2 != True — different measurement regime"),
    "turn_capped": ("MED", "hit the turn cap; turns_used is a floor, not a cost"),
    "agent_timed_out": ("MED", "wall-clock timeout"),
    "no_image_digest": ("MED", "deploy_image_digest null — rebuild not pinned"),
    "requery_burn": ("MED", "3+ parser requeries; wasted calls inflate turns/cost"),
}
REQUERY_FLOOR = 3
# Bridge-served runs report the requested id once in session-init and the real one on every turn.
_MODEL_RE = re.compile(rb'"model"\s*:\s*"([^"]+)"')


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def served_models(repo_dir):
    """Model ids actually seen on assistant turns, by count. Empty when the arm logs no stream."""
    counts = {}
    for name in ("claude_stream.jsonl",):
        path = os.path.join(repo_dir, name)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as fh:
            for line in fh:
                for m in _MODEL_RE.findall(line):
                    key = m.decode()
                    counts[key] = counts.get(key, 0) + 1
    return counts


def requery_count(repo_dir):
    """SWE-agent re-asks the model when the function-call parser rejects a turn. Each one is paid for."""
    path = os.path.join(repo_dir, "sweagent_df_stdout.txt")
    if not os.path.exists(path):
        return 0
    with open(path, "rb") as fh:
        return fh.read().count(b"FunctionCallingFormatError")


def phase2_state(note):
    """setupx encodes phase-2 outcome in the free-text note: phase2=True/False/None."""
    m = re.search(r"phase2=(\w+)", note or "")
    return m.group(1) if m else None


def check_repo(repo_dir, meta, pinned_commit):
    flags = []
    detail = {}

    head = meta.get("head_sha")
    if pinned_commit and head and not (head.startswith(pinned_commit) or pinned_commit.startswith(head)):
        flags.append("head_sha_mismatch")
        detail["head_sha"] = f"{head} != {pinned_commit}"

    models = served_models(repo_dir)
    if models:
        detail["models"] = models
        served = max(models, key=models.get)  # the one on every turn, not the init line
        requested = meta.get("requested_model") or (meta.get("agent_settings") or {}).get("model")
        if requested and served.split(":")[0] not in requested:
            flags.append("model_mismatch")
            detail["served"] = served

    if meta.get("cost_usd") is not None and meta.get("usage_source") != "computed":
        flags.append("cost_provenance")
        detail["usage_source"] = meta.get("usage_source")

    # total_tokens is a derived rollup that some arms never populate, so a null there is not evidence
    # of missing usage — fall back to the components before calling it a black hole.
    tokens = meta.get("total_tokens") or (meta.get("tokens_in") or 0) + (meta.get("tokens_out") or 0)
    if (meta.get("llm_calls") or 0) > 0 and not tokens:
        flags.append("token_blackhole")
    elif not meta.get("total_tokens") and tokens:
        flags.append("total_tokens_underived")
        detail["tokens"] = f"in+out={tokens:.0f}, total_tokens=null"

    if meta.get("status") == "produced" and not os.path.exists(
        os.path.join(repo_dir, "eval_build", "Dockerfile")
    ):
        flags.append("missing_dockerfile")
    if meta.get("status") == "error":
        flags.append("status_error")
        detail["note"] = (meta.get("note") or "")[:160]

    if meta.get("unreplayed"):
        flags.append("unreplayed")

    p2 = phase2_state(meta.get("note"))
    if p2 is not None and p2 != "True":
        flags.append("phase2_incomplete")
        detail["phase2"] = p2

    for key in ("turn_capped", "agent_timed_out"):
        if meta.get(key):
            flags.append(key)

    if meta.get("deploy_image_digest") is None:
        flags.append("no_image_digest")

    rq = requery_count(repo_dir)
    if rq >= REQUERY_FLOOR:
        flags.append("requery_burn")
        detail["requeries"] = rq

    return flags, detail


def check_merge(run_dir, names, dataset_by_name):
    """A merged dir is the unit of analysis, so its own provenance has to hold before its rows do."""
    manifest = _read_json(os.path.join(run_dir, "_run_manifest.json"))
    if not manifest:
        return []
    problems = []
    ds = manifest.get("dataset", {})
    ds_path = ds.get("path")
    if ds_path and os.path.exists(ds_path):
        actual = _sha256(ds_path)
        if actual != ds.get("sha256"):
            problems.append(f"HIGH dataset sha256 drift: {ds_path} is {actual[:12]}, manifest says {(ds.get('sha256') or '')[:12]}")
    else:
        problems.append(f"HIGH dataset file not found: {ds_path}")

    expected = set(dataset_by_name)
    extra, missing = names - expected, expected - names
    if extra:
        problems.append(f"HIGH {len(extra)} repo(s) outside the dataset: {sorted(extra)[:5]}")
    if missing:
        problems.append(f"HIGH {len(missing)} dataset repo(s) absent: {sorted(missing)[:5]}")

    if manifest.get("harness_commit_consistent_across_sources") is False:
        pairs = ", ".join(
            f"{s.get('run')}@{s.get('agent_commit')}" for s in manifest.get("source_runs", [])
        )
        problems.append(f"HIGH merge spans multiple agent commits: {pairs}")
    return problems


def verify(run_dir, dataset_path):
    dataset_by_name = {r["full_name"]: r.get("commit") for r in _read_json(dataset_path, [])}
    rows = []
    for meta_path in sorted(glob.glob(os.path.join(run_dir, "output", "*", "*", "_meta.json"))):
        repo_dir = os.path.dirname(meta_path)
        meta = _read_json(meta_path, {})
        name = meta.get("full_name") or "/".join(repo_dir.split(os.sep)[-2:])
        flags, detail = check_repo(repo_dir, meta, dataset_by_name.get(name))
        rows.append({"repo": name, "dir": repo_dir, "flags": flags, "detail": detail})
    # deploy_image_digest is an arm property, not a per-repo anomaly: setupx and claudecode-dockerfile
    # never populate it. Flagging 100/100 rows buries the Tier-1 worklist under noise, so it only
    # stays a flag when the run populates it *somewhere* — i.e. when a null is genuinely the outlier.
    notes = []
    if rows and all("no_image_digest" in r["flags"] for r in rows):
        notes.append(f"deploy_image_digest null for all {len(rows)} repos — arm does not pin rebuild images")
        for r in rows:
            r["flags"].remove("no_image_digest")

    merge = check_merge(run_dir, {r["repo"] for r in rows}, dataset_by_name)
    return {"run": run_dir, "n": len(rows), "merge_problems": merge, "notes": notes, "repos": rows}


def severity(flags):
    return "HIGH" if any(FLAGS[f][0] == "HIGH" for f in flags) else ("MED" if flags else "OK")


def report(result, out=sys.stdout):
    print(f"\n=== {result['run']}  (n={result['n']})", file=out)
    for p in result["merge_problems"]:
        print(f"  !! {p}", file=out)
    for n in result.get("notes", []):
        print(f"  -- {n}", file=out)

    counts = {}
    for row in result["repos"]:
        for f in row["flags"]:
            counts[f] = counts.get(f, 0) + 1
    if counts:
        print("\n  flag counts:", file=out)
        for f, c in sorted(counts.items(), key=lambda kv: (FLAGS[kv[0]][0] != "HIGH", -kv[1])):
            print(f"    {FLAGS[f][0]:4} {f:20} {c:4}   {FLAGS[f][1]}", file=out)

    flagged = [r for r in result["repos"] if r["flags"]]
    if flagged:
        print(f"\n  flagged repos ({len(flagged)}/{result['n']}) — Tier-1 worklist:", file=out)
        for row in sorted(flagged, key=lambda r: severity(r["flags"]) != "HIGH"):
            extra = " ".join(f"{k}={v}" for k, v in row["detail"].items() if k != "models")
            print(f"    {severity(row['flags']):4} {row['repo']:45} {','.join(row['flags'])} {extra}"[:200], file=out)
    else:
        print("  no flags", file=out)
    return sum(1 for r in result["repos"] if severity(r["flags"]) == "HIGH") + len(result["merge_problems"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tier-0 mechanical audit of a benchmark run directory.")
    ap.add_argument("run_dirs", nargs="+")
    ap.add_argument("--dataset", default="datasets/envbench_python100.json")
    ap.add_argument("--json", help="write full per-repo results here")
    args = ap.parse_args(argv)

    results, high = [], 0
    for run_dir in args.run_dirs:
        result = verify(run_dir, args.dataset)
        results.append(result)
        high += report(result)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2)
        print(f"\nwrote {args.json}", file=sys.stderr)
    print(f"\n{high} HIGH-severity item(s) across {len(results)} run(s).", file=sys.stderr)
    return 1 if high else 0


if __name__ == "__main__":
    sys.exit(main())
