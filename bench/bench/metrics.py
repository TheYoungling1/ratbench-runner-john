# bench/metrics.py
from __future__ import annotations

import json
import os
import statistics
from collections import Counter

# from bench.gold import gold_coverage  # golden-set calc disabled for now (no gold JSON)
from bench.schema import MeasureRow

# Statuses that DISQUALIFY a row from EBSR credit (design §2.4). A row is "conforming" (repo genuinely
# present at /testbed, build ok) iff its status is NOT one of these. `unmeasurable` is handled
# separately — excluded from the denominator entirely, never a 0 for a method.
_DISQUALIFIED = ("non_conforming", "empty_testbed", "build_fail", "missing", "measure_error", "error",
                 "config_restore_failed")

# Repos whose MEASURED outcome is not evidence about the pinned repo, for any arm.
#
# `pypa/distutils`: its certified commit is unreachable upstream, so no arm can legitimately build
# the commit under test — yet sweagent_repo2run_modern scored status=executed / collect_clean=True
# on it, which can only mean it built a DIFFERENT, reachable commit. Trusting that result would
# credit one arm for solving a repo none of them could have solved.
#
# This is NOT the same as "no gold count". A no-gold repo (neurogym, Checkmk, mlflow) is one WE
# failed to certify — not the arm's failure — so it keeps its measured EBSR and simply leaves the
# ESSR mean. An untrusted repo is denied EBSR credit and STAYS IN the denominator, so n is
# constant across arms and the penalty is uniform; and it leaves the ESSR mean, because scoring it
# 0 there would assert a failure rather than an absence of evidence.
UNTRUSTED_REPOS = frozenset({"pypa/distutils"})


def _conforming(r: MeasureRow) -> bool:
    return r.status not in _DISQUALIFIED


def _r(x: float) -> float:
    return round(x, 4)


def _div(num: float, den: float) -> float:
    return _r(num / den) if den else 0.0


def _mean_opt(vals: list) -> float | None:
    xs = [v for v in vals if v is not None]
    return _r(sum(xs) / len(xs)) if xs else None


