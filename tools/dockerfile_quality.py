#!/usr/bin/env python3
"""Dockerfile quality appendix: hadolint score + image size per baseline.

Image size/digest is already computed by bench/measure.py into each repo's row.json
(schema.py: image_size_mb, image_delta_mb) — this script only reads it back. Hadolint is new:
it lints the Dockerfile TEXT hadolint sees in output/<owner>/<repo>/eval_build/Dockerfile, via
the pinned `hadolint/hadolint:v2.12.0` image over `docker run -i ... - < Dockerfile` so the
result doesn't depend on a local hadolint install or its version drifting between runs.

No `--ignore` list: default ruleset, so nothing is cherry-picked out of a baseline-comparison
score. Per-rule-code frequency is reported alongside the severity counts so a reader can see
what's actually driving a variety's numbers.

USAGE
    python3 tools/dockerfile_quality.py \\
        --run claudecode-dockerfile=runs/claudecode-dockerfile/envbench100-merged-20260915 \\
        --run setupx=runs/setupx/envbench100-merged-20260915 \\
        --run sweagent_repo2run_modern=runs/sweagent_repo2run_modern/envbench100-merged-20260915 \\
        --out runs/dockerfile_quality
    Writes <out>/per_repo.csv (one row per Dockerfile) and <out>/summary.md (one row per variety).
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import shlex
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

HADOLINT_IMAGE = "hadolint/hadolint:v2.12.0"
SEVERITIES = ("error", "warning", "info", "style")


def find_dockerfiles(run_dir: str) -> list[tuple[str, str]]:
    """[(full_name, dockerfile_path)] for every Dockerfile bench/measure builds under run_dir/output:
    eval_build/Dockerfile for harvested arms, else <owner>/<repo>/Dockerfile for the adapted ones
    (graph2env, pjy-*), which measure builds as-is (tools/adapt_graph2env.py, adapt_pjy.py)."""
    out = []
    output_root = os.path.join(run_dir, "output")
    for owner in sorted(os.listdir(output_root)) if os.path.isdir(output_root) else []:
        owner_dir = os.path.join(output_root, owner)
        if not os.path.isdir(owner_dir):
            continue
        for repo in sorted(os.listdir(owner_dir)):
            for dpath in (os.path.join(owner_dir, repo, "eval_build", "Dockerfile"),
                          os.path.join(owner_dir, repo, "Dockerfile")):
                if os.path.isfile(dpath):
                    out.append((f"{owner}/{repo}", dpath))
                    break
    return out


_COPY_SCRIPT = re.compile(r"^COPY\s+(\S+\.(?:sh|bash))\s+(\S+)\s*$")
_HEREDOC = "INLINED_SCRIPT_EOF"


_HEREDOC_OPEN = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")


def pad_heredoc_terminators(dockerfile: str) -> str:
    """Put a blank line after every heredoc terminator that is followed directly by more text.
    hadolint 2.12 rejects `EOF` + next instruction with DL1000, which the sweep would otherwise
    score as one finding for the whole file; the blank line is a no-op for docker build."""
    out, pending = [], []
    lines = dockerfile.splitlines(keepends=True)
    for k, line in enumerate(lines):
        out.append(line)
        if pending:
            if line.strip("\t\n") == pending[0]:
                pending.pop(0)
                if not pending and k + 1 < len(lines) and lines[k + 1].strip():
                    out.append("\n")
            continue
        pending = _HEREDOC_OPEN.findall(line)
    return "".join(out)


def unwrap_bash_c(script: str) -> str:
    """Replace each top-level `bash -c '<cmd>'` (possibly spanning lines) with <cmd>. hadolint
    and ShellCheck see a quoted `bash -c` argument as one opaque string, so a script made of
    them (setupx's replay) lints clean however its commands are written. Anything that doesn't
    tokenize to exactly [bash, -c, cmd] by end of script is kept verbatim."""
    lines = script.splitlines(keepends=True)
    out, i = [], 0
    while i < len(lines):
        if not lines[i].startswith("bash -c "):
            out.append(lines[i])
            i += 1
            continue
        for j in range(i, len(lines)):
            try:
                tokens = shlex.split("".join(lines[i:j + 1]))
            except ValueError:          # quote still open: the command continues on the next line
                continue
            if len(tokens) == 3 and tokens[:2] == ["bash", "-c"]:
                out.append(tokens[2].rstrip("\n") + "\n")
                i = j + 1
                break
            out.append(lines[i])        # balanced but not a lone `bash -c cmd`: leave it
            i += 1
            break
        else:
            out.extend(lines[i:])       # never balances: leave the rest untouched
            break
    return "".join(out)


def inline_scripts(dockerfile: str, context_dir: str) -> str:
    """Paste each build-time script (`COPY x.sh D` + a one-line `RUN ... bash D`) into the
    Dockerfile as a heredoc RUN, so hadolint's DL rules and ShellCheck see its commands the way
    they see commands other arms write inline. hadolint never opens COPY'd files, so without
    this an arm that keeps its per-repo work in a script (graph2env setup.sh, setupx replay)
    is scored on its wrapper alone. One RUN keeps the real layer structure (no DL3059 inflation).
    Scripts never run at build time (ENTRYPOINT) or absent from the context are left alone;
    an invocation this can't rewrite raises rather than silently scoring the wrapper only."""
    lines = dockerfile.splitlines(keepends=True)
    for i, line in enumerate(list(lines)):
        m = _COPY_SCRIPT.match(line.strip())
        script_path = m and os.path.join(context_dir, m.group(1))
        if not m or not os.path.isfile(script_path):
            continue
        dest = m.group(2) + os.path.basename(m.group(1)) if m.group(2).endswith("/") else m.group(2)
        call = re.compile(rf"(?<=[\s;&|(])(?:bash|sh)\s+{re.escape(dest)}(?=\s|$|;|&)")
        hits = [j for j, l in enumerate(lines) if j != i and call.search(l)]
        if not hits:
            continue
        j = hits[0]
        run = lines[j].rstrip("\n")
        if len(hits) > 1 or not run.startswith("RUN ") or run.endswith("\\") \
                or lines[j - 1].rstrip("\n").endswith("\\"):
            raise ValueError(f"can't inline {m.group(1)}: invoked as {run!r}")
        with open(script_path) as f:
            script = unwrap_bash_c(f.read())
        if _HEREDOC in script:
            raise ValueError(f"can't inline {m.group(1)}: contains {_HEREDOC}")
        before, after = call.split(run, maxsplit=1)
        before = before.removeprefix("RUN").strip().removesuffix("&&").strip()
        after = after.strip().removeprefix("&&").strip()
        body = "".join(f"{s}\n" for s in (before,) if s) + script.rstrip("\n") + "\n" \
            + "".join(f"{s}\n" for s in (after,) if s)
        # hadolint 2.12 fails to parse (DL1000) a heredoc terminator followed directly by the
        # next instruction; a blank line after it is valid Dockerfile syntax either way.
        lines[j] = f"RUN <<{_HEREDOC}\n{body}{_HEREDOC}\n\n"
        lines[i] = ""
    return "".join(lines)


def run_hadolint(dockerfile: bytes) -> list[dict] | None:
    """hadolint's JSON findings for one Dockerfile, via the pinned docker image on stdin.
    None on any docker/hadolint failure or timeout, so a failed lint is never read as a clean
    file. The timeout is generous: an inlined 70 KB replay script takes ~30s on its own."""
    try:
        p = subprocess.run(
            ["docker", "run", "--rm", "-i", HADOLINT_IMAGE,
             "hadolint", "--format", "json", "-"],
            input=dockerfile, capture_output=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError):
        return None
    try:
        return json.loads(p.stdout.decode())
    except json.JSONDecodeError:
        return None


def load_row(run_dir: str, variety: str, full_name: str) -> dict:
    """measure/<arm>/<full_name>/row.json. <arm> is the measured variety name, which need not
    match the --run label (graph2env measures as g2e-<dataset>), so match any single arm dir."""
    paths = glob.glob(os.path.join(run_dir, "measure", "*", full_name, "row.json"))
    if len(paths) > 1:
        raise SystemExit(f"{variety} {full_name}: ambiguous measure rows {paths}")
    if not paths:
        return {}
    with open(paths[0]) as f:
        return json.load(f)


def score_repo(variety: str, run_dir: str, full_name: str, dpath: str,
               inline: bool = False) -> dict:
    with open(dpath) as f:
        text = f.read()
    if inline:
        text = inline_scripts(text, os.path.dirname(dpath))
    findings = run_hadolint(pad_heredoc_terminators(text).encode())
    if findings is None:
        return {"variety": variety, "full_name": full_name, "unparseable": f"{dpath} (lint failed)"}
    if any(f.get("code") == "DL1000" for f in findings):
        return {"variety": variety, "full_name": full_name, "unparseable": f"{dpath} (DL1000)"}
    sev_counts = Counter(f.get("level", "unknown") for f in findings)
    code_counts = Counter(f.get("code", "unknown") for f in findings)
    row = load_row(run_dir, variety, full_name)
    return {
        "variety": variety,
        "full_name": full_name,
        "image_size_mb": row.get("image_size_mb"),
        "image_delta_mb": row.get("image_delta_mb"),
        "build_ok": row.get("build_ok"),
        "hadolint_error": sev_counts.get("error", 0),
        "hadolint_warning": sev_counts.get("warning", 0),
        "hadolint_info": sev_counts.get("info", 0),
        "hadolint_style": sev_counts.get("style", 0),
        "hadolint_total": sum(sev_counts.values()),
        "top_codes": ";".join(f"{c}:{n}" for c, n in code_counts.most_common()),
    }


def summarize(rows: list[dict]) -> dict[str, dict]:
    by_variety: dict[str, list[dict]] = {}
    for r in rows:
        by_variety.setdefault(r["variety"], []).append(r)
    summary = {}
    for variety, vrows in by_variety.items():
        n = len(vrows)
        sizes = [r["image_size_mb"] for r in vrows if r["image_size_mb"] is not None]
        zero_error = sum(1 for r in vrows if r["hadolint_error"] == 0)
        code_totals: Counter = Counter()
        for r in vrows:
            for pair in r["top_codes"].split(";"):
                if ":" in pair:
                    code, cnt = pair.rsplit(":", 1)
                    code_totals[code] += int(cnt)
        summary[variety] = {
            "n_dockerfiles": n,
            "n_built": sum(1 for r in vrows if r["build_ok"]),
            "mean_image_size_mb": round(sum(sizes) / len(sizes), 1) if sizes else None,
            "mean_hadolint_error": round(sum(r["hadolint_error"] for r in vrows) / n, 2),
            "mean_hadolint_warning": round(sum(r["hadolint_warning"] for r in vrows) / n, 2),
            "mean_hadolint_total": round(sum(r["hadolint_total"] for r in vrows) / n, 2),
            "pct_zero_error": round(100 * zero_error / n, 1),
            "top_rule_codes": ", ".join(f"{c} ({n})" for c, n in code_totals.most_common(5)),
        }
    return summary


def write_csv(rows: list[dict], path: str) -> None:
    if not rows:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_markdown(summary: dict[str, dict], path: str) -> None:
    cols = ["variety", "n_dockerfiles", "n_built", "mean_image_size_mb",
            "mean_hadolint_error", "mean_hadolint_warning", "mean_hadolint_total",
            "pct_zero_error", "top_rule_codes"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for variety, s in sorted(summary.items()):
        lines.append("| " + " | ".join(str(s.get(c, variety if c == "variety" else "")) for c in cols) + " |")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", required=True, metavar="VARIETY=RUN_DIR",
                     help="Repeatable. e.g. --run setupx=runs/setupx/envbench100-merged-20260915")
    ap.add_argument("--out", required=True, help="Output directory for per_repo.csv + summary.md")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--inline-scripts", action="store_true",
                     help="lint build-time COPY'd scripts as heredoc RUNs (see inline_scripts)")
    a = ap.parse_args()

    targets = []
    for spec in a.run:
        variety, _, run_dir = spec.partition("=")
        if not variety or not run_dir:
            print(f"bad --run spec: {spec!r} (want VARIETY=RUN_DIR)", file=sys.stderr)
            return 2
        if not os.path.isdir(run_dir):
            print(f"no such run dir: {run_dir}", file=sys.stderr)
            return 2
        for full_name, dpath in find_dockerfiles(run_dir):
            targets.append((variety, run_dir, full_name, dpath))

    print(f"linting {len(targets)} Dockerfiles across {len(a.run)} varieties...", file=sys.stderr)
    rows = []
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        futs = {ex.submit(score_repo, v, rd, fn, dp, a.inline_scripts): fn
                for v, rd, fn, dp in targets}
        for i, fut in enumerate(as_completed(futs), 1):
            rows.append(fut.result())
            if i % 10 == 0:
                print(f"  {i}/{len(targets)}", file=sys.stderr)

    os.makedirs(a.out, exist_ok=True)
    # hadolint failed or can't parse these; scoring them would count the file as 0 or 1 findings
    unparseable = sorted(f"{r['variety']}\t{r['full_name']}\t{r['unparseable']}"
                         for r in rows if "unparseable" in r)
    rows = [r for r in rows if "unparseable" not in r]
    with open(os.path.join(a.out, "parse_errors.txt"), "w") as f:
        f.write("".join(f"{u}\n" for u in unparseable))
    if unparseable:
        print(f"excluded {len(unparseable)} unparseable Dockerfile(s): see parse_errors.txt",
              file=sys.stderr)
    write_csv(sorted(rows, key=lambda r: (r["variety"], r["full_name"])),
              os.path.join(a.out, "per_repo.csv"))
    summary = summarize(rows)
    write_markdown(summary, os.path.join(a.out, "summary.md"))
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
