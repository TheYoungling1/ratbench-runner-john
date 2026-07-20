from src.eval.graph_repair_ablation.run import aggregate, render_report_md


def _r(cls, arm, l1, wasted=0.0, mis=False):
    return {"failure_class": cls, "arm": arm,
            "score": {"localized_at_1": l1, "localized_at_3": l1, "first_correct_rank": 1 if l1 else None,
                      "mislocalized": mis, "wasted_rate": wasted, "success_action": None}}


def test_aggregate_groups_by_class_and_arm():
    agg = aggregate([_r("SYSLIB_MISSING", "C0", False, 0.5), _r("SYSLIB_MISSING", "C1", True, 0.0)])
    assert agg[("SYSLIB_MISSING", "C1")]["localized_at_1"] == 1.0
    assert agg[("SYSLIB_MISSING", "C0")]["localized_at_1"] == 0.0


def test_aggregate_two_classes_two_arms_c1_vs_c0():
    results = [
        _r("SYSLIB_MISSING", "C0", False, 0.5),
        _r("SYSLIB_MISSING", "C1", True, 0.0),
        _r("OVERINCLUDE", "C0", False, 1.0, mis=True),
        _r("OVERINCLUDE", "C1", True, 0.0),
    ]
    agg = aggregate(results)
    assert set(agg) == {
        ("SYSLIB_MISSING", "C0"), ("SYSLIB_MISSING", "C1"),
        ("OVERINCLUDE", "C0"), ("OVERINCLUDE", "C1"),
    }
    # C1 localizes at least as well as C0 on both synthetic classes here.
    assert agg[("OVERINCLUDE", "C1")]["localized_at_1"] >= agg[("OVERINCLUDE", "C0")]["localized_at_1"]
    assert agg[("SYSLIB_MISSING", "C1")]["localized_at_1"] >= agg[("SYSLIB_MISSING", "C0")]["localized_at_1"]
    assert agg[("OVERINCLUDE", "C0")]["mislocalized"] == 1
    assert agg[("OVERINCLUDE", "C1")]["mislocalized"] == 0


def test_aggregate_first_correct_rank_finite_only_and_none_when_all_none():
    results = [
        _r("SYSLIB_MISSING", "C0", False, 1.0),  # rank None
        _r("SYSLIB_MISSING", "C0", False, 1.0),  # rank None
    ]
    agg = aggregate(results)
    assert agg[("SYSLIB_MISSING", "C0")]["first_correct_rank"] is None


def test_aggregate_mean_wasted_rate_and_n():
    results = [_r("TOOL_ABSENT", "C1", True, 0.2), _r("TOOL_ABSENT", "C1", True, 0.6)]
    agg = aggregate(results)
    cell = agg[("TOOL_ABSENT", "C1")]
    assert cell["n"] == 2
    assert abs(cell["wasted_rate"] - 0.4) < 1e-9


def test_aggregate_skips_none_score_entries():
    results = [
        {"failure_class": "SYSLIB_MISSING", "arm": "C0", "score": None},
        _r("SYSLIB_MISSING", "C0", True, 0.0),
    ]
    agg = aggregate(results)
    assert agg[("SYSLIB_MISSING", "C0")]["n"] == 1


def test_render_report_md_is_nonempty_and_names_both_arms():
    agg = aggregate([_r("SYSLIB_MISSING", "C0", False, 0.5), _r("SYSLIB_MISSING", "C1", True, 0.0)])
    report = render_report_md(agg)
    assert isinstance(report, str) and report.strip()
    assert "C0" in report and "C1" in report
    assert "SYSLIB_MISSING" in report


def test_render_report_md_empty_agg():
    report = render_report_md({})
    assert isinstance(report, str) and report.strip()