def compute_metrics(rows: list[MeasureRow], gold: dict | None = None,
                    population: list | None = None, untrusted=None) -> dict:
    # `unmeasurable` (non-producers: live-claude, sweagent) and `legacy_missing` (pre-contract runs
    # with no Dockerfile) are excluded from EVERY denominator — neither must emit an EBSR-0 that
    # shadows a method's real score (design §2.4/§2.5).
    untrusted = {u.lower() for u in (UNTRUSTED_REPOS if untrusted is None else untrusted)}
    prod = [r for r in rows if r.status not in ("unmeasurable", "legacy_missing")]
    n = len(prod)
    n_unmeasurable = sum(1 for r in rows if r.status == "unmeasurable")
    n_legacy_missing = sum(1 for r in rows if r.status == "legacy_missing")
    ex = [r for r in prod if r.executed]
    n_exec = len(ex)
    n_collect_clean = sum(1 for r in prod if r.collect_clean)   # collect-only exit code in {0,5}
    # real_success is EBSR credit plus a high pass rate; an untrusted build cannot claim either.
    n_real = sum(1 for r in prod
                 if r.ebsr and r.pass_rate >= 0.8 and r.repo.lower() not in untrusted)
    micro_passed = sum(r.passed for r in ex)
    micro_total = sum(max(r.total - r.skipped, 0) for r in ex)

    # EBSR credit uses the per-language gate outcome (collect_clean), so a language whose clean gate
    # is NOT rc-5 (e.g. compiled: rc==0 only) is scored correctly. For Python collect_clean == (rc in
    # {0,5}), so this is byte-identical. `n_raw` below stays the Repo2Run rc-{0,5} parity diagnostic.
    n_ebsr = sum(1 for r in prod if _conforming(r) and r.collect_clean
                 and r.repo.lower() not in untrusted)
    # Diagnostic: the OLD ungated gate (what M3/paper reported). The delta is the removed false-green.
    n_raw = sum(1 for r in prod if r.collect_rc in (0, 5))
    # Diagnostic ONLY — deliberately does NOT change n_ebsr, so no existing number moves.
    # Python's clean gate is rc in {0,5}, and rc 5 means "no tests collected". Every repo in these
    # datasets HAS tests (`test_count` in the dataset file), so a row with EBSR credit and zero
    # collected tests is a hollow pass: the image built and /testbed is a real worktree, but nothing
    # ran. `n_real_success` already excludes it (it needs pass_rate >= 0.8), yet the headline EBSR
    # still counts it. Surfacing the count makes that visible instead of averaged in; if it is ever
    # non-zero, decide deliberately whether EBSR should require collected > 0.
    n_ebsr_zero_collected = sum(1 for r in prod
                                if _conforming(r) and r.collect_clean
                                and r.repo.lower() not in untrusted
                                and not r.collected_node_ids)

    out = {
        "n": n, "n_exec": n_exec, "n_collect_clean": n_collect_clean,
        "n_real_success": n_real, "n_unmeasurable": n_unmeasurable,
        "n_legacy_missing": n_legacy_missing,
        # EBSR (option (a)): conforming AND Repo2Run collect gate (rc in {0,5}).
        "EBSR": _div(n_ebsr, n), "n_ebsr": n_ebsr,
        # Audit diagnostics: the ungated Repo2Run gate + the false-green removed by the /testbed guard.
        "EBSR_repo2run_raw": _div(n_raw, n), "n_raw": n_raw,
        "false_green_removed": _div(n_raw - n_ebsr, n),
        # Every disqualified reason named, over the FULL row set (incl. unmeasurable).
        "status_census": dict(Counter(r.status for r in rows)),
        # EBSR collection diagnostics: tests collected + collection errors (over measured repos).
        "n_ebsr_zero_collected": n_ebsr_zero_collected,
        "total_collected": sum(len(r.collected_node_ids) for r in prod),
        "mean_collected": _div(sum(len(r.collected_node_ids) for r in prod), n),
        "total_collect_errors": sum(r.collect_error_count for r in prod),
        "mean_collect_errors": _div(sum(r.collect_error_count for r in prod), n),
        # ESSR is GOLD-ANCHORED ONLY — see gold_essr() below, merged into this dict at the end.
        # The ÷exec and ÷all variants that used to live here were REMOVED (2026-09-15): both
        # divided by the agent's OWN pytest collection, so an environment that collected 12 tests
        # and passed 11 scored 0.92, identical to one that collected 1400 and passed 1288. Keeping
        # three differently-denominated numbers all called "ESSR" was a standing source of
        # mis-citation — the paper's table quoted one and its method section defined another.
        "real_success": _div(n_real, n),
        "micro": _div(micro_passed, micro_total),
        "full_pass_repos": sum(1 for r in ex if r.pass_rate >= 0.999),
        "coverage": _div(n_exec, n),
    }
    # THE ESSR. Requires gold: with no manifest `ESSR` is None, which is the honest answer —
    # there is no denominator to divide by. A None here means "not scored", never "scored zero";
    # the key is always present so the two stay distinguishable.
    #
    # NOTE the node-id-intersection metric in bench/gold.py (EBSR_improved/ESSR_improved) is a
    # DIFFERENT, still-dormant metric. This one is the scalar-ratio form the paper's table uses.
    out.update(gold_essr(prod, gold or {}, population=population, untrusted=untrusted))

    # Economy metrics are computed over `prod` too (unmeasurable rows carry no rebuildable env).
    tok_rows = [r for r in prod if r.tokens_in is not None and r.tokens_out is not None]
    tok_total = sum(r.tokens_in + r.tokens_out for r in tok_rows)
    # Cost is reported directly by the agent (Claude Code's total_cost_usd), not derived from
    # tokens and a rate card — Claude Code mixes models within one run, so a single rate would
    # mis-price it. Rows from producers whose API reports no cost stay None and are EXCLUDED
    # from the denominator rather than counted as free.
    cost_rows = [r for r in prod if r.cost_usd is not None]
    cost_total = sum(r.cost_usd for r in cost_rows)
    n_build_ok = sum(1 for r in prod if r.build_ok)
    n_setup_compile = sum(1 for r in prod if r.setup_compile_ok)
    n_unreplayed = sum(1 for r in prod if r.meta.get("unreplayed"))

    out.update({
        "mean_image_delta_mb": _mean_opt([r.image_delta_mb for r in prod]),
        "mean_installed_pkgs": _mean_opt([r.installed_pkg_count for r in prod]),
        "mean_tokens": _r(tok_total / len(tok_rows)) if tok_rows else None,
        "mean_tokens_out": _mean_opt([r.tokens_out for r in tok_rows]) if tok_rows else None,
        "tokens_per_ebsr": _r(tok_total / n_ebsr) if (tok_rows and n_ebsr) else None,
        "tokens_per_real_success": _r(tok_total / n_real) if (tok_rows and n_real) else None,
        "mean_turns": _mean_opt([r.turns_used for r in prod]),
        "total_cost_usd": _r(cost_total) if cost_rows else None,
        "mean_cost_usd": _r(cost_total / len(cost_rows)) if cost_rows else None,
        "cost_per_ebsr": _r(cost_total / n_ebsr) if (cost_rows and n_ebsr) else None,
        "cost_per_real_success": _r(cost_total / n_real) if (cost_rows and n_real) else None,
        "n_cost_reporting": len(cost_rows),
        "mean_produce_s": _mean_opt([r.produce_s for r in prod]),
        "wall_s_per_real_success": (
            _r(sum((r.produce_s or 0) + (r.build_s or 0) + (r.test_s or 0) for r in prod) / n_real)
            if n_real else None),
        "n_token_reporting": len(tok_rows),
        "rebuild_ok_rate": _div(n_build_ok, n),
        # setup.sh execution-success rate: fraction of produced repos whose initial construction
        # setup.sh ran to rc 0 (>= rebuild_ok_rate; credits builds that only failed a later step).
        "setup_compile_rate": _div(n_setup_compile, n),
        "n_setup_compile": n_setup_compile,
        "unreplayed_rate": _div(n_unreplayed, n),
    })
    return out


