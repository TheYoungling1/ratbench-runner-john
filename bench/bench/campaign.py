# bench/campaign.py
#
# Campaign-level scoring: point at ONE agent's run root (runs/<arm>/) and score every dataset it
# has run, without hand-picking directories.
#
# WHY THIS EXISTS
# ---------------
# `unified_bench --harvest arm=<run>/output --out <run>/measure` scores ONE run. But an arm's
# results live across several run dirs — rat50 in one, envbench100 either as a direct run or as
# an envbench70 + minus70 pair — alongside one-repo smokes, renamed duplicates and merges. Until
# now that mapping lived as hardcoded lists in tools/essr_gold.py, which go stale the moment
# anyone adds a run.
#
# Selection is driven by each run's own `_run_manifest.json`, which records the dataset it ran
# against (path + sha256). Every candidate considered is reported — the chosen one AND the
# rejected ones — so a wrong pick is auditable rather than silent.
from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass, field

# dataset file -> (group, role). `part` datasets are pooled to make up their group; `direct` ones
# cover it alone. envbench70 and minus70 are disjoint and together cover the canonical 100.
DATASET_GROUPS = {
    "rat_python50.json": ("rat50", "direct"),
    "rat_python50_new.json": ("rat50new", "direct"),
    "envbench_python100.json": ("envbench100", "direct"),
    "envbench_python70.json": ("envbench100", "part"),
    "envbench_python100_minus70.json": ("envbench100", "part"),
}

# Which corpus of the combined gold manifest each group's denominators come from.
GROUP_GOLD_CORPUS = {"rat50": "rat_python50", "rat50new": "rat_python50_new",
                     "envbench100": "envbench_python100"}

# The canonical repo list a group's score must be filtered to. envbench70 carries 9 repos that
# were never part of envbench_python100, so an unfiltered pool is 109 repos and is not a number
# about envbench100 at all.
GROUP_DATASET = {"rat50": "rat_python50.json", "rat50new": "rat_python50_new.json",
                 "envbench100": "envbench_python100.json"}

# Every group DATASET_GROUPS knows about, in declaration order. Derived rather than restated so
# adding a dataset above is the only edit a new corpus needs.
GROUPS = tuple(dict.fromkeys(g for g, _ in DATASET_GROUPS.values()))


@dataclass(frozen=True)
class RunInfo:
    path: str
    name: str
    dataset: str | None          # manifest's dataset basename; None when unreadable
    group: str | None            # "rat50" | "envbench100" | None when unrecognised
    role: str | None             # "direct" | "part" | None
    n_rows: int                  # measured row.json count — the smoke-vs-real signal


@dataclass
class Selection:
    group: str
    paths: list = field(default_factory=list)
    pooled: bool = False         # made from `part` runs rather than one direct run
    incomplete: bool = False     # pooled from fewer parts than the group needs
    ambiguous: bool = False      # a tie on row count was broken by name
    rejected: list = field(default_factory=list)   # candidates not chosen, for reporting


def discover_runs(agent_dir: str) -> list:
    """Every run directory under `agent_dir`, labelled by the dataset its manifest names.

    A directory with neither a manifest nor any measured row is not a run and is skipped. A
    corrupt manifest yields a RunInfo with dataset=None rather than an exception: the rows are
    still real, only the label is unknown, and a crash here would take down the whole campaign
    report over one bad file.
    """
    if not os.path.isdir(agent_dir):
        raise FileNotFoundError(f"agent run directory not found: {agent_dir}")

    out = []
    for name in sorted(os.listdir(agent_dir)):
        path = os.path.join(agent_dir, name)
        if not os.path.isdir(path):
            continue
        n_rows = sum(1 for _ in _row_paths(path))
        dataset = None
        mp = os.path.join(path, "_run_manifest.json")
        if os.path.isfile(mp):
            try:
                with open(mp, encoding="utf-8") as fh:
                    dataset = os.path.basename(str((json.load(fh).get("dataset") or {}).get("path") or "")) or None
            except (OSError, json.JSONDecodeError, AttributeError):
                dataset = None
        if dataset is None and n_rows == 0:
            continue                              # not a run at all
        group, role = DATASET_GROUPS.get(dataset or "", (None, None))
        out.append(RunInfo(path=path, name=name, dataset=dataset, group=group,
                           role=role, n_rows=n_rows))
    return out


