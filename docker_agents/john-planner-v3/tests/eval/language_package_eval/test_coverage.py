"""TDD tests for src/eval/language_package_eval/coverage.py — the Phase-1
COVERAGE evaluator (graph-vs-oracle recall + execution-discovered gaps).

Pure parts only (per the loop doc's TDD guardrail): tier diffing, package pin
checking, execution-failure classification, top-level import name guessing,
base-image derivation, and report aggregation. The container probe
(``run_execution_probe`` / ``build_graph_construction_only``) is integration —
not exercised here (see the loop doc §3 item 3 and the module docstring).
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.eval.language_package_eval.coverage import (
    TierDiff,
    base_image_for_repo,
    canon_pip,
    classify_execution_failures,
    diff_membership,
    diff_packages,
    first_failure_evidence,
    graph_counts_by_tier,
    missing_node_clusters,
    pin_status,
    pooled_recall_by_tier,
    render_report_md,
    score_repo_against_oracle,
    split_pip_spec,
    top_level_import_name,
)

sys.path.insert(0, str(_REPO_ROOT / "src"))
from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph, DiscoveredBy, Layer, Node, NodeType, State,
)


def _node(node_id, node_type, name, **kwargs) -> Node:
    defaults = dict(layer=Layer.SYSTEM, discovered_by=DiscoveredBy.PROBE, state=State.SATISFIED)
    defaults.update(kwargs)
    return Node(id=node_id, type=node_type, name=name, **defaults)


# ---------------------------------------------------------------------------
# diff_membership (generic tier diff)
# ---------------------------------------------------------------------------

class TestDiffMembership:
    def test_exact_match_all_matched(self):
        result = diff_membership(["libpq-dev", "gcc"], ["libpq-dev", "gcc"])
        assert result.matched == ("gcc", "libpq-dev")
        assert result.missing == ()
        assert result.extra == ()

    def test_case_insensitive_by_default(self):
        result = diff_membership(["Libpq-Dev"], ["libpq-dev"])
        assert result.matched == ("Libpq-Dev",)

    def test_missing_and_extra_both_reported(self):
        result = diff_membership(["libpq-dev", "curl"], ["libpq-dev", "gcc"])
        assert result.missing == ("curl",)
        assert result.extra == ("gcc",)

    def test_recall_is_matched_over_matched_plus_missing(self):
        result = diff_membership(["a", "b", "c"], ["a"])
        assert result.recall == 1 / 3

    def test_recall_none_when_oracle_declares_nothing(self):
        result = diff_membership([], ["extra-only"])
        assert result.recall is None
        assert result.missing == ()
        assert result.extra == ("extra-only",)

    def test_recall_one_when_oracle_and_graph_both_empty(self):
        result = diff_membership([], [])
        assert result.recall is None

    def test_is_frozen_dataclass_instance(self):
        assert isinstance(diff_membership(["a"], ["a"]), TierDiff)


# ---------------------------------------------------------------------------
# Package-specific diff: name split, canon, pin status
# ---------------------------------------------------------------------------

class TestSplitPipSpec:
    def test_pinned_spec_splits_name_and_pin(self):
        assert split_pip_spec("requests==2.31.0") == ("requests", "==2.31.0")

    def test_range_spec_splits_name_and_pin(self):
        assert split_pip_spec("flask>=2.0,<3.0") == ("flask", ">=2.0,<3.0")

    def test_bare_name_has_no_pin(self):
        assert split_pip_spec("flask") == ("flask", None)

    def test_whitespace_stripped(self):
        assert split_pip_spec("  requests == 2.31.0 ") == ("requests", "== 2.31.0")


class TestCanonPip:
    def test_case_and_separator_insensitive(self):
        assert canon_pip("Flask_Login") == canon_pip("flask-login") == "flask-login"

    def test_dots_normalize_like_dashes(self):
        assert canon_pip("zope.interface") == "zope-interface"


class TestPinStatus:
    def test_exact_match(self):
        assert pin_status("==2.31.0", "2.31.0") == "match"

    def test_mismatch(self):
        assert pin_status("==2.31.0", "2.30.0") == "mismatch"

    def test_range_satisfied_is_match(self):
        assert pin_status(">=2.0,<3.0", "2.5.0") == "match"

    def test_range_violated_is_mismatch(self):
        assert pin_status(">=2.0,<3.0", "3.1.0") == "mismatch"

    def test_no_graph_version(self):
        assert pin_status("==2.31.0", None) == "no_graph_version"

    def test_unparseable_pin_is_unverifiable(self):
        assert pin_status("not-a-real-specifier!!", "1.0.0") == "unverifiable"


class TestDiffPackages:
    def test_name_only_membership_ignores_version(self):
        diff, mismatches = diff_packages(["requests==2.31.0"], {"requests": "9.9.9"})
        assert diff.matched == ("requests",)
        assert diff.missing == ()
        assert mismatches[0]["status"] == "mismatch"

    def test_matched_with_correct_pin_has_no_mismatch(self):
        diff, mismatches = diff_packages(["requests==2.31.0"], {"requests": "2.31.0"})
        assert diff.matched == ("requests",)
        assert mismatches == ()

    def test_bare_name_declared_no_pin_check(self):
        diff, mismatches = diff_packages(["flask"], {"flask": "1.2.3"})
        assert diff.matched == ("flask",)
        assert mismatches == ()

    def test_missing_package_not_in_graph(self):
        diff, mismatches = diff_packages(["requests==2.31.0"], {})
        assert diff.missing == ("requests",)
        assert mismatches == ()

    def test_canon_normalizes_dash_underscore_across_sides(self):
        diff, _ = diff_packages(["flask-login==1.0"], {"Flask_Login": "1.0"})
        assert diff.matched == ("flask-login",)


# ---------------------------------------------------------------------------
# Graph tier extraction + Reference-A scoring
# ---------------------------------------------------------------------------

def _graph_for_scoring() -> DepGraph:
    g = DepGraph()
    g = g.with_node(_node("syslib:libpq", NodeType.SYSTEM_LIB, "libpq", chosen_fix="apt:libpq-dev"))
    g = g.with_node(_node("tool:gcc", NodeType.TOOL, "gcc", chosen_fix="apt:gcc"))
    g = g.with_node(_node("runtime:python-3.11", NodeType.RUNTIME, "python 3.11", version="3.11"))
    g = g.with_node(_node("pkg:requests", NodeType.PACKAGE, "requests", version="2.31.0"))
    g = g.with_node(_node("pkg:flask", NodeType.PACKAGE, "flask", version="3.0.0"))
    return g


class TestGraphTierExtraction:
    def test_apt_names_merge_system_lib_and_tool(self):
        from src.eval.language_package_eval.coverage import apt_names_in_graph
        assert apt_names_in_graph(_graph_for_scoring()) == frozenset({"libpq-dev", "gcc"})

    def test_apt_name_none_when_no_chosen_fix(self):
        from src.eval.language_package_eval.coverage import apt_names_in_graph
        g = DepGraph().with_node(_node("syslib:x", NodeType.SYSTEM_LIB, "x"))
        assert apt_names_in_graph(g) == frozenset()

    def test_runtime_minors(self):
        from src.eval.language_package_eval.coverage import runtime_minors_in_graph
        assert runtime_minors_in_graph(_graph_for_scoring()) == frozenset({"3.11"})

    def test_package_versions(self):
        from src.eval.language_package_eval.coverage import package_versions_in_graph
        assert package_versions_in_graph(_graph_for_scoring()) == {
            "requests": "2.31.0", "flask": "3.0.0",
        }

    def test_graph_counts_by_tier_zero_fills_all_node_types(self):
        counts = graph_counts_by_tier(_graph_for_scoring())
        assert counts["PACKAGE"] == 2
        assert counts["SYSTEM_LIB"] == 1
        assert counts["TOOL"] == 1
        assert counts["SERVICE"] == 0
        assert set(counts) == {t.name for t in NodeType}


class TestScoreRepoAgainstOracle:
    def test_recall_and_diff_shape_per_tier(self):
        declared = {
            "SYSTEM_LIB": ("libpq-dev", "curl"),
            "RUNTIME": ("3.11",),
            "PACKAGE": ("requests==2.31.0", "black"),
            "SERVICE": (),
            "CONFIG": (),
        }
        result = score_repo_against_oracle(_graph_for_scoring(), declared)
        assert result["oracle_recall_by_tier"]["SYSTEM_LIB"] == 1 / 2
        assert "curl" in result["missing_vs_oracle_by_tier"]["SYSTEM_LIB"]
        assert "gcc" in result["extra_vs_oracle_by_tier"]["SYSTEM_LIB"]
        assert result["oracle_recall_by_tier"]["RUNTIME"] == 1.0
        assert result["oracle_recall_by_tier"]["PACKAGE"] == 1 / 2
        assert "black" in result["missing_vs_oracle_by_tier"]["PACKAGE"]
        assert result["oracle_recall_by_tier"]["SERVICE"] is None
        assert result["oracle_recall_by_tier"]["CONFIG"] is None

    def test_all_five_oracle_tier_keys_always_present(self):
        result = score_repo_against_oracle(_graph_for_scoring(), {})
        assert set(result["oracle_recall_by_tier"]) == {
            "SYSTEM_LIB", "RUNTIME", "PACKAGE", "SERVICE", "CONFIG",
        }

    def test_pin_mismatch_surfaced(self):
        declared = {"PACKAGE": ("requests==9.9.9",)}
        result = score_repo_against_oracle(_graph_for_scoring(), declared)
        assert result["package_pin_mismatches"][0]["name"] == "requests"
        assert result["package_pin_mismatches"][0]["status"] == "mismatch"

    def test_undeclared_runtime_is_not_applicable_not_a_miss(self):
        # (E) When the oracle declares NO python (declared["RUNTIME"] == ()),
        # the graph's own RUNTIME node(s) must never be reported as "missing"
        # and recall must be None ("no signal"), not 0.0 -- an undeclared
        # runtime is not evidence the graph is wrong.
        declared = {"RUNTIME": ()}
        result = score_repo_against_oracle(_graph_for_scoring(), declared)
        assert result["missing_vs_oracle_by_tier"]["RUNTIME"] == []
        assert result["oracle_recall_by_tier"]["RUNTIME"] is None

    def test_undeclared_runtime_key_absent_entirely_is_also_not_a_miss(self):
        result = score_repo_against_oracle(_graph_for_scoring(), {})
        assert result["missing_vs_oracle_by_tier"]["RUNTIME"] == []
        assert result["oracle_recall_by_tier"]["RUNTIME"] is None


# ---------------------------------------------------------------------------
# Execution-failure classification
# ---------------------------------------------------------------------------

class TestClassifyExecutionFailures:
    def test_module_not_found(self):
        gaps = classify_execution_failures("ModuleNotFoundError: No module named 'click'")
        assert gaps == ({"tier": "PACKAGE", "id": "click",
                          "evidence": "ModuleNotFoundError: No module named 'click'"},)

    def test_module_not_found_takes_top_level_name(self):
        gaps = classify_execution_failures("ModuleNotFoundError: No module named 'a.b.c'")
        assert gaps[0]["id"] == "a"

    def test_shared_object_import_error(self):
        text = "ImportError: libGL.so.1: cannot open shared object file: No such file or directory"
        gaps = classify_execution_failures(text)
        assert gaps == ({"tier": "SYSTEM_LIB", "id": "libGL.so.1",
                          "evidence": "libGL.so.1: cannot open shared object file"},)

    def test_ldd_not_found_line(self):
        gaps = classify_execution_failures("\tlibfoo.so.1 => not found\n")
        assert gaps[0]["tier"] == "SYSTEM_LIB"
        assert gaps[0]["id"] == "libfoo.so.1"

    def test_command_not_found(self):
        gaps = classify_execution_failures("/setup.sh: line 4: solc: command not found")
        assert gaps[0] == {"tier": "TOOL", "id": "solc",
                            "evidence": "solc: command not found"}

    def test_service_connection_error(self):
        gaps = classify_execution_failures("psycopg2.OperationalError: could not connect to server")
        assert gaps[0]["tier"] == "SERVICE"

    def test_clean_output_yields_no_gaps(self):
        assert classify_execution_failures("collected 42 items\n") == ()

    def test_dedupes_repeated_matches(self):
        text = "ModuleNotFoundError: No module named 'click'\n" * 3
        gaps = classify_execution_failures(text)
        assert len(gaps) == 1

    def test_multiple_distinct_gaps_all_reported(self):
        text = (
            "ModuleNotFoundError: No module named 'click'\n"
            "ImportError: libGL.so.1: cannot open shared object file: x\n"
        )
        gaps = classify_execution_failures(text)
        tiers = {g["tier"] for g in gaps}
        assert tiers == {"PACKAGE", "SYSTEM_LIB"}


class TestFirstFailureEvidence:
    def test_uses_last_trace_line_when_present(self):
        output = "+ apt-get update\n+ apt-get install -y libfoo\nE: Unable to locate package libfoo\n"
        result = first_failure_evidence(output)
        assert result["command"] == "apt-get install -y libfoo"
        assert "Unable to locate package libfoo" in result["stderr_tail"]

    def test_falls_back_to_last_line_without_trace(self):
        result = first_failure_evidence("some error happened\nfinal line\n")
        assert result["command"] == "final line"

    def test_empty_output_is_handled(self):
        result = first_failure_evidence("")
        assert result["command"] is None
        assert result["stderr_tail"] == ""

    def test_tail_is_bounded(self):
        output = "\n".join(f"line{i}" for i in range(200))
        result = first_failure_evidence(output, tail_lines=10)
        assert result["stderr_tail"].count("\n") == 9


# ---------------------------------------------------------------------------
# Top-level import name guess
# ---------------------------------------------------------------------------

class TestTopLevelImportName:
    def test_finds_package_matching_pyproject_name(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "typer"\n')
        (tmp_path / "typer").mkdir()
        (tmp_path / "typer" / "__init__.py").write_text("")
        assert top_level_import_name(tmp_path) == "typer"

    def test_dash_in_project_name_normalizes_to_underscore(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "python-semantic-release"\n')
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "semantic_release").mkdir()
        (tmp_path / "src" / "semantic_release" / "__init__.py").write_text("")
        # project name doesn't literally match the src package dir; falls back
        assert top_level_import_name(tmp_path) == "semantic_release"

    def test_src_layout_preferred_when_name_matches(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "widget"\n')
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "widget").mkdir()
        (tmp_path / "src" / "widget" / "__init__.py").write_text("")
        assert top_level_import_name(tmp_path) == "widget"

    def test_ignores_tests_and_docs_dirs(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "nomatch"\n')
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "__init__.py").write_text("")
        (tmp_path / "realpkg").mkdir()
        (tmp_path / "realpkg" / "__init__.py").write_text("")
        assert top_level_import_name(tmp_path) == "realpkg"

    def test_returns_none_when_nothing_found(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "ghost"\n')
        assert top_level_import_name(tmp_path) is None


# ---------------------------------------------------------------------------
# Base-image derivation (deterministic, no LLM)
# ---------------------------------------------------------------------------

class TestBaseImageForRepo:
    def test_requires_python_pins_minor(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nrequires-python = ">=3.12"\n'
        )
        image, minor, reason = base_image_for_repo(tmp_path)
        assert image == "python:3.12-slim"
        assert minor == "3.12"

    def test_no_requires_python_defaults(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
        image, minor, _ = base_image_for_repo(tmp_path)
        assert image.endswith("-slim")
        assert minor in {"3.9", "3.10", "3.11", "3.12", "3.13"}

    def test_python_version_pin_wins_over_wider_requires_python_floor(self, tmp_path):
        # requirespy_floor edge case: .python-version=3.12 pinned,
        # requires-python>=3.9 is a floor -> keep the pin, not the floor default.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nrequires-python = ">=3.9"\n'
        )
        (tmp_path / ".python-version").write_text("3.12\n")
        image, minor, _ = base_image_for_repo(tmp_path)
        assert minor == "3.12"
        assert image == "python:3.12-slim"

    def test_unsupported_python_version_pin_is_ignored(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
        (tmp_path / ".python-version").write_text("2.7\n")
        image, minor, _ = base_image_for_repo(tmp_path)
        assert minor != "2.7"

    def test_always_slim_variant(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nrequires-python = ">=3.10"\n'
        )
        image, _, _ = base_image_for_repo(tmp_path)
        assert image.endswith("-slim")


# ---------------------------------------------------------------------------
# Aggregation: pooled recall + missing-node clusters + report rendering
# ---------------------------------------------------------------------------

def _scorecard(repo, feasible=True, matched=None, missing=None, execution_missing=()):
    matched = matched or {}
    missing = missing or {}
    tiers = ("SYSTEM_LIB", "RUNTIME", "PACKAGE", "SERVICE", "CONFIG")
    return {
        "repo": repo,
        "feasible": feasible,
        "install_ok": True,
        "oracle_recall_by_tier": {t: 1.0 for t in tiers},
        "matched_vs_oracle_by_tier": {t: matched.get(t, []) for t in tiers},
        "missing_vs_oracle_by_tier": {t: missing.get(t, []) for t in tiers},
        "execution_missing": list(execution_missing),
        "concerns": [],
    }


class TestPooledRecallByTier:
    def test_pools_counts_not_ratios(self):
        cards = [
            _scorecard("a", matched={"SYSTEM_LIB": ["x"]}, missing={"SYSTEM_LIB": []}),
            _scorecard("b", matched={"SYSTEM_LIB": []}, missing={"SYSTEM_LIB": ["y", "z"]}),
        ]
        pooled = pooled_recall_by_tier(cards)
        # 1 matched / (1 matched + 2 missing) = 1/3, NOT avg(1.0, 0.0)=0.5
        assert pooled["SYSTEM_LIB"] == 1 / 3

    def test_none_when_no_signal_anywhere(self):
        cards = [_scorecard("a"), _scorecard("b")]
        pooled = pooled_recall_by_tier(cards)
        assert pooled["SERVICE"] is None


class TestMissingNodeClusters:
    def test_ranks_by_count_desc(self):
        cards = [
            _scorecard("a", missing={"SYSTEM_LIB": ["libgl1"]}),
            _scorecard("b", missing={"SYSTEM_LIB": ["libgl1"]}),
            _scorecard("c", missing={"SYSTEM_LIB": ["libssl-dev"]}),
        ]
        clusters = missing_node_clusters(cards)
        assert clusters[0]["tier"] == "SYSTEM_LIB"
        assert clusters[0]["id"] == "libgl1"
        assert clusters[0]["count"] == 2
        assert set(clusters[0]["repos"]) == {"a", "b"}

    def test_excludes_infeasible_repos(self):
        cards = [_scorecard("a", feasible=False, missing={"SYSTEM_LIB": ["libgl1"]})]
        assert missing_node_clusters(cards) == ()

    def test_pools_execution_missing_too(self):
        cards = [_scorecard("a", execution_missing=[{"tier": "PACKAGE", "id": "click", "evidence": "x"}])]
        clusters = missing_node_clusters(cards)
        assert clusters[0]["tier"] == "PACKAGE"
        assert clusters[0]["id"] == "click"


class TestRenderReportMd:
    def test_report_contains_repo_and_cluster_info(self):
        cards = [_scorecard("org/a", missing={"SYSTEM_LIB": ["libgl1"]})]
        report = render_report_md(cards, deferred=("org/darts",))
        assert "org/a" in report
        assert "libgl1" in report
        assert "org/darts" in report
        assert "Top missing-node clusters" in report

    def test_report_is_nonempty_on_empty_corpus(self):
        report = render_report_md([])
        assert "Phase-1 Coverage Report" in report

    def test_install_failure_surfaced_with_first_command(self):
        card = _scorecard("org/b")
        card["install_ok"] = False
        card["install_first_failure"] = {"command": "pip install broken==1.0", "stderr_tail": "x"}
        report = render_report_md([card])
        assert "Install failures" in report
        assert "org/b" in report
        assert "pip install broken==1.0" in report

    def test_no_install_failures_reports_none(self):
        report = render_report_md([_scorecard("org/a")])
        assert "(none)" in report
