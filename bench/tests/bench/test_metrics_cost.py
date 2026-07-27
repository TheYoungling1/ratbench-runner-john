# bench/tests/bench/test_metrics_cost.py — USD aggregation (Claude Code reports actual cost).
from bench.metrics import compute_metrics
from bench.schema import MeasureRow


def _row(repo, *, cost=None, ebsr=True, pass_rate=1.0, status="executed"):
    return MeasureRow(agent="a", repo=repo, env_status="produced", build_ok=True,
                      executed=True, ebsr=ebsr, pass_rate=pass_rate, status=status,
                      collect_clean=True, cost_usd=cost)


def test_cost_aggregates_over_reporting_rows():
    m = compute_metrics([_row("o/a", cost=1.0), _row("o/b", cost=3.0)])
    assert m["total_cost_usd"] == 4.0
    assert m["mean_cost_usd"] == 2.0
    assert m["n_cost_reporting"] == 2


def test_cost_per_ebsr_and_per_real_success():
    # b is EBSR but below the 0.8 real-success bar, so it counts for cost_per_ebsr only.
    m = compute_metrics([_row("o/a", cost=1.0), _row("o/b", cost=3.0, pass_rate=0.5)])
    assert m["cost_per_ebsr"] == 2.0            # 4.0 / 2 ebsr rows
    assert m["cost_per_real_success"] == 4.0    # 4.0 / 1 real success


def test_rows_without_cost_are_excluded_not_zeroed():
    # A producer that reports no cost (dockeragent, repo2run) must not drag the mean to 0.
    m = compute_metrics([_row("o/a", cost=2.0), _row("o/b", cost=None)])
    assert m["mean_cost_usd"] == 2.0
    assert m["n_cost_reporting"] == 1


def test_no_cost_anywhere_reports_none_not_zero():
    m = compute_metrics([_row("o/a"), _row("o/b")])
    assert m["total_cost_usd"] is None
    assert m["mean_cost_usd"] is None
    assert m["cost_per_ebsr"] is None
    assert m["cost_per_real_success"] is None
    assert m["n_cost_reporting"] == 0
