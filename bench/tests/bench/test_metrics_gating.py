# tests/bench/test_metrics_gating.py
"""The minimal-(a) EBSR rule + status gating in bench/metrics.py (design §2.4).

Proves the three properties the /testbed guard buys us:
  1. a non_conforming row with collect_rc in {0,5} is EBSR-0 (false-green removed, false_green_removed>0),
  2. a conforming rc-5 row is STILL EBSR-credited (option-(a) byte-comparability),
  3. an unmeasurable row is excluded from every denominator.
"""
from bench.schema import MeasureRow
from bench.metrics import compute_metrics


def _row(**kw):
    base = dict(agent="a", repo="r", env_status="ok", build_ok=True)
    base.update(kw)
    return MeasureRow(**base)


def test_non_conforming_row_loses_ebsr_despite_clean_collect_rc():
    # The false-green: repo2run-naive built ok, /testbed empty -> collect on an empty dir exits 5.
    # collect_rc is 5 (would pass the raw Repo2Run gate) but status=non_conforming -> EBSR 0.
    rows = [
        _row(repo="r1", status="non_conforming", collect_rc=5, collect_clean=True,
             executed=False, ebsr=False),
        _row(repo="r2", status="executed", collect_rc=0, collect_clean=True,
             executed=True, ebsr=True, total=4, passed=4, pass_rate=1.0),
    ]
    m = compute_metrics(rows)
    assert m["n"] == 2
    # Only r2 is conforming -> EBSR = 1/2. The raw (ungated) gate would have counted both -> 2/2.
    assert m["n_ebsr"] == 1 and m["EBSR"] == round(1 / 2, 4)
    assert m["n_raw"] == 2 and m["EBSR_repo2run_raw"] == 1.0
    assert m["false_green_removed"] == round(1 / 2, 4)   # the removed false-green is auditable
    assert m["status_census"]["non_conforming"] == 1


def test_conforming_rc5_row_is_still_ebsr_credited():
    # Option (a): a CONFORMING image whose collect exits rc 5 (no tests) keeps EBSR credit, exactly
    # as Repo2Run — rc-5 credit is removed ONLY for non-conforming images.
    rows = [_row(repo="r1", status="no_tests_collected", collect_rc=5, collect_clean=True,
                 executed=False, ebsr=False)]
    m = compute_metrics(rows)
    assert m["n"] == 1 and m["n_ebsr"] == 1 and m["EBSR"] == 1.0
    assert m["false_green_removed"] == 0.0               # conforming repos are byte-unaffected


def test_unmeasurable_excluded_from_every_denominator():
    # A non-producer (live-claude/sweagent) row must never be a 0 in any denominator, and must not
    # shadow a real inline score.
    rows = [
        _row(repo="r1", status="executed", collect_rc=0, collect_clean=True,
             executed=True, ebsr=True, total=2, passed=2, pass_rate=1.0),
        _row(repo="r2", status="unmeasurable", build_ok=False, executed=False, ebsr=False),
    ]
    m = compute_metrics(rows)
    # denominator is 1 (only r1), unmeasurable counted separately
    assert m["n"] == 1 and m["n_unmeasurable"] == 1
    assert m["EBSR"] == 1.0 and m["ESSR"] == 1.0 and m["ESSR_all"] == 1.0
    assert m["coverage"] == 1.0 and m["real_success"] == 1.0
    assert m["status_census"]["unmeasurable"] == 1


def test_census_and_false_green_present_for_empty_rows():
    m = compute_metrics([])
    assert m["n"] == 0 and m["EBSR"] == 0.0
    assert m["false_green_removed"] == 0.0 and m["status_census"] == {}
    assert m["n_unmeasurable"] == 0
