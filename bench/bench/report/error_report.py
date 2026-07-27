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


def category_status(rows, universe, *, source: str = "collect", turn_cap=None,
                    threshold: float = DEFAULT_THRESHOLD) -> dict:
    """category -> {present, absent, unknown} over ALL rows (spec section 7). A masked repo is
    PARTIALLY observed: its startup cause is `present`, every other category `unknown`."""
    out = {c: {"present": 0, "absent": 0, "unknown": 0} for c in universe}
    for r in rows:
        v = verdict(r, turn_cap=turn_cap, threshold=threshold)
        known = {e.category for e in row_events(r) if e.source == source}
        for c in universe:
            if c in known:
                out[c]["present"] += 1
            elif v.error_surface == "full":
                out[c]["absent"] += 1
            else:
                out[c]["unknown"] += 1
    return out


def _universe(rows_a, rows_b, *, source: str) -> tuple:
    cats = set(CATEGORIES)
    for rows in (rows_a, rows_b):
        for r in rows:
            cats |= {e.category for e in row_events(r) if e.source == source}
    return tuple(sorted(cats))


def interval_delta(rows_a, rows_b, *, source: str = "collect", turn_cap=None,
                   threshold: float = DEFAULT_THRESHOLD) -> dict:
    """Manski-style bounds. `identified` means the interval excludes zero, so the direction holds
    no matter what the masked repos were hiding."""
    kw = dict(source=source, turn_cap=turn_cap, threshold=threshold)
    uni = _universe(rows_a, rows_b, source=source)
    sa, sb = category_status(rows_a, uni, **kw), category_status(rows_b, uni, **kw)
    out = {}
    for c in uni:
        a, b = sa[c], sb[c]
        lower = b["present"] - (a["present"] + a["unknown"])
        upper = (b["present"] + b["unknown"]) - a["present"]
        out[c] = {"a": a, "b": b, "lower": lower, "upper": upper,
                  "identified": lower > 0 or upper < 0}
    return out


def masking_2x2(rows_a, rows_b, *, turn_cap=None, threshold: float = DEFAULT_THRESHOLD) -> dict:
    """Marginals are compatible with repos REGRESSING; report cells. Only repos present in BOTH
    arms can be cross-tabulated — a repo missing from one arm is reported separately rather than
    silently counted as 'not masked'."""
    def surf(rows):
        return {r.repo: verdict(r, turn_cap=turn_cap, threshold=threshold).error_surface
                for r in rows}
    sa, sb = surf(rows_a), surf(rows_b)
    both = set(sa) & set(sb)
    cells = Counter((sa[repo] == "masked", sb[repo] == "masked") for repo in both)
    return {"neither_masked": cells[(False, False)], "a_only_masked": cells[(True, False)],
            "b_only_masked": cells[(False, True)], "both_masked": cells[(True, True)],
            "only_in_a": len(set(sa) - set(sb)), "only_in_b": len(set(sb) - set(sa))}


def paired_repos(rows_a, rows_b, *, turn_cap=None, threshold: float = DEFAULT_THRESHOLD):
    """SENSITIVITY ONLY — conditioning on error_surface is post-treatment (spec section 7)."""
    def full(rows):
        return {r.repo for r in rows
                if verdict(r, turn_cap=turn_cap, threshold=threshold).error_surface == "full"}
    return frozenset(full(rows_a) & full(rows_b))


def _diff(da: dict, db: dict) -> dict:
    return {k: db.get(k, 0) - da.get(k, 0) for k in sorted(set(da) | set(db))}


def delta(rows_a, rows_b, *, source: str = "collect", turn_cap=None,
          threshold: float = DEFAULT_THRESHOLD) -> dict:
    kw = dict(turn_cap=turn_cap, threshold=threshold)
    fa = arm_report(rows_a, source=source, **kw)
    fb = arm_report(rows_b, source=source, **kw)
    paired = paired_repos(rows_a, rows_b, **kw)
    pa = arm_report([r for r in rows_a if r.repo in paired], source=source, **kw)
    pb = arm_report([r for r in rows_b if r.repo in paired], source=source, **kw)
    return {
        "source": source,
        "n_repos": {"a": fa.n_repos, "b": fb.n_repos},
        "category_interval": interval_delta(rows_a, rows_b, source=source, **kw),   # HEADLINE
        "masking_2x2": masking_2x2(rows_a, rows_b, **kw),
        "surfaces": {"a": fa.surfaces, "b": fb.surfaces},
        "buckets": {"a": fa.buckets, "b": fb.buckets},
        "status_derived": {"a": fa.n_status_derived, "b": fb.n_status_derived},
        "uncategorized_rate": {"a": fa.uncategorized_rate, "b": fb.uncategorized_rate},
        "sensitivity_paired": {
            "paired_repos": len(paired),
            "category_repos_delta": _diff(pa.category_repos, pb.category_repos),
            "token_repos_delta": _diff(pa.token_repos, pb.token_repos),
        },
    }
