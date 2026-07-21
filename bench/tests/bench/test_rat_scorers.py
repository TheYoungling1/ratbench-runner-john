# tests/bench/test_rat_scorers.py
"""Behavior tests for the copied RAT scorers (bench/rat_scorers.py).

The scorers read run-dir JSON at {root_path}/output/{full_name}/*.json, plus
success_scorer which only reads output["status"]. We build synthetic run dirs
under tmp_path and, when the rat50 fixtures exist, sanity-check real repo dirs.
"""
import json
import os
from pathlib import Path

import pytest

from bench.rat_scorers import (
    success_scorer,
    pytest_pass_rate_scorer,
    pytest_collect_scorer,
)


def _write_run_dir(root: Path, full_name: str, filename: str, payload: dict) -> dict:
    """Materialize {root}/output/{full_name}/{filename} and return the out dict."""
    out_dir = root / "output" / full_name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / filename).write_text(json.dumps(payload), encoding="utf-8")
    return {"status": "success", "root_path": str(root), "full_name": full_name}


# ── success_scorer ───────────────────────────────────────────────────────────

def test_success_scorer_success():
    assert success_scorer({"status": "success"}) == {"success": True}


def test_success_scorer_error():
    assert success_scorer({"status": "error"}) == {"success": False}


def test_success_scorer_missing_status():
    assert success_scorer({}) == {"success": False}


# ── pytest_pass_rate_scorer ──────────────────────────────────────────────────

def test_pass_rate_executed_all_pass(tmp_path):
    out = _write_run_dir(
        tmp_path, "acme/lib", "run_pytest_results.json",
        {"summary": {"total_tests": 10, "passed": 10, "failed": 0,
                     "errors": 0, "skipped": 0},
         "error_breakdown": {}},
    )
    r = pytest_pass_rate_scorer(out)
    assert r["pytest_executed"] is True
    assert r["pytest_pass_rate"] == 1.0
    assert r["pass_rate_exclude_code_issues"] == 1.0
    assert r["pytest_total_tests"] == 10
    assert r["pytest_passed"] == 10
    assert r["pytest_timeout_unverified"] is False
    # Every documented key must be present.
    for k in ("pytest_pass_rate", "pytest_total_tests", "pytest_passed",
              "pytest_failed", "pytest_errors", "pytest_executed",
              "pytest_timeout_unverified", "error_breakdown",
              "pass_rate_exclude_code_issues"):
        assert k in r


def test_pass_rate_executed_partial_fail(tmp_path):
    # 8/10 pass, 2 fail; skipped excluded from denominator.
    out = _write_run_dir(
        tmp_path, "acme/lib", "run_pytest_results.json",
        {"summary": {"total_tests": 10, "passed": 8, "failed": 2,
                     "errors": 0, "skipped": 0},
         "error_breakdown": {"AssertionError": 2}},
    )
    r = pytest_pass_rate_scorer(out)
    assert r["pytest_executed"] is True
    assert r["pytest_pass_rate"] == 0.8
    assert r["pytest_failed"] == 2


def test_pass_rate_skipped_excluded_from_denominator(tmp_path):
    # effective_total = total - skipped = 8; 4/8 = 0.5
    out = _write_run_dir(
        tmp_path, "acme/lib", "run_pytest_results.json",
        {"summary": {"total_tests": 10, "passed": 4, "failed": 4,
                     "errors": 0, "skipped": 2},
         "error_breakdown": {}},
    )
    r = pytest_pass_rate_scorer(out)
    assert r["pytest_pass_rate"] == 0.5


def test_pass_rate_exclude_code_issues(tmp_path):
    # denom = passed + ModuleNotFoundError + ImportError = 6 + 3 + 1 = 10 -> 0.6
    out = _write_run_dir(
        tmp_path, "acme/lib", "run_pytest_results.json",
        {"summary": {"total_tests": 20, "passed": 6, "failed": 14,
                     "errors": 0, "skipped": 0},
         "error_breakdown": {"ModuleNotFoundError": 3, "ImportError": 1,
                             "AssertionError": 10}},
    )
    r = pytest_pass_rate_scorer(out)
    assert r["pass_rate_exclude_code_issues"] == 0.6


