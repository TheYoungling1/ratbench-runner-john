#!/usr/bin/env python3
"""Pool two or more tools/dockerfile_quality.py outputs into one unified table.

Reuses the already-computed per_repo.csv files (no re-running hadolint/docker). Reports BOTH:
  - pooled:    every repo weighted equally (sum over all repos / total repo count)
  - macro_avg: mean of each dataset's own per-variety mean, weighted 50/50 by dataset

These differ whenever a variety's success rate (n_built, n_dockerfiles) differs across datasets:
a repo-weighted pool lets whichever dataset a variety happened to succeed on more dominate its
"unified" number, which is a property of that run's success rate, not of Dockerfile quality.
macro_avg is invariant to that. Report both rather than picking one silently.

Only sound when the input datasets are repo-disjoint (checked below) — otherwise pooling would
double-count a repo that appears in both.

USAGE
    python3 tools/combine_dockerfile_quality.py \\
        --in envbench100=runs/dockerfile_quality/per_repo.csv \\
        --in ratbench50=runs/dockerfile_quality_ratbench50/per_repo.csv \\
        --out runs/dockerfile_quality_combined
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict


def load(path: str, dataset: str) -> list[dict]:
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["dataset"] = dataset
    return rows


def check_disjoint(datasets: dict[str, list[dict]]) -> None:
    seen: dict[tuple[str, str], str] = {}
    for dataset, rows in datasets.items():
        for r in rows:
            key = (r["variety"], r["full_name"])
            if key in seen:
                raise SystemExit(f"repo overlap: {key} appears in both {seen[key]!r} and "
                                  f"{dataset!r} — pooling would double-count it")
            seen[key] = dataset


def _mean(rows: list[dict], field: str) -> float | None:
    vals = [float(r[field]) for r in rows if r.get(field) not in (None, "", "None")]
    return round(sum(vals) / len(vals), 2) if vals else None


def _top_codes(rows: list[dict], k: int = 5) -> str:
    totals: Counter = Counter()
    for r in rows:
        for pair in (r.get("top_codes") or "").split(";"):
            if ":" in pair:
                code, cnt = pair.rsplit(":", 1)
                totals[code] += int(cnt)
    return ", ".join(f"{c} ({n})" for c, n in totals.most_common(k))


_METRICS = ["image_size_mb", "hadolint_error", "hadolint_warning", "hadolint_total"]


def per_dataset_means(rows: list[dict]) -> dict[str, float | None]:
    n = len(rows)
    zero_error = sum(1 for r in rows if r.get("hadolint_error") == "0")
    out = {m: _mean(rows, m) for m in _METRICS}
    out["n_dockerfiles"] = n
    out["n_built"] = sum(1 for r in rows if r.get("build_ok") == "True")
    out["pct_zero_error"] = round(100 * zero_error / n, 1) if n else None
    return out


def combine(datasets: dict[str, list[dict]]) -> dict[str, dict]:
    by_variety: dict[str, dict[str, list[dict]]] = defaultdict(dict)
    for dataset, rows in datasets.items():
        for variety, vrows in groupby(rows, "variety").items():
            by_variety[variety][dataset] = vrows

    summary = {}
    for variety, per_dataset_rows in by_variety.items():
        all_rows = [r for rows in per_dataset_rows.values() for r in rows]
        per_ds_summaries = {ds: per_dataset_means(rows) for ds, rows in per_dataset_rows.items()}

        pooled = per_dataset_means(all_rows)
        macro = {}
        for m in _METRICS + ["pct_zero_error"]:
            vals = [s[m] for s in per_ds_summaries.values() if s[m] is not None]
            macro[m] = round(sum(vals) / len(vals), 2) if vals else None

        summary[variety] = {
            "n_by_dataset": {ds: s["n_dockerfiles"] for ds, s in per_ds_summaries.items()},
            "n_built_by_dataset": {ds: s["n_built"] for ds, s in per_ds_summaries.items()},
            "n_total": pooled["n_dockerfiles"],
            "pooled": {m: pooled[m] for m in _METRICS + ["pct_zero_error"]},
            "macro_avg": macro,
            "per_dataset": per_ds_summaries,
            "top_rule_codes": _top_codes(all_rows),
        }
    return summary


def groupby(rows: list[dict], field: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r[field]].append(r)
    return out


def write_markdown(summary: dict[str, dict], path: str) -> None:
    cols = ["variety", "n_total", "n_by_dataset",
            "pooled.image_size_mb", "macro_avg.image_size_mb",
            "pooled.hadolint_total", "macro_avg.hadolint_total",
            "pooled.pct_zero_error", "macro_avg.pct_zero_error"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for variety, s in sorted(summary.items()):
        vals = []
        for c in cols:
            if c == "variety":
                vals.append(variety)
            elif c == "n_total":
                vals.append(str(s["n_total"]))
            elif c == "n_by_dataset":
                vals.append(", ".join(f"{k}={v}" for k, v in s["n_by_dataset"].items()))
            else:
                section, _, field = c.partition(".")
                vals.append(str(s[section][field]))
        lines.append("| " + " | ".join(vals) + " |")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
        f.write("\n`pooled` = every repo weighted equally across both datasets. "
                "`macro_avg` = mean of each dataset's own per-variety mean (50/50 by dataset, "
                "immune to a variety's success rate differing across datasets).\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inputs", action="append", required=True,
                     metavar="DATASET=per_repo.csv")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    datasets = {}
    for spec in a.inputs:
        name, _, path = spec.partition("=")
        if not name or not path:
            print(f"bad --in spec: {spec!r} (want DATASET=per_repo.csv)", file=sys.stderr)
            return 2
        datasets[name] = load(path, name)

    check_disjoint(datasets)
    summary = combine(datasets)

    os.makedirs(a.out, exist_ok=True)
    all_rows = [r for rows in datasets.values() for r in rows]
    with open(os.path.join(a.out, "per_repo_combined.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        w.writerows(all_rows)
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    write_markdown(summary, os.path.join(a.out, "summary.md"))

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
