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


def test_hollow_pass_is_counted_but_ebsr_is_unchanged():
    """Python's clean gate is rc in {0,5}, and rc 5 means "no tests collected". Every repo in these
    datasets has tests, so an EBSR-credited row that collected nothing is a hollow pass: the image
    built and /testbed is a real worktree, but nothing ran. It must be VISIBLE without silently
    redefining EBSR — that redefinition is a deliberate decision, not a side effect."""
    rows = [_row(collected_node_ids=("t1", "t2")),
            _row(collect_rc=5, collected_node_ids=(), pass_rate=0.0)]
    m = compute_metrics(rows)
    assert m["n_ebsr_zero_collected"] == 1
    assert m["n_ebsr"] == 2, "the diagnostic must not move EBSR"
    assert m["n_real_success"] == 1, "the hollow row was never a real success"


def test_no_hollow_passes_reports_zero_not_absent():
    m = compute_metrics([_row(collected_node_ids=("t1",))])
    assert m["n_ebsr_zero_collected"] == 0