def test_pass_rate_timeout_only_no_tests_unverified(tmp_path):
    out = _write_run_dir(
        tmp_path, "acme/lib", "run_pytest_results.json",
        {"summary": {"total_tests": 0, "passed": 0, "failed": 0,
                     "errors": 0, "skipped": 0},
         "error_breakdown": {"TimeoutError": 1}},
    )
    r = pytest_pass_rate_scorer(out)
    assert r["pytest_timeout_unverified"] is True
    assert r["pytest_pass_rate"] == 0.0
    assert r["pass_rate_exclude_code_issues"] == 0.0


def test_pass_rate_not_executed_missing_ids():
    # No root_path / full_name -> default (not executed).
    r = pytest_pass_rate_scorer({"status": "success"})
    assert r["pytest_executed"] is False
    assert r["pytest_pass_rate"] == 0.0
    assert r["pass_rate_exclude_code_issues"] == 0.0
    assert r["error_breakdown"] == {}


def test_pass_rate_not_executed_missing_file(tmp_path):
    # Valid ids but no results file on disk -> default.
    out = {"status": "success", "root_path": str(tmp_path), "full_name": "acme/missing"}
    r = pytest_pass_rate_scorer(out)
    assert r["pytest_executed"] is False
    assert r["pytest_pass_rate"] == 0.0


# ── pytest_collect_scorer ────────────────────────────────────────────────────

def test_collect_success(tmp_path):
    out = _write_run_dir(
        tmp_path, "acme/lib", "run_pytest_collect_results.json",
        {"success": True, "returncode": 0, "errors": []},
    )
    assert pytest_collect_scorer(out) == {"pytest_collect_success": True}


def test_collect_failure(tmp_path):
    out = _write_run_dir(
        tmp_path, "acme/lib", "run_pytest_collect_results.json",
        {"success": False, "returncode": 2, "errors": ["ModuleNotFoundError"]},
    )
    assert pytest_collect_scorer(out) == {"pytest_collect_success": False}


def test_collect_not_executed_missing_ids():
    assert pytest_collect_scorer({"status": "error"}) == {"pytest_collect_success": False}


def test_collect_not_executed_missing_file(tmp_path):
    out = {"status": "success", "root_path": str(tmp_path), "full_name": "acme/missing"}
    assert pytest_collect_scorer(out) == {"pytest_collect_success": False}


# ── Real rat50 fixtures (only if present) ────────────────────────────────────

_RAT50 = Path.home() / "env-bench-fixtures" / "rat50"


def _fixture_full_names(filename: str, limit: int = 5):
    root = _RAT50
    hits = sorted(str(p.parent.relative_to(root / "output"))
                  for p in (root / "output").rglob(filename))
    return hits[:limit]


@pytest.mark.skipif(not _RAT50.exists(), reason="rat50 fixtures not present")
def test_pass_rate_over_real_fixtures():
    names = _fixture_full_names("run_pytest_results.json")
    assert names, "expected at least one real pytest-results fixture"
    for full_name in names:
        out = {"status": "success", "root_path": str(_RAT50), "full_name": full_name}
        r = pytest_pass_rate_scorer(out)
        assert r["pytest_executed"] is True
        assert isinstance(r["pytest_pass_rate"], float)
        assert 0.0 <= r["pytest_pass_rate"] <= 1.0
        assert 0.0 <= r["pass_rate_exclude_code_issues"] <= 1.0
        assert isinstance(r["pytest_timeout_unverified"], bool)
        assert isinstance(r["error_breakdown"], dict)


@pytest.mark.skipif(not _RAT50.exists(), reason="rat50 fixtures not present")
def test_collect_over_real_fixtures():
    names = _fixture_full_names("run_pytest_collect_results.json")
    assert names, "expected at least one real collect fixture"
    for full_name in names:
        out = {"status": "success", "root_path": str(_RAT50), "full_name": full_name}
        r = pytest_collect_scorer(out)
        assert set(r.keys()) == {"pytest_collect_success"}
        assert isinstance(r["pytest_collect_success"], bool)
