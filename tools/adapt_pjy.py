#!/usr/bin/env python3
"""Shim results_pjy/ into the harness's run layout so unified_bench can score it.

results_pjy/ is a SEPARATE evaluation pipeline's output (EBSR/DGSR + MTPR, computed by its own
scripts on Windows). Its per-repo artifacts happen to sit in exactly the shape bench.harvest
already reads -- <root>/<owner>/<repo>/Dockerfile -- so nothing needs re-implementing: point the
real measure stage at it and the numbers come out of the same code path as every other arm.

Three things stand between the two layouts, and this script does only those three:
  1. a `_run_manifest.json` naming the dataset, so bench.campaign can discover the run;
  2. a `_meta.json` per repo, because their token accounting uses three different key spellings;
  3. a symlink mirror, so neither 5GB is copied nor the source tree written into.

It deliberately does NOT invent scores. Rebuilding is the caller's next step:
    python -m bench.unified_bench --harvest <arm>=<run>/output --out <run>/measure ...
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

_COPY = re.compile(r"^\s*COPY\s+(.*)$", re.M)

_REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(_REPO_ROOT, "bench"))
sys.path.insert(0, _REPO_ROOT)
from bench.campaign import DATASET_GROUPS                      # noqa: E402
from producers.repo2run import rehome_dockerfile               # noqa: E402

# dataset file -> group, straight from the scorer. Deriving it here (rather than guessing from the
# filename) is what keeps rat_python50.json and rat_python50_new.json in separate run dirs.
_GROUP_OF = {ds: group for ds, (group, _role) in DATASET_GROUPS.items()}

# results_pjy dir -> (arm slug, dataset file). The RAT `(image)` arms ship a docker image
# reference and no Dockerfile at all, so they are listed but cannot be rebuilt; see _check.
ARMS = {
    "RatBench_Graph2Env":       ("pjy-graph2env", "rat_python50.json"),
    "EnvBench_Graph2Env":       ("pjy-graph2env", "envbench_python100.json"),
    "RatBench_Repo2Run":        ("pjy-repo2run", "rat_python50.json"),
    "EnvBench_Repo2Run":        ("pjy-repo2run", "envbench_python100.json"),
    "RAT_Python50_new_Repo2Run": ("pjy-repo2run", "rat_python50_new.json"),
    "RatBench_RAT(dockerfile)": ("pjy-rat-dockerfile", "rat_python50.json"),
    "RAT_Python50_new_RAT(dockerfile)": ("pjy-rat-dockerfile", "rat_python50_new.json"),
    "EnvBench_RAT(dockerfile)": ("pjy-rat-dockerfile", "envbench_python100.json"),
    "RatBench_RAT(image)":      ("pjy-rat-image", "rat_python50.json"),
    "EnvBench_RAT(image)":      ("pjy-rat-image", "envbench_python100.json"),
    os.path.join("ratbench_new50", "RAT_Python50_new_RAT_image"):
                                ("pjy-rat-image", "rat_python50_new.json"),
}

# Their three token spellings. Normalised to the harness's tokens_in/tokens_out.
_IN = ("input_tokens", "prompt_tokens")
_OUT = ("output_tokens", "completion_tokens")
_CALLS = ("api_calls", "request_count")


def _repo_dirs(output_root: str):
    """(full_name, source_dir) for every repo under an `output/` tree.

    Their writer flattens a repo whose owner and name match (peering-manager/peering-manager lands
    as output/peering-manager/Dockerfile), so a directory holding a Dockerfile and no subdirectory
    is that repo, not an owner.
    """
    for owner in sorted(os.listdir(output_root)):
        od = os.path.join(output_root, owner)
        if not os.path.isdir(od):
            continue
        subs = [s for s in sorted(os.listdir(od)) if os.path.isdir(os.path.join(od, s))
                and s not in ("patch", "search_patch")]
        if subs:
            for s in subs:
                yield f"{owner}/{s}", os.path.join(od, s)
        else:
            yield f"{owner}/{owner}", od


def _meta(src: str) -> dict:
    """tokens_in / tokens_out / turns_used, plus the harvest status.

    `status` matters: bench.harvest treats "produced" as measurable and anything else as a
    short-circuit. A repo their agent never got a Dockerfile out of must reach compute_metrics as
    a counted EBSR-0, not as an un-measured hole -- which is exactly what "missing" does.
    """
    m: dict = {"status": "produced" if os.path.isfile(os.path.join(src, "Dockerfile")) else "missing",
               "language": "python", "source": "results_pjy"}
    p = os.path.join(src, "token_usage.json")
    if not os.path.isfile(p):
        return m
    try:
        with open(p, encoding="utf-8") as fh:
            t = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        m["token_usage_error"] = repr(e)          # loud, not silent: a null token is a real finding
        return m
    if not isinstance(t, dict):
        m["token_usage_error"] = f"not an object: {type(t).__name__}"
        return m
    pick = lambda keys: next((t[k] for k in keys if isinstance(t.get(k), int)), None)
    m["tokens_in"], m["tokens_out"] = pick(_IN), pick(_OUT)
    m["llm_calls"] = pick(_CALLS)
    if isinstance(t.get("turn_count"), int):
        m["turns_used"] = t["turn_count"]
    return m


def _check(src: str, dest: str | None = None) -> list:
    """COPY sources the Dockerfile needs that this artifact bundle does not carry.

    A COPY goes unsatisfied when its source is absent from the bundle altogether -- RAT's
    dockerfile arms `COPY repo /repo`, a materialised checkout that was never shipped, and
    Repo2Run's `COPY search_patch /search_patch` on 10 envbench repos. Reported up front, because
    the alternative is build failures that look like the agent's fault.

    A DIRECTORY source is no longer flagged: bench.harvest._sibling_scripts walks it and emits
    `<dirname>/<relpath>` keys, so Repo2Run's `COPY patch /patch` now reaches the build context.
    That was 60 repos across the two RAT datasets, 35 of which build successfully.
    """
    dfp = os.path.join(src, "Dockerfile")
    if not os.path.isfile(dfp):
        return []
    with open(dfp, encoding="utf-8", errors="replace") as fh:
        df = fh.read()
    # `dest` is where the adapted run dir lives; a COPY source absent from the bundle may have been
    # reconstructed there by _materialise_repo, and is then satisfied.
    missing = []
    for args in _COPY.findall(df):
        parts = [p for p in args.split() if not p.startswith("--")]
        for s in parts[:-1]:
            base = os.path.basename(s)
            if os.path.exists(os.path.join(src, base)):
                continue
            if dest and os.path.exists(os.path.join(dest, base)):
                continue                          # reconstructed into the build context
            missing.append(s)
    return sorted(set(missing))


def _mirror(src: str, dest: str) -> None:
    """Symlink every entry of `src` into `dest`. Directories too -- bench.harvest only inlines
    file siblings of a COPY, so a `patch/` directory has to be reachable in the build context
    itself."""
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(src):
        link = os.path.join(dest, name)
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(os.path.abspath(os.path.join(src, name)), link)


def _copy_names(dockerfile: str) -> set:
    """Basenames of every COPY source in the Dockerfile."""
    out = set()
    for args in _COPY.findall(dockerfile):
        parts = [p for p in args.split() if not p.startswith("--")]
        out.update(os.path.basename(p) for p in parts[:-1])
    return out


def _output_root(pjy_root: str, arm_dir: str) -> str | None:
    """The repo tree inside a bundle, whichever layout it uses.

    Most bundles nest repos under `ebsr_evaluation/output`. RAT_Python50_new_RAT(dockerfile) puts
    them under `output/` directly (with a _run_config.json alongside). Hardcoding the first
    silently skipped that whole arm with "no ebsr_evaluation/output".
    """
    for rel in (("ebsr_evaluation", "output"), ("output",)):
        p = os.path.join(pjy_root, arm_dir, *rel)
        if os.path.isdir(p):
            return p
    return None


def _repo_revision(src: str) -> tuple:
    """(clone_url, commit) for a `COPY repo` checkout, from _ebsr_result.json.

    RAT's dockerfile arm records `source_revision.expected_commit` and verifies the tree it saved
    really was at that commit (`matches: true` on 48 of 50 RatBench repos). With the clone_url from
    `dataset_row` that is enough to reproduce the checkout exactly. Returns (None, None) when
    either is absent -- EnvBench_RAT(dockerfile) carries no clone_url at all, and guessing one at
    an unverified revision would fabricate an environment rather than reconstruct it.
    """
    p = os.path.join(src, "_ebsr_result.json")
    if not os.path.isfile(p):
        return (None, None)
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return (None, None)
    sr = d.get("source_revision") or {}
    commit = sr.get("expected_commit") or d.get("commit")
    url = (d.get("dataset_row") or {}).get("clone_url")
    # Each is returned independently: EnvBench_RAT records a commit with NO url, and the caller
    # fills the url from the dataset. Gating on both would discard the commit and skip the
    # artifact-vs-dataset agreement check that makes that fallback safe.
    return (url or None, commit or None)


def _materialise_repo(src: str, dest: str, dataset_row: dict | None = None,
                      timeout: int = 600) -> str | None:
    """Clone the fixed-revision checkout a `COPY repo` needs. Returns None on success, else why.

    RAT's Dockerfile says `COPY repo /repo` with the comment "the fixed-revision repository saved
    with this artifact" -- but the bundle does not carry it, so every such build fails on a missing
    path and scores EBSR 0 for a reason that is ours, not the agent's.

    Fails CLOSED: a checkout that does not land exactly on the pinned commit is deleted rather than
    handed to the build, because a silently-wrong revision is a fabricated result. Never overwrites
    a `repo` the bundle really did ship (Repo2Run's COPY sources are present).
    """
    dfp = os.path.join(src, "Dockerfile")
    if not os.path.isfile(dfp):
        return None
    with open(dfp, encoding="utf-8", errors="replace") as fh:
        names = _copy_names(fh.read())
    if "repo" not in names or os.path.exists(os.path.join(src, "repo")):
        return None                                   # not needed, or genuinely shipped

    url, commit = _repo_revision(src)
    row = dataset_row or {}
    # EnvBench_RAT(dockerfile) records a commit for all 100 repos but no clone_url. The DATASET is
    # the authoritative pin those runs were made against and carries both, and its commit matches
    # the artifact's on 100/100 (verified) -- so it supplies the URL, and the agreement is the
    # verification. Disagreement is refused: cloning either revision would misrepresent the run.
    if commit and row.get("commit") and row["commit"] != commit:
        return f"revision mismatch: artifact {commit[:12]} vs dataset {row['commit'][:12]}"
    url = url or row.get("clone_url")
    commit = commit or row.get("commit")
    if not (url and commit):
        return "no recorded revision (need a commit plus a clone_url from the artifact or dataset)"

    target = os.path.join(dest, "repo")
    if os.path.lexists(target):
        (shutil.rmtree if os.path.isdir(target) and not os.path.islink(target) else os.unlink)(target)
    try:
        subprocess.run(["git", "clone", "--filter=blob:none", "--no-checkout", url, target],
                       check=True, capture_output=True, timeout=timeout)
        subprocess.run(["git", "-C", target, "checkout", "--detach", commit],
                       check=True, capture_output=True, timeout=timeout)
        head = subprocess.run(["git", "-C", target, "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True, timeout=60).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        shutil.rmtree(target, ignore_errors=True)
        return f"clone/checkout failed at {commit[:12]}: {type(e).__name__}"
    if head != commit:
        shutil.rmtree(target, ignore_errors=True)
        return f"revision mismatch: wanted {commit[:12]}, got {head[:12]}"
    return None


def _write_dockerfile(src: str, dest: str) -> bool:
    """Write a REAL Dockerfile into `dest`, re-homed to /testbed when the artifact isn't already.

    The harness contract puts the repo at /testbed: contract.probe_testbed calls anything else
    `non_conforming`, and that status is in metrics._DISQUALIFIED, so the repo scores EBSR 0. Every
    Repo2Run Dockerfile homes its clone at /repo and none mentions /testbed, so adapting them
    verbatim scores the entire arm zero for a path convention.

    producers.repo2run.rehome_dockerfile is the same stanza the native repo2run variety uses
    (varieties.toml: `measure = "rehome"`); it detects the destination and falls back to /repo.

    Written as a real file, NOT symlinked like the rest of the bundle — a symlink would feed the
    original un-rehomed text to the build and silently undo this.
    """
    srcp = os.path.join(src, "Dockerfile")
    if not os.path.isfile(srcp):
        return False
    with open(srcp, encoding="utf-8", errors="replace") as fh:
        df = fh.read()
    if "/testbed" not in df:
        df = rehome_dockerfile(df)
    destp = os.path.join(dest, "Dockerfile")
    if os.path.lexists(destp):
        os.unlink(destp)
    with open(destp, "w", encoding="utf-8") as fh:
        fh.write(df)
    return True


def _dataset(datasets_dir: str, name: str) -> tuple:
    path = os.path.join(datasets_dir, name)
    with open(path, "rb") as fh:
        raw = fh.read()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def adapt(pjy_root: str, runs_root: str, datasets_dir: str) -> int:
    if not os.path.isdir(pjy_root):
        print(f"not a directory: {pjy_root}", file=sys.stderr)
        return 2
    rc = 0
    for arm_dir, (slug, ds_name) in sorted(ARMS.items()):
        out_root = _output_root(pjy_root, arm_dir)
        if out_root is None:
            print(f"skip {arm_dir}: no output tree (looked for ebsr_evaluation/output and output/)",
                  file=sys.stderr)
            continue
        rows, sha = _dataset(datasets_dir, ds_name)
        canonical = {r["full_name"].lower() for r in rows}
        by_name = {r["full_name"].lower(): r for r in rows}   # clone_url/commit fallback source

        # campaign.DATASET_GROUPS is the single source of truth for dataset -> group. The old
        # `"rat50" if ds_name.startswith("rat")` guess sent rat_python50_new.json to "rat50" too,
        # so the two RAT runs shared one run_dir and the second silently overwrote the first.
        group = _GROUP_OF.get(ds_name)
        if group is None:
            print(f"skip {arm_dir}: {ds_name} maps to no dataset group in campaign.DATASET_GROUPS",
                  file=sys.stderr)
            rc = 1
            continue
        run_dir = os.path.join(runs_root, slug, f"{group}-pjy")
        dest_out = os.path.join(run_dir, "output")

        n = n_df = reconstructed = 0
        unbuildable: dict = {}
        unreconstructed: dict = {}
        off_dataset = []
        for full_name, src in _repo_dirs(out_root):
            if full_name.lower() not in canonical:
                off_dataset.append(full_name)
                continue
            dest = os.path.join(dest_out, *full_name.split("/"))
            _mirror(src, dest)
            _write_dockerfile(src, dest)          # real file, re-homed to /testbed
            why = _materialise_repo(src, dest, dataset_row=by_name.get(full_name.lower()))
            if why:
                unreconstructed[full_name] = why
            elif os.path.isdir(os.path.join(dest, "repo")):
                reconstructed += 1
            with open(os.path.join(dest, "_meta.json"), "w", encoding="utf-8") as fh:
                json.dump(_meta(src), fh, indent=2)
            n += 1
            if os.path.isfile(os.path.join(src, "Dockerfile")):
                n_df += 1
            for miss in _check(src, dest):
                unbuildable[miss] = unbuildable.get(miss, 0) + 1

        manifest = {
            "variety": slug, "tier": "all", "adapted_from": os.path.join(arm_dir, "ebsr_evaluation"),
            "adapter": "tools/adapt_pjy.py",
            "dataset": {"path": f"datasets/{ds_name}", "repo_count": len(rows), "sha256": sha},
            "repos_linked": n, "repos_with_dockerfile": n_df,
            "repos_with_reconstructed_checkout": reconstructed,
            "unreconstructed_checkouts": unreconstructed,
            "missing_copy_sources": unbuildable,
            "off_dataset_dropped": sorted(off_dataset),
            "note": "Symlink mirror of a separate pipeline's artifacts. Scores come from "
                    "bench.unified_bench rebuilding these Dockerfiles, NOT from that pipeline's "
                    "own ebsr/MTPR JSONs.",
        }
        with open(os.path.join(run_dir, "_run_manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)

        flag = ""
        if n_df == 0:
            flag = "  !! NO DOCKERFILES — cannot be rebuilt; every repo would score EBSR 0"
            rc = 1
        elif unbuildable:
            flag = f"  !! build context incomplete: {unbuildable}"
            rc = 1
        recon = f", {reconstructed:3d} checkouts cloned" if reconstructed else ""
        print(f"{slug:20s} {group:12s} {n:3d} repos, {n_df:3d} dockerfiles{recon} -> {run_dir}{flag}")
        if unreconstructed:
            print(f"{'':20s} {'':12s} !! {len(unreconstructed)} checkout(s) not reconstructible: "
                  f"{list(unreconstructed.values())[0]}")
        if off_dataset:
            print(f"{'':20s} {'':12s} dropped {len(off_dataset)} off-dataset: {off_dataset[:3]}")
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pjy", default="results_pjy")
    ap.add_argument("--runs", default="runs")
    ap.add_argument("--datasets", default="datasets")
    a = ap.parse_args(argv)
    return adapt(a.pjy, a.runs, a.datasets)


if __name__ == "__main__":
    raise SystemExit(main())
