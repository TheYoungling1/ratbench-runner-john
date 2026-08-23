"""Stratified aggregate report for the e2e build-script eval. Pure. Headline =
first-pass env-works rate (overall + per stratum); plus the replay-ladder funnel,
attribution histogram, and gap clusters. tests_passed is reported with a loud
service/config confound caveat — never a gate.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.eval.language_package_eval.coverage import missing_node_clusters  # noqa: E402

_RUNGS = ("install_ok", "env_works", "collect_ok", "tests_ran", "tests_passed")
_TESTS_PASSED_CAVEAT = (
    "tests_passed is a CAVEATED diagnostic, never the headline: it depends on the "
    "service/config tier (live Postgres/Redis, fixtures, network) which is OUT OF "
    "SCOPE until service detection lands, so a low tests_passed is frequently not a "
    "graph fault. tests_ran is the clean, service-independent env-quality signal."
)


def _feasible(cards: list[dict]) -> list[dict]:
    return [c for c in cards if c.get("feasible")]


def _rate(cards: list[dict], key: str) -> tuple[int, int]:
    passed = sum(1 for c in cards if c.get(key))
    return (passed, len(cards))


def aggregate(scorecards: list[dict]) -> dict:
    """Headline + funnel + histogram + clusters. Headline denominator excludes
    infeasible repos; funnel/histogram count all scored repos."""
    feasible = _feasible(scorecards)
    strata = sorted({c.get("stratum") for c in scorecards})
    headline = {"overall": _rate(feasible, "first_pass_env_works")}
    for s in strata:
        headline[s] = _rate([c for c in feasible if c.get("stratum") == s], "first_pass_env_works")

    funnel = {rung: sum(1 for c in scorecards if c.get(rung)) for rung in _RUNGS}
    histogram = dict(Counter(c.get("attribution", "unknown") for c in scorecards))

    apt_safety = [
        {"repo": c.get("repo"), "stratum": c.get("stratum"), "predicted_apt": c.get("predicted_apt", [])}
        for c in scorecards
        if c.get("stratum") == "S_control" and c.get("predicted_apt")   # over-prediction on a control
    ]
    return {
        "headline_env_works": headline,
        "ladder_funnel": funnel,
        "attribution_histogram": histogram,
        "gap_clusters": list(missing_node_clusters(scorecards)),
        "control_overprediction": apt_safety,
        "n_scored": len(scorecards),
        "n_feasible": len(feasible),
    }


def _fmt_rate(pair) -> str:
    passed, total = pair
    return f"{passed}/{total} ({passed / total:.0%})" if total else "n/a (0 feasible)"


def render_report_md(agg: dict) -> str:
    lines = ["# E2E Build-Script Effectiveness Report", ""]
    lines.append(f"Corpus: {agg['n_scored']} scored ({agg['n_feasible']} feasible).")
    lines += ["", "## First-pass env-works (HEADLINE)", "", "| Scope | Rate |", "|---|---|"]
    lines.append(f"| overall | {_fmt_rate(agg['headline_env_works']['overall'])} |")
    for s in sorted(k for k in agg["headline_env_works"] if k != "overall"):
        lines.append(f"| {s} | {_fmt_rate(agg['headline_env_works'][s])} |")

    lines += ["", "## Replay-ladder funnel", "", "| Rung | Repos |", "|---|---|"]
    for rung in _RUNGS:
        lines.append(f"| {rung} | {agg['ladder_funnel'][rung]} |")
    lines += ["", f"> {_TESTS_PASSED_CAVEAT}"]

    lines += ["", "## Failure attribution", "", "| Label | Count |", "|---|---|"]
    for label, n in sorted(agg["attribution_histogram"].items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"| {label} | {n} |")

    lines += ["", "## Gap clusters (fix-next, ranked)", ""]
    if agg["gap_clusters"]:
        for i, c in enumerate(agg["gap_clusters"], 1):
            lines.append(f"{i}. **{c['tier']}** `{c['id']}` — {c['count']} repo(s): {', '.join(c['repos'])}")
    else:
        lines.append("(none)")

    lines += ["", "## Over-prediction on control repos (apt-safety)", ""]
    if agg["control_overprediction"]:
        for c in agg["control_overprediction"]:
            lines.append(f"- **{c['repo']}** predicted apt: {', '.join(c['predicted_apt'])} (control should be empty)")
    else:
        lines.append("(none — control strata predicted no apt)")

    lines.append("")
    return "\n".join(lines)
