# tests/bench/test_metrics_gates.py
from bench.schema import MeasureRow
from bench.metrics import compute_metrics


def _row(**kw):
    base = dict(agent="a", repo="r", env_status="ok", build_ok=True, executed=True)
    base.update(kw)
    return MeasureRow(**base)


def test_gates_over_full_denominator():
    rows = [
        _row(repo="r1", ebsr=True, pass_rate=1.0, collect_clean=True, total=10, passed=10),
        _row(repo="r2", ebsr=True, pass_rate=0.5, collect_clean=False, total=10, passed=5),
        _row(repo="r3", build_ok=False, executed=False, ebsr=False, pass_rate=0.0),
    ]
    m = compute_metrics(rows)
    assert m["n"] == 3
    # EBSR = collect-only exit 0 (Repo2Run-style): only r1 is collect_clean -> 1/3
    assert m["n_collect_clean"] == 1 and m["EBSR"] == round(1 / 3, 4)
    assert m["ESSR_all"] == round((1.0 + 0.5 + 0.0) / 3, 4)
    # ESSR (RAT-official headline) = mean pass_rate over the 2 executed repos
    assert m["ESSR"] == round((1.0 + 0.5) / 2, 4)
    assert m["coverage"] == round(2 / 3, 4)


def test_ebsr_collection_diagnostics():
    rows = [
        _row(repo="r1", collect_clean=True, collect_error_count=0,
             collected_node_ids=("a.py::t1", "a.py::t2", "a.py::t3")),
        _row(repo="r2", collect_clean=False, collect_error_count=2, collected_node_ids=("b.py::t1",)),
    ]
    m = compute_metrics(rows)
    assert m["total_collected"] == 4 and m["mean_collected"] == round(4 / 2, 4)
    assert m["total_collect_errors"] == 2 and m["mean_collect_errors"] == round(2 / 2, 4)


def test_real_success_requires_ebsr_and_pass_ge_080():
    rows = [_row(repo="r1", ebsr=True, pass_rate=0.80), _row(repo="r2", ebsr=True, pass_rate=0.79),
            _row(repo="r3", ebsr=False, pass_rate=1.0)]
    m = compute_metrics(rows)
    assert m["n_real_success"] == 1 and m["real_success"] == round(1 / 3, 4)


def test_micro_is_test_weighted_over_executed():
    rows = [_row(repo="r1", total=100, skipped=0, passed=90), _row(repo="r2", total=10, skipped=2, passed=8)]
    m = compute_metrics(rows)
    assert m["micro"] == round((90 + 8) / (100 + 8), 4)


def test_empty_rows_safe():
    m = compute_metrics([])
    assert m["n"] == 0 and m["EBSR"] == 0.0 and m["ESSR_all"] == 0.0
