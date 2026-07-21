# bench/metrics.py
from __future__ import annotations

from collections import Counter

# from bench.gold import gold_coverage  # golden-set calc disabled for now (no gold JSON)
from bench.schema import MeasureRow

# Statuses that DISQUALIFY a row from EBSR credit (design §2.4). A row is "conforming" (repo genuinely
# present at /testbed, build ok) iff its status is NOT one of these. `unmeasurable` is handled
# separately — excluded from the denominator entirely, never a 0 for a method.
_DISQUALIFIED = ("non_conforming", "empty_testbed", "build_fail", "missing", "measure_error", "error")


def _conforming(r: MeasureRow) -> bool:
    return r.status not in _DISQUALIFIED


def _r(x: float) -> float:
    return round(x, 4)


def _div(num: float, den: float) -> float:
    return _r(num / den) if den else 0.0


def _mean_opt(vals: list) -> float | None:
    xs = [v for v in vals if v is not None]
    return _r(sum(xs) / len(xs)) if xs else None


def compute_metrics(rows: list[MeasureRow], gold: dict | None = None) -> dict:
    # `unmeasurable` (non-producers: live-claude, sweagent) and `legacy_missing` (pre-contract runs
    # with no Dockerfile) are excluded from EVERY denominator — neither must emit an EBSR-0 that
    # shadows a method's real score (design §2.4/§2.5).
    prod = [r for r in rows if r.status not in ("unmeasurable", "legacy_missing")]
    n = len(prod)
    n_unmeasurable = sum(1 for r in rows if r.status == "unmeasurable")
    n_legacy_missing = sum(1 for r in rows if r.status == "legacy_missing")
    ex = [r for r in prod if r.executed]
    n_exec = len(ex)
    n_collect_clean = sum(1 for r in prod if r.collect_clean)   # collect-only exit code in {0,5}
    n_real = sum(1 for r in prod if r.ebsr and r.pass_rate >= 0.8)
    micro_passed = sum(r.passed for r in ex)
    micro_total = sum(max(r.total - r.skipped, 0) for r in ex)

    # EBSR credit uses the per-language gate outcome (collect_clean), so a language whose clean gate
    # is NOT rc-5 (e.g. compiled: rc==0 only) is scored correctly. For Python collect_clean == (rc in
    # {0,5}), so this is byte-identical. `n_raw` below stays the Repo2Run rc-{0,5} parity diagnostic.
    n_ebsr = sum(1 for r in prod if _conforming(r) and r.collect_clean)
    # Diagnostic: the OLD ungated gate (what M3/paper reported). The delta is the removed false-green.
    n_raw = sum(1 for r in prod if r.collect_rc in (0, 5))

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
        "total_collected": sum(len(r.collected_node_ids) for r in prod),
        "mean_collected": _div(sum(len(r.collected_node_ids) for r in prod), n),
        "total_collect_errors": sum(r.collect_error_count for r in prod),
        "mean_collect_errors": _div(sum(r.collect_error_count for r in prod), n),
        # ESSR (RAT-official headline): mean pass_rate over EXECUTED repos, where
        # pass_rate = passed / (total - skipped) — errors kept IN the denominator (RAT parity).
        "ESSR": _div(sum(r.pass_rate for r in ex), n_exec),
        # ÷all coverage-penalized variant (mean over ALL measured repos, build-fails count as 0).
        "ESSR_all": _div(sum(r.pass_rate for r in prod), n),
        "real_success": _div(n_real, n),
        "micro": _div(micro_passed, micro_total),
        "full_pass_repos": sum(1 for r in ex if r.pass_rate >= 0.999),
        "coverage": _div(n_exec, n),
    }
    # Gold-anchored scoring (EBSR_improved/ESSR_improved) is DEPRECATED and removed from the
    # runner — the active headline metrics are EBSR (Repo2Run-exact) and ESSR (RAT-exact) above.
    # `gold_coverage` (bench/gold.py) is kept for reference only; do not re-enable without a
    # pinned gold JSON + the node-id-form contract.
    # if gold:
    #     out.update(gold_coverage(rows, gold))

    # Economy metrics are computed over `prod` too (unmeasurable rows carry no rebuildable env).
    tok_rows = [r for r in prod if r.tokens_in is not None and r.tokens_out is not None]
    tok_total = sum(r.tokens_in + r.tokens_out for r in tok_rows)
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
