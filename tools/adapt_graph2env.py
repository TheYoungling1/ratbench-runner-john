#!/usr/bin/env python3
"""Shim Graph2Env ablation output into the harness run layout so unified_bench can score it.

Graph2Env's ReAct runner writes <root>/<owner>/<repo>/{Dockerfile,setup.sh,...} -- already the
shape bench.harvest reads. Its own react_ebsr_results.json reports an EBSR, but that number comes
from a different gate than the paper's (no /testbed conformance probe, no collect_clean taxonomy),
so it is NOT comparable with the sweagent/setupx/ccdf arms. Rebuilding the same Dockerfiles
through bench.measure is what makes the ablation citable next to the baselines.

Four things stand between the two layouts, and this script does only those four:
  1. a `_run_manifest.json` naming the dataset, so the run is self-describing;
  2. a `_meta.json` per repo -- harvest needs `status`, and gold ESSR needs `head_sha`;
  3. STUB dirs for dataset repos the arm never emitted, so every arm scores over the same N
     (no_populate is missing scylladb/scylla-cluster-tests entirely -- without a stub it would
     score over 39 repos while no_graph_context scores over 40, and the delta would be a lie);
  4. a symlink mirror, so nothing is copied and the source tree is never written into.

It deliberately does NOT invent scores. Rebuilding is the caller's next step:
    python -m bench.unified_bench --harvest <arm>=<run>/output --out <run>/measure --gold <gold>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

_SOURCE_SHA = re.compile(r"^\s*ARG\s+SOURCE_SHA=([0-9a-fA-F]{40})\s*$", re.M)
_FROM = re.compile(r"^\s*FROM\s+(\S+)", re.M)
_COPY = re.compile(r"^\s*COPY\s+(.*)$", re.M)

# Graph2Env's token_usage.json spellings, normalised to the harness's names.
_IN, _OUT, _CALLS = ("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"), ("api_calls",)

# Gold corpora to draw denominators from, in priority order. A repo found in an earlier corpus is
# not re-read from a later one.
_COMBINED = "datasets/gold_manifests_combined.json"


def _repo_dirs(output_root: str):
    """(full_name, source_dir) for every <owner>/<repo> under an output tree."""
    for owner in sorted(os.listdir(output_root)):
        od = os.path.join(output_root, owner)
        if not os.path.isdir(od):
            continue
        for name in sorted(os.listdir(od)):
            rd = os.path.join(od, name)
            if os.path.isdir(rd):
                yield f"{owner}/{name}", rd


def _head_sha(src: str) -> str | None:
    """The commit the image was actually built at.

    source_revision.json when the runner wrote one; else the Dockerfile's `ARG SOURCE_SHA`, which
    the build itself verifies (`test "$(git rev-parse HEAD)" = "$SOURCE_SHA"`) and is therefore
    authoritative. NEVER the dataset's pinned commit -- that is what was asked for, not what was
    built, and substituting it would forge the very alignment gold_essr checks.
    """
    p = os.path.join(src, "source_revision.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as fh:
                sha = (json.load(fh).get("head_sha") or "").strip().lower()
            if len(sha) == 40:
                return sha
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    dfp = os.path.join(src, "Dockerfile")
    if os.path.isfile(dfp):
        with open(dfp, encoding="utf-8", errors="replace") as fh:
            m = _SOURCE_SHA.search(fh.read())
        if m:
            return m.group(1).lower()
    return None


def unbuildable(src: str) -> str | None:
    """Why this repo cannot be rebuilt at all, or None if it can.

    Two shapes, both meaning "the agent emitted no environment":
      * no Dockerfile -- the construction never reached synthesis;
      * a Dockerfile wrapping an EMPTY setup.sh. Graph2Env still renders its generic template
        when the agent dies (google/trax: artifact_kind="empty", v3_returncode=-9 i.e. SIGKILL),
        and that template BUILDS. Measuring it scores a false green off an OOM.
    A `best_effort` artifact with real commands is NOT unbuildable -- it is a genuine attempt and
    must get a real rebuild verdict.
    """
    if not os.path.isfile(os.path.join(src, "Dockerfile")):
        return "no Dockerfile"
    p = os.path.join(src, "setup.sh")
    if not os.path.isfile(p):
        return "Dockerfile but no setup.sh"
    with open(p, encoding="utf-8", errors="replace") as fh:
        body = fh.read()
    inert = ("true", ":", "set -e", "set -eu", "set -euo pipefail")
    if not [l for l in (ln.strip() for ln in body.splitlines())
            if l and not l.startswith("#") and l not in inert]:
        return f"empty setup.sh ({len(body)}B, no effective commands)"
    return None


def _meta(src: str, skip_reason: str | None = None, *, charge: bool = False) -> dict:
    """tokens / calls / head_sha, plus the harvest status.

    `status` decides the denominator. bench.harvest passes "produced" through to measure and
    short-circuits anything else; bench.metrics drops only {unmeasurable, legacy_missing} from
    every denominator. So a repo the agent got no Dockerfile out of must arrive as "missing" --
    a counted EBSR-0 -- and NOT as one of those two, which would quietly shrink n.
    """
    dfp = os.path.join(src, "Dockerfile")
    # "unmeasurable" is the ONLY status bench.metrics drops from every denominator alongside
    # legacy_missing; "missing" would score a counted EBSR 0 instead. Skipping is therefore a
    # deliberate narrowing of n, reported as n_unmeasurable, never a silent zero.
    # Two ways to treat a repo that emitted no rebuildable environment, and they are NOT the same
    # number. CHARGE (status "missing") keeps it in the EBSR denominator as a zero -- the paper's
    # convention: "EBSR = mean over ALL measured repos", so n stays constant across arms and the
    # column is comparable. SKIP ("unmeasurable") drops it from every denominator, which answers a
    # different question ("given the agent produced something, how good is it?") and must never be
    # mixed into a table with charged arms.
    if skip_reason:
        m: dict = {"status": "missing" if charge else "unmeasurable",
                   "language": "python", "source": "graph2env",
                   ("charge_reason" if charge else "skip_reason"): skip_reason}
    else:
        m = {"status": "produced" if os.path.isfile(dfp) else "missing",
             "language": "python", "source": "graph2env"}
    sha = _head_sha(src)
    if sha:
        m["head_sha"] = sha
    # base_image feeds image_delta_mb only. Without it that column is None and the ablation's
    # economy numbers cannot be put next to the baselines', which all report it.
    if os.path.isfile(dfp):
        with open(dfp, encoding="utf-8", errors="replace") as fh:
            first = _FROM.search(fh.read())
        if first:
            m["base_image"] = first.group(1)
    p = os.path.join(src, "token_usage.json")
    if not os.path.isfile(p):
        return m
    try:
        with open(p, encoding="utf-8") as fh:
            t = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        m["token_usage_error"] = repr(e)      # loud, not silent: a null token is a real finding
        return m
    if not isinstance(t, dict):
        m["token_usage_error"] = f"not an object: {type(t).__name__}"
        return m
    pick = lambda keys: next((t[k] for k in keys if isinstance(t.get(k), int)), None)
    m["tokens_in"], m["tokens_out"], m["llm_calls"] = pick(_IN), pick(_OUT), pick(_CALLS)
    return m


def _check(src: str) -> list:
    """COPY sources the Dockerfile needs that this bundle does not carry as a FILE.

    bench.measure builds from a tmpdir holding the Dockerfile plus the file siblings harvest
    inlined, so a COPY of a directory present on disk still reaches the build empty.
    """
    dfp = os.path.join(src, "Dockerfile")
    if not os.path.isfile(dfp):
        return []
    with open(dfp, encoding="utf-8", errors="replace") as fh:
        df = fh.read()
    missing = []
    for args in _COPY.findall(df):
        parts = [p for p in args.split() if not p.startswith("--")]
        for s in parts[:-1]:
            p = os.path.join(src, os.path.basename(s))
            if not os.path.exists(p):
                missing.append(s)
            elif os.path.isdir(p):
                missing.append(f"{s} (directory: on disk, not carried into the build context)")
    return sorted(set(missing))


def _mirror(src: str, dest: str) -> None:
    """Symlink every entry of `src` into `dest`."""
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(src):
        link = os.path.join(dest, name)
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(os.path.abspath(os.path.join(src, name)), link)


def _dataset(path: str) -> tuple:
    with open(path, "rb") as fh:
        raw = fh.read()
    doc = json.loads(raw)
    repos = doc["repos"] if isinstance(doc, dict) else doc
    return repos, hashlib.sha256(raw).hexdigest()


def adapt_arm(name: str, roots: list, repos: list, ds_path: str, ds_sha: str, runs_root: str,
              skip: dict | None = None, *, charge: bool = False) -> int:
    """Mirror one arm (possibly assembled from several output roots) into runs_root/<name>.

    `skip` maps repo_lower -> reason; those land as `unmeasurable` and leave every denominator.
    """
    skip = skip or {}
    canonical = {r["full_name"].lower(): r["full_name"] for r in repos}
    run_dir = os.path.join(runs_root, name, os.path.basename(ds_path).removesuffix(".json"))
    dest_out = os.path.join(run_dir, "output")

    seen: dict = {}
    off_dataset, unbuildable, sources = [], {}, {}
    for root in roots:
        if not os.path.isdir(root):
            print(f"  !! no such output root: {root}", file=sys.stderr)
            return 2
        for full_name, src in _repo_dirs(root):
            key = full_name.lower()
            if key not in canonical:
                off_dataset.append(full_name)
                continue
            if key in seen:
                # Two roots claiming one repo would silently pick whichever came last.
                print(f"  !! {full_name} present in both {seen[key]} and {root}", file=sys.stderr)
                return 2
            seen[key] = root
            dest = os.path.join(dest_out, *canonical[key].split("/"))
            _mirror(src, dest)
            with open(os.path.join(dest, "_meta.json"), "w", encoding="utf-8") as fh:
                json.dump(_meta(src, skip.get(key), charge=charge), fh, indent=2)
            sources[root] = sources.get(root, 0) + 1
            for miss in _check(src):
                unbuildable[miss] = unbuildable.get(miss, 0) + 1

    # Stubs: a dataset repo this arm never emitted is an arm failure, not an absent measurement.
    stubs = sorted(set(canonical) - set(seen))
    for key in stubs:
        dest = os.path.join(dest_out, *canonical[key].split("/"))
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "_meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"status": "missing" if (charge or key not in skip) else "unmeasurable",
                       "language": "python", "source": "graph2env",
                       "skip_reason": skip.get(key),
                       "stub_reason": "no output directory in any source root"}, fh, indent=2)

    n_df = sum(1 for k in seen
               if os.path.isfile(os.path.join(dest_out, *canonical[k].split("/"), "Dockerfile")))
    manifest = {
        "variety": name, "tier": "all", "adapter": "tools/adapt_graph2env.py",
        "adapted_from": [os.path.abspath(r) for r in roots],
        "dataset": {"path": ds_path, "repo_count": len(repos), "sha256": ds_sha},
        "repos_linked": len(seen), "repos_stubbed": stubs,
        "repos_skipped": {canonical[k]: v for k, v in sorted(skip.items()) if k in canonical},
        "repos_with_dockerfile": n_df,
        "repos_per_source": {os.path.abspath(k): v for k, v in sources.items()},
        "missing_copy_sources": unbuildable,
        "off_dataset_dropped": sorted(off_dataset),
        "note": "Symlink mirror of Graph2Env artifacts. Scores come from bench.unified_bench "
                "rebuilding these Dockerfiles, NOT from react_ebsr_results.json.",
    }
    with open(os.path.join(run_dir, "_run_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    flag = ""
    rc = 0
    if n_df == 0:
        flag, rc = "  !! NO DOCKERFILES — every repo would score EBSR 0", 1
    elif unbuildable:
        flag, rc = f"  !! build context incomplete: {unbuildable}", 1
    n_skip = sum(1 for k in skip if k in canonical)
    word = "charged 0" if charge else "skipped"
    n = len(canonical) if charge else len(canonical) - n_skip
    print(f"{name:22s} {len(seen):3d} linked, {n_df:3d} dockerfiles, {n_skip} {word} "
          f"-> EBSR n={n}  {run_dir}{flag}")
    if len(roots) > 1:
        print(f"{'':22s} assembled from {len(sources)} roots: "
              + ", ".join(f"{v} from {os.path.basename(os.path.dirname(os.path.dirname(k)))}"
                          for k, v in sources.items()))
    if off_dataset:
        print(f"{'':22s} dropped {len(off_dataset)} off-dataset")
    return rc


def build_gold(repos: list, extra: list, out_path: str, *, include_partial: bool,
               repo_root: str, combined: str | None = None) -> int:
    """Emit one combined gold file covering exactly this dataset.

    Denominators come from the canonical combined manifest first, then from any --extra-gold
    files (e.g. a partial rat50new artifact). PARTIAL entries are EXCLUDED by default: a partial
    manifest's size is an UNDER-count, so dividing by it inflates that repo's ESSR toward the cap
    — a silent upward bias on exactly the repos with the weakest ground truth.
    """
    wanted = {r["full_name"].lower(): r["full_name"] for r in repos}
    corpora: dict = {}
    claimed: set = set()
    skipped: list = []

    def take(corpus: str, entries) -> None:
        block = corpora.setdefault(corpus, {"repos": {}})["repos"]
        for rec in (entries.values() if isinstance(entries, dict) else entries):
            key = (rec.get("full_name") or "").strip().lower()
            if key not in wanted or key in claimed:
                continue
            if not include_partial and rec.get("status") == "PARTIAL":
                skipped.append((wanted[key], corpus))
                continue
            if not isinstance(rec.get("manifest_size"), int) or rec["manifest_size"] <= 0:
                continue
            block[key] = {"full_name": rec.get("full_name"), "sha": rec.get("sha"),
                          "status": rec.get("status"), "manifest_size": rec["manifest_size"]}
            claimed.add(key)

    combined_path = combined or os.path.join(repo_root, _COMBINED)
    with open(combined_path, encoding="utf-8") as fh:
        for corpus, block in (json.load(fh).get("corpora") or {}).items():
            take(corpus, block.get("repos") or {})
    for path in extra:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        # An extra file may be either shape: a combined manifest with `corpora`, or a flat one with
        # `repos`. Reading only the flat key silently took ZERO denominators from a combined file.
        if doc.get("corpora"):
            for corpus, block in doc["corpora"].items():
                take(corpus, block.get("repos") or {})
        else:
            take(doc.get("dataset") or os.path.basename(path).removesuffix(".json"),
                 doc.get("repos") or {})

    corpora = {k: v for k, v in corpora.items() if v["repos"]}
    total = sum(len(v["repos"]) for v in corpora.values())
    doc = {"description": f"gold denominators covering {os.path.basename(out_path)}",
           "sources": [combined_path] + [os.path.abspath(p) for p in extra],
           "include_partial": include_partial,
           "corpora": corpora,
           "totals": {"repos_in_dataset": len(wanted), "repos_with_gold": total,
                      "repos_without_gold": sorted(wanted[k] for k in set(wanted) - claimed)}}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    print(f"\ngold  {total}/{len(wanted)} repos have a denominator -> {out_path}")
    for corpus, v in sorted(corpora.items()):
        print(f"        {len(v['repos']):3d}  {corpus}")
    if skipped:
        print(f"      excluded {len(skipped)} PARTIAL entr{'y' if len(skipped)==1 else 'ies'} "
              f"(pass --include-partial to score them): "
              + ", ".join(n for n, _ in skipped))
    if doc["totals"]["repos_without_gold"]:
        print(f"      !! {len(doc['totals']['repos_without_gold'])} repo(s) have NO gold and will "
              f"leave the ESSR mean:\n         "
              + "\n         ".join(doc["totals"]["repos_without_gold"]))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", action="append", default=[], metavar="NAME=DIR[,DIR...]",
                    help="arm name and the Graph2Env output root(s) to assemble it from")
    ap.add_argument("--dataset", required=True, help="dataset JSON the arms ran against")
    ap.add_argument("--runs-root", default="runs")
    ap.add_argument("--gold-out", help="write a dataset-scoped combined gold manifest here")
    ap.add_argument("--charge-unbuildable", action="store_true",
                    help="detect unbuildable repos as --skip-unbuildable does, but COUNT them as "
                         "EBSR 0 (status `missing`) instead of excluding them. This is the paper "
                         "convention: EBSR is the mean over ALL measured repos, so n stays constant "
                         "across arms. Detection still matters -- a Dockerfile wrapping an empty "
                         "setup.sh would otherwise BUILD and score a false green.")
    ap.add_argument("--combined", help="combined gold manifest to draw denominators from "
                                       f"(default: {_COMBINED})")
    ap.add_argument("--extra-gold", action="append", default=[],
                    help="additional gold manifest to draw denominators from")
    ap.add_argument("--skip-unbuildable", action="store_true",
                    help="mark repos that emitted no rebuildable environment (no Dockerfile, or "
                         "a Dockerfile wrapping an empty setup.sh) as `unmeasurable` so they leave "
                         "every denominator. The skip set is the UNION across all --arm arguments, "
                         "so every arm still scores over the SAME repos -- otherwise the arm that "
                         "crashed more often would also be judged on an easier subset.")
    ap.add_argument("--include-partial", action="store_true",
                    help="use PARTIAL gold entries as denominators (they under-count; see --help)")
    a = ap.parse_args(argv)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repos, ds_sha = _dataset(a.dataset)
    canonical = {r["full_name"].lower() for r in repos}
    arms = []
    for spec in a.arm:
        name, _, roots = spec.partition("=")
        if not (name and roots):
            ap.error(f"--arm must be NAME=DIR[,DIR...], got {spec!r}")
        arms.append((name.strip(), [r.strip() for r in roots.split(",") if r.strip()]))

    # Pass 1: which repos emitted no rebuildable environment.
    #
    # SKIP mode takes the UNION across arms, because excluding per-arm would hand the arm that
    # crashed more often a smaller, easier denominator and the delta would stop being paired.
    # CHARGE mode must NOT: every arm is already scored over the full dataset, so the union would
    # charge an arm 0 for a repo it actually built (scylladb builds under no_graph_context but not
    # under no_populate), understating it for its neighbour's failure.
    def unbuildable_in(roots: list) -> dict:
        found, present = {}, set()
        for root in roots:
            if not os.path.isdir(root):
                continue
            for full_name, src in _repo_dirs(root):
                key = full_name.lower()
                if key not in canonical:
                    continue
                present.add(key)
                why = unbuildable(src)
                if why:
                    found[key] = why
        for key in canonical - present:
            found.setdefault(key, "no output directory")
        return found

    per_arm: dict = {}
    skip: dict = {}
    if a.skip_unbuildable or a.charge_unbuildable:
        for name, roots in arms:
            per_arm[name] = unbuildable_in(roots)
        if a.charge_unbuildable:
            for name, _ in arms:
                print(f"charge  {name}: {len(per_arm[name])}/{len(canonical)} repos emitted no "
                      f"rebuildable environment -> scored EBSR 0, arm stays at n={len(canonical)}")
        else:
            for name, found in per_arm.items():
                for k, v in found.items():
                    skip.setdefault(k, f"{v} [{name}]")
            print(f"skip  {len(skip)}/{len(canonical)} repos unbuildable in at least one arm "
                  f"-> every arm scores over n={len(canonical) - len(skip)}")
            for k, v in sorted(skip.items()):
                print(f"        {k:<44} {v}")
        print()

    rc = 0
    for name, roots in arms:
        arm_skip = per_arm.get(name, {}) if a.charge_unbuildable else skip
        rc |= adapt_arm(name, roots, repos, a.dataset, ds_sha, a.runs_root,
                        skip=arm_skip, charge=a.charge_unbuildable)
    if a.gold_out:
        rc |= build_gold(repos, a.extra_gold, a.gold_out, combined=a.combined,
                         include_partial=a.include_partial, repo_root=repo_root)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
