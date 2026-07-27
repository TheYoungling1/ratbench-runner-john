# tests/bench/test_error_report.py
import pytest

from bench.report.error_report import arm_report, row_events
from bench.schema import MeasureRow

MNF_W = "E   ModuleNotFoundError: No module named 'wrapt'"
MNF_H = "E   ModuleNotFoundError: No module named 'httpx'"
MNF_T = "E   ModuleNotFoundError: No module named 'tests.conftest'"
RUN_BOTO = "FAILED tests/a.py::t - ModuleNotFoundError: No module named 'boto3'"
RUN_ASSERT = "FAILED tests/b.py::t - AssertionError: expected 3 got 4"


def _row(repo, **kw):
    # collected_node_ids MUST be non-empty for a `full` surface
    base = dict(agent="a", repo=repo, env_status="ok", build_ok=True, status="executed",
                collect_rc=0, collect_clean=True, collected_node_ids=("tests/a.py::t",),
                executed=True, collect_errors=(), repo_toplevel=(), pass_rate=0.5,
                timed_out=False, meta={})
    base.update(kw)
    return MeasureRow(**base)


def test_counting_unit_is_repo_presence_not_event_volume():
    rows = [_row("o/big", collect_errors=tuple([MNF_W] * 40)),
            _row("o/a", collect_errors=(MNF_H,)), _row("o/b", collect_errors=(MNF_H,))]
    r = arm_report(rows)
    assert r.token_repos["ModuleNotFoundError"] == 3
    assert r.token_events["ModuleNotFoundError"] == 3     # deduped per repo, not 42


def test_masked_repos_are_counted_but_contribute_no_events():
    rows = [_row("o/full", collect_errors=(MNF_W,)),
            _row("o/masked", collect_rc=4, collect_clean=False, collected_node_ids=(),
                 collect_errors=(MNF_H,))]
    r = arm_report(rows)
    assert r.n_masked == 1 and r.n_admissible == 1
    assert r.token_repos["ModuleNotFoundError"] == 1 and "httpx" not in str(r.top_groups)


def test_collect_and_run_are_never_pooled():
    rows = [_row("o/x", collect_errors=(MNF_W,), run_failed_lines=(RUN_BOTO, RUN_ASSERT))]
    c, n = arm_report(rows, source="collect"), arm_report(rows, source="run")
    assert c.source == "collect" and n.source == "run"
    assert c.token_events == {"ModuleNotFoundError": 1}
    assert c.uncategorized_rate == 0.0        # NOT 0.5 — the AssertionError is a CODE failure
    assert n.token_events == {"ModuleNotFoundError": 1, "AssertionError": 1}


def test_crosstab_is_keyed_on_bucket_not_status():
    rows = [_row("o/x", status="executed", pass_rate=0.5, collect_errors=(MNF_W,))]
    assert arm_report(rows).crosstab["partial|module_not_found"] == 1


def test_derived_status_is_counted_loudly():
    rows = [_row("o/x", status="", collect_errors=(MNF_W,)),
            _row("o/y", status="executed", collect_errors=(MNF_W,))]
    assert arm_report(rows).n_status_derived == 1


def test_internal_split_uses_repo_toplevel_from_the_row():
    rows = [_row("o/x", collect_errors=(MNF_T, MNF_W), repo_toplevel=("tests", "src"))]
    r = arm_report(rows)
    assert r.category_repos["internal_import_failure"] == 1
    assert r.category_repos["module_not_found"] == 1


def test_top_groups_counts_repos_not_occurrences():
    rows = [_row("o/x", collect_errors=(MNF_W, MNF_W, MNF_W)), _row("o/y", collect_errors=(MNF_W,))]
    assert dict(arm_report(rows).top_groups["module_not_found"])["wrapt"] == 2


def test_unobserved_rows_counted_and_excluded():
    rows = [_row("o/dead", build_ok=False, status="build_fail"), _row("o/live", collect_errors=(MNF_W,))]
    r = arm_report(rows)
    assert r.n_unobserved == 1 and r.n_repos == 2 and r.n_admissible == 1
    assert r.buckets["build_fail"] == 1


def test_duplicate_repo_rows_are_rejected_not_silently_miscounted():
    with pytest.raises(ValueError, match="duplicate repo"):
        arm_report([_row("o/x"), _row("o/x")])
