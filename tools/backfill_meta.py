#!/usr/bin/env python3
"""Rebuild `_meta.json` files that a pre-0b25b0d runner clobbered on the produce-FAILURE path.

WHY THIS EXISTS
    `_ProducerModel.predict()` always wrote an authoritative `_meta.json` (status, cost_usd,
    tokens, turns). But `_run_one` keyed its own meta write on `run_produced.json`, which only the
    SUCCESS path writes — so on failure it dumped `_collect_meta`'s 9 runtime keys over the top and
    destroyed 17. Downstream, `harvest.py` found no `status`, resolved the packet as
    `legacy_missing`, and `metrics.py` dropped it from `n` entirely. Failed repos vanished from the
    denominator AND their real spend vanished from total_cost_usd.

    0b25b0d fixed the write path. It cannot fix runs already on disk — hence this.

WHAT IT RECOVERS, AND FROM WHERE
    The economy comes from each repo's `claude_stream.jsonl`, parsed with the SAME
    `summarize_stream` the live producer uses, so the recovered numbers are byte-for-byte what a
    fixed run would have written rather than a second, subtly different parser.

    Identity/contract fields (producer, contract_version, conformance, language) are copied from a
    sibling INTACT `_meta.json` in the same run — they are per-run constants, and taking them from
    the run itself beats hardcoding.

    `status` is set to "error". That is not a guess: the clobber happens on exactly one branch,
    the one where `run_produced.json` is absent. The script asserts that absence per repo and
    refuses to touch anything else.

    `note` and `produce_s` are NOT invented. The original note ("no Dockerfile.gen") is
    unrecoverable, and `produce_s` measured only the agent call while the surviving `duration_s`
    covers clone+setup too. Writing a plausible number here would be fabrication; they stay null
    and `backfilled` marks the record so no one mistakes it for a live write.

USAGE
    python3 tools/backfill_meta.py <run_dir> [<run_dir> ...] [--apply]
    Dry-run by default: prints what it would change and exits without writing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from glob import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from producers._claudecode_helpers import summarize_stream  # noqa: E402

# Copied from a sibling intact meta rather than hardcoded — they are per-run constants.
_INHERITED = ("contract_version", "producer", "conformance", "language")


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _run_constants(repo_dirs: list) -> dict:
    """Per-run constants, taken from the intact metas. Requires unanimity: if two intact packets
    disagree on `language`, this is a mixed-language run and copying either one is wrong."""
    seen: dict = {k: set() for k in _INHERITED}
    for d in repo_dirs:
        meta = _load(os.path.join(d, "_meta.json"))
        if "status" not in meta:
            continue
        for k in _INHERITED:
            if meta.get(k) is not None:
                seen[k].add(meta[k])
    out = {}
    for k, vals in seen.items():
        if len(vals) == 1:
            out[k] = vals.pop()
        elif len(vals) > 1:
            raise SystemExit(f"[backfill] FATAL: intact packets disagree on {k!r}: {sorted(vals)}. "
                             f"Refusing to guess — backfill this run by hand.")
    return out


def plan_one(repo_dir: str, consts: dict) -> dict | None:
    """Return the meta dict this repo SHOULD have, or None if it needs no backfill."""
    meta_path = os.path.join(repo_dir, "_meta.json")
    meta = _load(meta_path)
    if not meta or "status" in meta:
        return None                                   # intact (or absent) — leave it alone
    if os.path.exists(os.path.join(repo_dir, "run_produced.json")):
        # A success marker with a statusless meta is a DIFFERENT failure than the one this tool
        # understands. Skip loudly rather than assert "error" over a run that may have succeeded.
        print(f"  !! {repo_dir}: statusless meta but run_produced.json present — SKIPPED")
        return None

    stream_path = os.path.join(repo_dir, "claude_stream.jsonl")
    try:
        with open(stream_path, encoding="utf-8", errors="replace") as fh:
            info = summarize_stream(fh.read())
    except OSError:
        info = {}                                     # no stream: still restore status, economy null

    full_name = "/".join(repo_dir.rstrip("/").split(os.sep)[-2:])
    rebuilt = dict(meta)                              # keep every surviving runtime key
    rebuilt.update({
        "status": "error",
        "note": None,
        "unreplayed": False,
        "inline": None,
        "full_name": full_name,
        "repo_url": f"https://github.com/{full_name}",
        "tokens_in": info.get("tokens_in"),
        "tokens_out": info.get("tokens_out"),
        "llm_calls": info.get("llm_calls"),
        "turns_used": info.get("turns"),
        "total_tokens": info.get("total_tokens"),
        "cost_usd": info.get("cost_usd"),
        "produce_s": None,
        "backfilled": "tools/backfill_meta.py from claude_stream.jsonl",
    })
    for k, v in consts.items():
        rebuilt.setdefault(k, v)
        if rebuilt.get(k) is None:
            rebuilt[k] = v
    return rebuilt


def backfill_run(run_dir: str, apply: bool) -> tuple:
    out_root = os.path.join(run_dir, "output")
    repo_dirs = [d for d in sorted(glob(os.path.join(out_root, "*", "*"))) if os.path.isdir(d)]
    if not repo_dirs:
        raise SystemExit(f"[backfill] no repo dirs under {out_root}")
    consts = _run_constants(repo_dirs)
    changed, recovered = 0, 0.0
    for d in repo_dirs:
        rebuilt = plan_one(d, consts)
        if rebuilt is None:
            continue
        changed += 1
        recovered += rebuilt.get("cost_usd") or 0.0
        print(f"  {'WRITE' if apply else 'would write'} {'/'.join(d.split(os.sep)[-2:]):42s} "
              f"status=error cost=${rebuilt.get('cost_usd') or 0:.4f} "
              f"turns={rebuilt.get('turns_used')}")
        if apply:
            path = os.path.join(d, "_meta.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rebuilt, fh, indent=2)
            os.replace(tmp, path)                     # atomic: never leave a half-written packet
    return changed, recovered


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dirs", nargs="+")
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry-run")
    args = ap.parse_args()
    total, money = 0, 0.0
    for run_dir in args.run_dirs:
        print(f"\n=== {run_dir} ===")
        c, m = backfill_run(run_dir, args.apply)
        total += c
        money += m
        print(f"  -> {c} packet(s), ${m:.4f} recovered")
    verb = "restored" if args.apply else "recoverable (dry run — pass --apply to write)"
    print(f"\n{total} packet(s), ${money:.4f} {verb}")


if __name__ == "__main__":
    main()
