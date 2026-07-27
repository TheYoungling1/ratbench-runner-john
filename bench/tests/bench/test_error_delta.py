# tests/bench/test_error_delta.py
from bench.report.error_report import (category_status, delta, interval_delta,
                                       masking_2x2, paired_repos)
from bench.schema import MeasureRow

MNF_W = "E   ModuleNotFoundError: No module named 'wrapt'"
SVC = "E   redis.exceptions.ConnectionError: Error 111 connecting to localhost:6379."
UNI = ("module_not_found", "service_unavailable")


def _row(agent, repo, **kw):
    base = dict(agent=agent, repo=repo, env_status="ok", build_ok=True, status="executed",
                collect_rc=0, collect_clean=True, collected_node_ids=("tests/a.py::t",),
                executed=True, collect_errors=(), repo_toplevel=(), pass_rate=0.5,
                timed_out=False, meta={})
    base.update(kw)
    return MeasureRow(**base)


def _masked(agent, repo, **kw):
    return _row(agent, repo, collect_rc=4, collect_clean=False, collected_node_ids=(), **kw)


def test_masked_repo_contributes_its_known_cause_to_present_rest_unknown():
    s = category_status([_masked("A", "o/1", collect_errors=(MNF_W,))], UNI)
    assert s["module_not_found"] == {"present": 1, "absent": 0, "unknown": 0}
    assert s["service_unavailable"] == {"present": 0, "absent": 0, "unknown": 1}


def test_full_surface_without_the_category_is_absent_not_unknown():
    s = category_status([_row("A", "o/1", collect_errors=(MNF_W,))], UNI)
    assert s["service_unavailable"] == {"present": 0, "absent": 1, "unknown": 0}


def test_unobserved_repo_is_all_unknown():
    s = category_status([_row("A", "o/1", build_ok=False)], UNI)
    assert s["module_not_found"] == {"present": 0, "absent": 0, "unknown": 1}


def test_interval_is_identified_when_it_excludes_zero():
    a = [_row("A", f"o/{i}", collect_errors=(MNF_W,)) for i in (1, 2, 3)]
    b = [_row("B", f"o/{i}", collect_errors=(SVC,)) for i in (1, 2, 3)]
    iv = interval_delta(a, b)["module_not_found"]
    assert (iv["lower"], iv["upper"]) == (-3, -3) and iv["identified"] is True


def test_interval_spans_zero_when_masking_could_explain_the_move():
    a = [_row("A", "o/1", collect_errors=(MNF_W,)), _row("A", "o/2", collect_errors=(SVC,))]
    b = [_row("B", "o/1", collect_errors=(SVC,)), _masked("B", "o/2")]
    iv = interval_delta(a, b)["module_not_found"]
    assert iv["lower"] == -1 and iv["upper"] == 0 and iv["identified"] is False


def test_masking_2x2_exposes_a_regression_the_marginals_hide():
    a = [_masked("A", "o/1"), _masked("A", "o/2"), _row("A", "o/3")]
    b = [_row("B", "o/1"), _row("B", "o/2"), _masked("B", "o/3")]
    assert masking_2x2(a, b) == {"neither_masked": 0, "a_only_masked": 2, "b_only_masked": 1,
                                 "both_masked": 0, "only_in_a": 0, "only_in_b": 0}


def test_masking_2x2_does_not_fabricate_cells_for_a_repo_missing_from_an_arm():
    m = masking_2x2([_row("A", "o/a")], [_row("B", "o/b")])
    assert m["neither_masked"] == 0 and m["only_in_a"] == 1 and m["only_in_b"] == 1


def test_paired_set_is_a_sensitivity_row_not_the_headline():
    a = [_row("A", "o/1", collect_errors=(MNF_W,)), _masked("A", "o/2")]
    b = [_row("B", "o/1"), _row("B", "o/2", collect_errors=(MNF_W,))]
    d = delta(a, b)
    assert paired_repos(a, b) == frozenset({"o/1"})
    assert d["sensitivity_paired"]["paired_repos"] == 1
    assert "category_interval" in d and "category_repos_delta" not in d


def test_category_absent_from_both_arms_still_appears_with_zero_width_interval():
    iv = delta([_row("A", "o/1")], [_row("B", "o/1")])["category_interval"]["syslib_missing"]
    assert iv["lower"] == 0 and iv["upper"] == 0 and iv["identified"] is False