# ── gold-anchored ESSR ────────────────────────────────────────────────────────────────────────
#
# Ported from tools/essr_gold.py, which is what produced the paper's ESSR column.
#
#     ESSR_r = min(passed_r / D_r, 1.0)        ESSR_gold = mean(ESSR_r)     # MACRO
#
# D_r is the independently certified manifest size. The ÷exec ESSR above divides by the AGENT'S
# OWN collection, so an environment that collects 12 tests and passes 11 scores 0.92 — identical
# to one that collects 1400 and passes 1288. Anchoring removes that.
#
# MACRO, not micro, deliberately: a 20943-test repo counts exactly as much as a 16-test one, so a
# single huge suite cannot dominate.
#
# Scalar ratio, NOT node-id intersection (which is what bench/gold.py does). Gold `node_ids` is
# itself the intersection of two certification runs, built to reject flaky tests, so a run can
# legitimately pass real tests outside it; matching would be stricter but is not what the
# published numbers use.

# A status in this set means the row was excluded from every denominator upstream — it is not a
# failure of the arm and must not be scored 0.
_NOT_A_ROW = ("unmeasurable", "legacy_missing")


def _gold_size(entry) -> int | None:
    """The usable denominator from a gold entry, or None. Guards the whole class of bad inputs:
    a missing size, a string that looks like a number, a float, zero, or a negative."""
    size = entry.get("size") if isinstance(entry, dict) else entry
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    return size


