# bench/report/error_report.py
from __future__ import annotations

from collections import Counter

from bench.errors import CATEGORIES, extract_events
from bench.schema import ArmErrorReport
from bench.verdict import DEFAULT_THRESHOLD, verdict


def row_events(row) -> tuple:
    """Collect-pass and run-pass events, tagged by source and never pooled (spec section 8)."""
    tl = row.repo_toplevel or ()
    lang = row.language or "python"
    return (extract_events(row.collect_errors, source="collect", repo_toplevel=tl, language=lang)
            + extract_events(row.run_failed_lines, source="run", repo_toplevel=tl, language=lang))


def arm_report(rows, *, source: str = "collect", turn_cap=None,
               threshold: float = DEFAULT_THRESHOLD) -> ArmErrorReport:
    """One report PER SOURCE (spec section 8)."""
    dupes = sorted({r.repo for r in rows if sum(1 for x in rows if x.repo == r.repo) > 1})
    if dupes:
        raise ValueError(f"arm_report: duplicate repo rows would corrupt repo-level counts: {dupes}")

    verdicts = {r.repo: verdict(r, turn_cap=turn_cap, threshold=threshold) for r in rows}
    buckets = Counter(v.bucket for v in verdicts.values())
    surfaces = Counter(v.error_surface for v in verdicts.values())

    token_repos, token_events = Counter(), Counter()
    cat_repos, cat_events, crosstab = Counter(), Counter(), Counter()
    groups: dict = {}
    n_events = n_uncat = 0

    for r in rows:
        v = verdicts[r.repo]
        if v.error_surface != "full":          # only admissible repos contribute events
            continue
        evs = tuple(e for e in row_events(r) if e.source == source)
        for e in evs:
            token_events[e.token] += 1
            cat_events[e.category] += 1
            n_events += 1
            n_uncat += (e.category == "uncategorized")
        for tok in {e.token for e in evs}:
            token_repos[tok] += 1
        for cat in {e.category for e in evs}:
            cat_repos[cat] += 1
            crosstab[f"{v.bucket}|{cat}"] += 1
        for cat, grp in {(e.category, e.group) for e in evs if e.group}:
            groups.setdefault(cat, Counter())[grp] += 1

    return ArmErrorReport(
        agent=(rows[0].agent if rows else ""), n_repos=len(rows), source=source,
        n_admissible=surfaces["full"], n_masked=surfaces["masked"],
        n_unobserved=surfaces["unobserved"],
        n_status_derived=sum(1 for v in verdicts.values() if v.status_derived),
        buckets=dict(buckets), surfaces=dict(surfaces),
        token_repos=dict(token_repos), token_events=dict(token_events),
        category_repos=dict(cat_repos), category_events=dict(cat_events),
        crosstab=dict(crosstab),
        top_groups={c: [list(x) for x in g.most_common(10)] for c, g in groups.items()},
        uncategorized_rate=round(n_uncat / n_events, 4) if n_events else 0.0)
