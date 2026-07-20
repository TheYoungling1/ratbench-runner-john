# bench/metrics.py
from __future__ import annotations

# from bench.gold import gold_coverage  # golden-set calc disabled for now (no gold JSON)
from bench.schema import MeasureRow


def _r(x: float) -> float:
    return round(x, 4)


def _div(num: float, den: float) -> float:
    return _r(num / den) if den else 0.0


def _mean_opt(vals: list) -> float | None:
    xs = [v for v in vals if v is not None]
    return _r(sum(xs) / len(xs)) if xs else None


def compute_metrics(rows: list[MeasureRow], gold: dict | None = None) -> dict:
    n = len(rows)
    ex = [r for r in rows if r.executed]
    n_exec = len(ex)
    n_collect_clean = sum(1 for r in rows if r.collect_clean)   # collect-only exit code == 0
    n_real = sum(1 for r in rows if r.ebsr and r.pass_rate >= 0.8)
    micro_passed = sum(r.passed for r in ex)
    micro_total = sum(max(r.total - r.skipped, 0) for r in ex)

    out = {
        "n": n, "n_exec": n_exec, "n_collect_clean": n_collect_clean,
        "n_real_success": n_real,
        # EBSR (Repo2Run-style): fraction of repos where `pytest --collect-only` exits 0.
        "EBSR": _div(n_collect_clean, n),
        # EBSR collection diagnostics: tests collected + collection errors (over all repos).
        "total_collected": sum(len(r.collected_node_ids) for r in rows),
        "mean_collected": _div(sum(len(r.collected_node_ids) for r in rows), n),
        "total_collect_errors": sum(r.collect_error_count for r in rows),
        "mean_collect_errors": _div(sum(r.collect_error_count for r in rows), n),
        # ESSR (RAT-official headline): mean pass_rate over EXECUTED repos, where
        # pass_rate = passed / (total - skipped) — errors kept IN the denominator (RAT parity).
        "ESSR": _div(sum(r.pass_rate for r in ex), n_exec),
        # ÷all coverage-penalized variant (mean over ALL repos, build-fails count as 0).
        "ESSR_all": _div(sum(r.pass_rate for r in rows), n),
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

    tok_rows = [r for r in rows if r.tokens_in is not None and r.tokens_out is not None]
    tok_total = sum(r.tokens_in + r.tokens_out for r in tok_rows)
    n_build_ok = sum(1 for r in rows if r.build_ok)
    n_unreplayed = sum(1 for r in rows if r.meta.get("unreplayed"))

    out.update({
        "mean_image_delta_mb": _mean_opt([r.image_delta_mb for r in rows]),
        "mean_installed_pkgs": _mean_opt([r.installed_pkg_count for r in rows]),
        "mean_tokens": _r(tok_total / len(tok_rows)) if tok_rows else None,
        "mean_tokens_out": _mean_opt([r.tokens_out for r in tok_rows]) if tok_rows else None,
        "tokens_per_ebsr": _r(tok_total / n_collect_clean) if (tok_rows and n_collect_clean) else None,
        "tokens_per_real_success": _r(tok_total / n_real) if (tok_rows and n_real) else None,
        "mean_turns": _mean_opt([r.turns_used for r in rows]),
        "mean_produce_s": _mean_opt([r.produce_s for r in rows]),
        "wall_s_per_real_success": (
            _r(sum((r.produce_s or 0) + (r.build_s or 0) + (r.test_s or 0) for r in rows) / n_real)
            if n_real else None),
        "n_token_reporting": len(tok_rows),
        "rebuild_ok_rate": _div(n_build_ok, n),
        "unreplayed_rate": _div(n_unreplayed, n),
    })
    return out
