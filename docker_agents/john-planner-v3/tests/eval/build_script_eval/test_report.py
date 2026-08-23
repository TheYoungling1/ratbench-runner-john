from src.eval.build_script_eval.report import aggregate, render_report_md


def _row(**kw):
    base = dict(repo="o/r", stratum="S_control", feasible=True, first_pass_env_works=True,
                install_ok=True, env_works=True, tests_ran=True, tests_passed=True,
                highest_rung="tests_passed", attribution="pass", predicted_apt=[],
                execution_missing=[], language_gaps=[], system_gaps=[])
    base.update(kw)
    return base


def test_headline_rate_overall_and_per_stratum():
    cards = [
        _row(stratum="S_control", first_pass_env_works=True),
        _row(stratum="S_syslib", first_pass_env_works=True),
        _row(stratum="S_syslib", first_pass_env_works=False, attribution="system_gap",
             system_gaps=[{"tier": "SYSTEM_LIB", "id": "libpq.so.5", "evidence": ""}],
             execution_missing=[{"tier": "SYSTEM_LIB", "id": "libpq.so.5", "evidence": ""}]),
    ]
    agg = aggregate(cards)
    assert agg["headline_env_works"]["overall"] == (2, 3)          # 2 of 3
    assert agg["headline_env_works"]["S_syslib"] == (1, 2)
    assert agg["headline_env_works"]["S_control"] == (1, 1)


def test_infeasible_excluded_from_denominator():
    cards = [_row(feasible=True, first_pass_env_works=True),
             _row(feasible=False, first_pass_env_works=False, attribution="infeasible")]
    agg = aggregate(cards)
    assert agg["headline_env_works"]["overall"] == (1, 1)


def test_attribution_histogram_and_ladder_funnel():
    cards = [_row(attribution="pass", tests_passed=True),
             _row(attribution="system_gap", first_pass_env_works=False, env_works=False,
                  tests_ran=False, tests_passed=False)]
    agg = aggregate(cards)
    assert agg["attribution_histogram"]["pass"] == 1
    assert agg["attribution_histogram"]["system_gap"] == 1
    assert agg["ladder_funnel"]["install_ok"] == 2
    assert agg["ladder_funnel"]["env_works"] == 1
    assert agg["ladder_funnel"]["tests_passed"] == 1


def test_report_md_has_headline_and_caveat():
    md = render_report_md(aggregate([_row()]))
    assert "First-pass env-works" in md
    assert "tests_passed" in md and "service" in md.lower()   # the confound caveat


def test_control_overprediction_and_gap_clusters_populated():
    shared_gap = {"tier": "SYSTEM_LIB", "id": "libpq.so.5", "evidence": ""}
    cards = [
        _row(repo="o/control", stratum="S_control", first_pass_env_works=False,
             attribution="system_gap", predicted_apt=["libpq-dev"],
             system_gaps=[shared_gap], execution_missing=[shared_gap]),
        _row(repo="o/other", stratum="S_syslib", first_pass_env_works=False,
             attribution="system_gap", predicted_apt=[],
             system_gaps=[shared_gap], execution_missing=[shared_gap]),
    ]
    agg = aggregate(cards)

    assert len(agg["control_overprediction"]) == 1
    control_row = agg["control_overprediction"][0]
    assert control_row["repo"] == "o/control"
    assert control_row["predicted_apt"] == ["libpq-dev"]

    assert len(agg["gap_clusters"]) == 1
    cluster = agg["gap_clusters"][0]
    assert cluster["tier"] == "SYSTEM_LIB"
    assert cluster["id"] == "libpq.so.5"
    assert cluster["count"] == 2
    assert set(cluster["repos"]) == {"o/control", "o/other"}

    md = render_report_md(agg)
    assert "libpq-dev" in md
    assert "libpq.so.5" in md