def _row_paths(run_dir: str):
    measure = os.path.join(run_dir, "measure")
    for dirpath, _dirs, files in os.walk(measure):
        if "row.json" in files:
            yield os.path.join(dirpath, "row.json")


def _best(cands: list) -> tuple:
    """(winner, rejected, ambiguous). Most rows wins — that is what separates a real run from a
    one-repo smoke. A tie is broken by name so two machines agree, and flagged."""
    ordered = sorted(cands, key=lambda r: (-r.n_rows, r.name))
    winner = ordered[0]
    ambiguous = len(ordered) > 1 and ordered[1].n_rows == winner.n_rows
    return winner, ordered[1:], ambiguous


def select_runs(runs: list) -> dict:
    """{group: Selection} plus `ignored`, choosing one source set per dataset group."""
    sel: dict = {"ignored": [(r.path, r.dataset) for r in runs if r.group is None]}

    for group in GROUPS:
        members = [r for r in runs if r.group == group and r.n_rows > 0]
        if not members:
            continue
        s = Selection(group=group)
        direct = [r for r in members if r.role == "direct"]
        if direct:
            # A single run covering the whole dataset beats pooling: no cross-run harness-commit
            # boundary, and no filtering needed.
            win, rej, amb = _best(direct)
            s.paths = [win.path]
            s.rejected = [r.path for r in rej] + [r.path for r in members if r.role != "direct"]
            s.ambiguous = amb
        else:
            parts = {}
            for r in members:
                parts.setdefault(r.dataset, []).append(r)
            for ds, cands in sorted(parts.items()):
                win, rej, amb = _best(cands)
                s.paths.append(win.path)
                s.rejected += [r.path for r in rej]
                s.ambiguous = s.ambiguous or amb
            s.pooled = True
            # envbench100 needs BOTH parts. Half a pool is not the dataset.
            needed = {d for d, (g, role) in DATASET_GROUPS.items() if g == group and role == "part"}
            s.incomplete = set(parts) != needed
        sel[group] = s
    return sel


# ── scoring ───────────────────────────────────────────────────────────────────────────────

def load_group_rows(paths: list, canonical: set | None) -> list:
    """MeasureRows from every chosen run dir, filtered to `canonical` and de-duplicated.

    The filter is not optional bookkeeping: envbench70 carries 9 repos that were never part of
    envbench_python100, so an unfiltered pool of envbench70 + minus70 is 109 repos and is not a
    number about envbench100 at all. A repo appearing in two pooled runs is a sign the run list is
    wrong (the parts are supposed to be disjoint), so the duplicate is surfaced, not absorbed.
    """
    from bench.unified_bench import load_rows           # local: unified_bench imports this module

    seen: dict = {}
    dupes: list = []
    for p in paths:
        for rows in load_rows(os.path.join(p, "measure")).values():
            for r in rows:
                key = r.repo.lower()
                if canonical is not None and key not in canonical:
                    continue
                if key in seen:
                    dupes.append(r.repo)
                    continue
                seen[key] = r
    return list(seen.values()), sorted(set(dupes))


def economy(rows: list) -> dict:
    """Median tokens/turns/cost over `rows`, plus how many rows actually carried telemetry.

    MEDIAN, not mean: the paper's economy column is a median and the two differ a lot here — a
    handful of very heavy repos drag the mean up (metrics.py's `mean_tokens` is the mean and is
    NOT the published figure). Rows with no telemetry are EXCLUDED rather than counted as zero,
    so `n` must be published next to the median: SetupX carries tokens on 134 of 150 rows and a
    median over 89% of an arm's repos is not the same claim as one over all of them.
    """
    tok = [r.tokens_in + r.tokens_out for r in rows
           if r.tokens_in is not None and r.tokens_out is not None]
    turns = [r.turns_used for r in rows if r.turns_used is not None]
    cost = [r.cost_usd for r in rows if r.cost_usd is not None]
    med = lambda v: round(statistics.median(v), 2) if v else None   # noqa: E731
    return {"median_tokens": med(tok), "n_token_reporting": len(tok),
            "median_turns": med(turns), "n_turn_reporting": len(turns),
            "median_cost_usd": med(cost), "n_cost_reporting": len(cost),
            "n_rows": len(rows)}


