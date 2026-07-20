"""Comparer — recall/precision/version-agreement and the platform_optional_extra
filter-escape bucket, on synthetic OURS/ORACLE records."""
from __future__ import annotations

from src.eval.language_package_eval.node.compare_node import score_repo


def _ours(packages, constraints=None, project="proj"):
    return {"packages": packages, "constraints": constraints or {}, "project": project,
            "unfiltered_count": len(packages), "platform_dropped_count": 0}


def _oracle(installed, repo="proj"):
    return {"installed": installed, "repo": repo}


def test_perfect_lock_match_is_recall_and_precision_one():
    s = score_repo(
        _ours({"esbuild": "0.21.5", "@esbuild/linux-x64": "0.21.5", "rollup": "4.18.0"}),
        _oracle({"esbuild": "0.21.5", "@esbuild/linux-x64": "0.21.5", "rollup": "4.18.0"}),
    )
    assert s["recall"] == 1.0 and s["precision"] == 1.0
    assert s["missing"] == [] and s["extra"] == []
    assert s["vexact"] == 3


def test_filter_escape_lands_in_platform_optional_bucket():
    # ours over-included a win32 binary the filter should have dropped
    s = score_repo(
        _ours(
            {"a": "1", "b": "2", "@s/win32-x64": "3"},
            constraints={"@s/win32-x64": {"os": ["win32"], "cpu": ["x64"], "libc": []}},
        ),
        _oracle({"a": "1", "b": "2"}),
    )
    assert s["precision"] == 2 / 3
    assert s["platform_optional_extra"] == ["@s/win32-x64"]
    assert s["extra"] == []                     # NOT in the plain bucket


def test_under_coverage_is_a_missing_bug_signal():
    s = score_repo(_ours({"a": "1"}), _oracle({"a": "1", "b": "2"}))
    assert s["recall"] == 0.5
    assert s["missing"] == ["b"]


def test_version_disagreement_counted():
    s = score_repo(_ours({"a": "1.0.0"}), _oracle({"a": "1.2.0"}))
    assert s["inter"] == 1
    assert s["vexact"] == 0 and s["vmm"] == 0   # 1.0 != 1.2


def test_own_package_excluded_both_sides():
    s = score_repo(
        _ours({"proj": "9.9.9", "a": "1"}, project="proj"),
        _oracle({"proj": "9.9.9", "a": "1"}, repo="proj"),
    )
    assert s["ours_n"] == 1 and s["installed_n"] == 1   # 'proj' excluded


def test_infeasible_oracle_empty_install_no_crash():
    # a repo whose `npm ci` failed (installed={}) -> recall None, precision 0.0, no crash
    s = score_repo(_ours({"a": "1", "b": "2"}), _oracle({}))
    assert s["recall"] is None          # |installed| == 0
    assert s["precision"] == 0.0
    assert s["missing"] == [] and s["inter"] == 0


def test_npm_names_not_pep503_collapsed():
    # underscore and dash are DISTINCT in npm — must not be conflated
    s = score_repo(_ours({"a_b": "1"}), _oracle({"a-b": "1"}))
    assert s["recall"] == 0.0                   # a_b != a-b (no PEP503 collapse)
    assert s["missing"] == ["a-b"] and s["extra"] == ["a_b"]
