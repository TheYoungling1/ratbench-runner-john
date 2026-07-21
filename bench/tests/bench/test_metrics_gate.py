# tests/bench/test_metrics_gate.py
from bench.metrics import compute_metrics
from bench.schema import MeasureRow


def _row(**kw):
    base = dict(agent="a", repo="o/r", env_status="ok", build_ok=True, status="executed",
                executed=True, ebsr=True, pass_rate=1.0, collect_rc=0, collect_clean=True)
    base.update(kw)
    return MeasureRow(**base)


def test_ebsr_credit_follows_collect_clean_not_raw_rc():
    # A compiled-language row that exited 5 (NOT a clean gate for that language) has collect_clean
    # False. EBSR must NOT credit it, even though `collect_rc in (0,5)` would.
    rows = [_row(collect_rc=5, collect_clean=False)]
    m = compute_metrics(rows)
    assert m["n_ebsr"] == 0 and m["EBSR"] == 0.0


def test_ebsr_credit_for_clean_gate():
    rows = [_row(collect_rc=0, collect_clean=True)]
    m = compute_metrics(rows)
    assert m["n_ebsr"] == 1 and m["EBSR"] == 1.0