def score_agent(agent_dir: str, *, gold_path: str | None = None, datasets_dir: str = "datasets",
                untrusted=None) -> dict:
    """Score every dataset group an arm has run. {group: {...}} plus `unified` and `selection`."""
    from bench.metrics import compute_metrics, load_gold_counts

    runs = discover_runs(agent_dir)
    sel = select_runs(runs)
    out: dict = {"agent": os.path.basename(os.path.abspath(agent_dir.rstrip("/"))),
                 "runs_found": len(runs), "ignored": sel["ignored"], "groups": {}}
    pool_src: dict = {}

    for group in GROUPS:
        s = sel.get(group)
        if s is None:
            continue
        canonical, n_canonical = None, None
        dpath = os.path.join(datasets_dir, GROUP_DATASET[group])
        if os.path.isfile(dpath):
            with open(dpath, encoding="utf-8") as fh:
                names = [r["full_name"].lower() for r in json.load(fh)]
            canonical, n_canonical = set(names), len(names)
        rows, dupes = load_group_rows(s.paths, canonical)

        gold = {}
        if gold_path:
            gold = load_gold_counts(gold_path, corpus=GROUP_GOLD_CORPUS[group])
        m = compute_metrics(rows, gold=gold or None, untrusted=untrusted)
        pool_src[group] = {"rows": rows, "gold": gold}
        out["groups"][group] = {
            "metrics": m,
            "sources": [os.path.basename(p) for p in s.paths],
            "rejected": [os.path.basename(p) for p in s.rejected],
            "pooled": s.pooled, "incomplete": s.incomplete, "ambiguous": s.ambiguous,
            "duplicate_repos": dupes,
            # n_measured vs n_expected is the coverage check the pooling trap needs: a pool that
            # silently lost repos, or a run that never attempted some, shows up here.
            "n_measured": m["n"], "n_expected": n_canonical,
            "economy": economy(rows),
        }

    # POOLED is the ONLY cross-dataset figure. The macro (mean of the two dataset means) and
    # weighted-150 conventions were REMOVED on 2026-09-15: "macro" already means per-repo inside a
    # single dataset, so reusing it for a mean-over-datasets gave one word two meanings in the same
    # paper, and the weighted form silently imputed the 4 gold-less repos at their dataset's mean.
    # Pooled is what the paper's own definition says — "the macro average over repositories" —
    # applied to all of them at once.
    pooled = pool_metrics(pool_src)
    out["pooled"] = None if pooled is None else {
        "EBSR": pooled["EBSR"], "ESSR": pooled["ESSR"],
        "n": pooled["n"], "n_ebsr": pooled["n_ebsr"], "n_gold_scored": pooled["n_gold_scored"],
        "economy": economy([r for b in pool_src.values() for r in b["rows"]]),
    }
    return out


def pool_metrics(groups: dict) -> dict | None:
    """One score over one pile of repos — the honest cross-dataset number.

    rat50 and envbench100 are DISJOINT (50 + 100 = 150 unique repos, overlap 0, verified), and so
    are their gold corpora (48 + 98 = 146, no key collisions). So there is no need to compute two
    means and reweight them: pool the rows, merge the gold, score once. Every repo counts exactly
    once, and the repos with no gold simply leave the ESSR mean instead of being imputed.

    `groups` is {group: {"rows": [...], "gold": {...}}}.
    """
    from bench.metrics import compute_metrics

    if not groups:
        return None
    rows, gold = [], {}
    for b in groups.values():
        rows += b["rows"]
        gold.update(b["gold"] or {})
    return compute_metrics(rows, gold=gold or None)