def gold_essr(rows: list, gold: dict, population: list | None = None, untrusted=None) -> dict:
    """Gold-anchored ESSR plus the diagnostics that keep it honest.

    Scoring rules, each of which is a decision rather than an accident:
      * a repo that never built scores 0.0 and STAYS IN the mean — that is the point of anchoring;
      * a repo in the run with no gold count is EXCLUDED, not zeroed — a gap in our ground truth
        is not a failure of the arm;
      * a repo in gold that the arm never produced scores 0.0 and stays in — that IS a failure of
        the arm — but is counted separately from one that ran and passed nothing;
      * passed > D_r is capped at 1.0 and reported, never silently.

    `population` is the repo list the arm was SUPPOSED to cover (the dataset). It decides who a
    missing row is charged to. With a dataset, a gold repo the arm never produced scores 0 — the
    arm failed it. WITHOUT one the population is the rows present, because otherwise a one-repo
    `--only` smoke would score the other 47 gold repos 0.0 and report ESSR 0.0208 for a repo that
    actually passed everything. metrics.py runs automatically on every run, smokes included.
    """
    untrusted = {u.lower() for u in (UNTRUSTED_REPOS if untrusted is None else untrusted)}
    by_repo = {r.repo.lower(): r for r in rows if r.status not in _NOT_A_ROW}
    if population is None:
        names = [n for n in gold if n in by_repo]
    else:
        names = [n for n in (p.lower() for p in population) if n in gold]

    per_repo, capped, excluded, no_row, sha_bad = {}, [], [], [], []
    cov, hollow = [], []
    for name in names:
        if name in untrusted:
            # Excluded, not zeroed: "ESSR = the mean of the other repos". A 0 would assert the arm
            # failed a repo that cannot be scored at all.
            excluded.append((name, "untrusted"))
            continue
        entry = gold[name]
        size = _gold_size(entry)
        if size is None:
            excluded.append((name, "gold_invalid_size"))
            continue
        row = by_repo.get(name)
        if row is None:
            per_repo[name] = 0.0
            no_row.append(name)
            continue
        want = (entry.get("sha") or "").lower() if isinstance(entry, dict) else ""
        got = ((row.meta or {}).get("head_sha") or "").lower()
        if want and got and want != got:
            # A count taken at a different commit is not ground truth for the pinned one. Report
            # it; do NOT drop the repo, because that would silently move the published number.
            sha_bad.append(name)
        raw = (row.passed or 0) / size
        if raw > 1.0:
            capped.append((name, round(raw, 3), row.passed, size))
        per_repo[name] = min(raw, 1.0)
        if _conforming(row) and row.collect_clean:
            c = min(len(row.collected_node_ids) / size, 1.0)
            cov.append(c)
            if c < 0.5:
                hollow.append((name, len(row.collected_node_ids), size, round(c, 4)))

    for name in by_repo:
        if name not in gold:
            excluded.append((name, "gold_missing"))

    vals = list(per_repo.values())
    return {
        "ESSR": _r(statistics.fmean(vals)) if vals else None,
        "ESSR_median": _r(statistics.median(vals)) if vals else None,
        "n_gold_scored": len(vals),
        "n_gold_zero": sum(1 for v in vals if v == 0.0),
        "n_gold_no_row": len(no_row),
        "gold_no_row": sorted(no_row),
        "n_gold_capped": len(capped),
        "gold_capped": sorted(capped, key=lambda x: -x[1]),
        "n_gold_excluded": len(excluded),
        "gold_excluded": sorted(excluded),
        "n_gold_sha_mismatch": len(sha_bad),
        "gold_sha_mismatch": sorted(sha_bad),
        # Hollow-pass spectrum. `n_ebsr_zero_collected` above catches only the degenerate
        # zero-collected case; with a certified denominator the whole range is measurable.
        "mean_collection_coverage": _r(statistics.fmean(cov)) if cov else None,
        "median_collection_coverage": _r(statistics.median(cov)) if cov else None,
        "n_gold_hollow": len(hollow),
        "gold_hollow": sorted(hollow, key=lambda x: x[3]),
    }


def load_gold_counts(path: str, corpus: str | None = None) -> dict:
    """{full_name_lower: {"size", "sha", "status"}} from a gold manifest.

    Handles both shapes on disk: the combined file (`corpora.<name>.repos`) and the legacy
    single-corpus one (`repos` at the top level). `corpus` selects one corpus from a combined
    file; omitted, every corpus is merged.

    Fails loudly on a missing or malformed file: a silently-absent gold manifest would publish a
    table with no anchor and no warning.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"gold manifest not found: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except json.JSONDecodeError as e:
        raise ValueError(f"gold manifest is not valid JSON: {path} ({e})") from e

    corpora = doc.get("corpora")
    if corpora is None:
        blocks = {"<flat>": doc.get("repos") or {}}
    elif corpus is None:
        blocks = {k: (v.get("repos") or {}) for k, v in corpora.items()}
    else:
        if corpus not in corpora:
            raise KeyError(f"no corpus {corpus!r} in {path}; available: {sorted(corpora)}")
        blocks = {corpus: corpora[corpus].get("repos") or {}}

    out: dict = {}
    for block in blocks.values():
        entries = block.values() if isinstance(block, dict) else block
        for rec in entries:
            name = (rec.get("full_name") or "").strip().lower()
            size = _gold_size({"size": rec.get("manifest_size")})
            if not name or size is None:
                continue          # REJECTED/ERROR entries carry size 0 and supply no denominator
            out[name] = {"size": size, "sha": rec.get("sha"), "status": rec.get("status")}
    return out
