"""TDD tests for compute_essr.py Stage E: repaired column detection.

Tests:
- score_agent returns repaired_count and repaired_pass_rate fields
- Repair sidecar detection is backward-compatible (old runs without sidecar still score identically)
- repair_meta.json with repair_rounds>0 increments repaired_count
- repair_meta.json with repair_rounds==0 does NOT increment repaired_count
- repaired_pass_rate is the average pass_rate over repos that were repaired
- score_agent never raises even if repair_meta.json is corrupted
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Insert project root into path so imports work without install
_PROJ_ROOT = str(Path(__file__).parent.parent)
if _PROJ_ROOT not in sys.path:
    sys.path.insert(0, _PROJ_ROOT)

from scripts.compute_essr import score_agent, official_pass_rate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_result_row(repo_dir: Path, pytest_pass_rate: float = 0.0, pytest_executed: bool = False) -> None:
    """Write a minimal _result_row.json."""
    (repo_dir / "_result_row.json").write_text(json.dumps({
        "pytest_pass_rate": pytest_pass_rate,
        "pytest_executed": pytest_executed,
        "pytest_collect_success": False,
    }))


def _write_pytest_results(repo_dir: Path, passed: int = 1, total: int = 1, failed: int = 0) -> None:
    """Write a minimal run_pytest_results.json."""
    (repo_dir / "run_pytest_results.json").write_text(json.dumps({
        "summary": {
            "total_tests": total,
            "passed": passed,
            "failed": failed,
            "errors": 0,
            "skipped": 0,
            "xfailed": 0,
            "xpassed": 0,
        },
        "error_breakdown": {},
        "failed_tests": [],
        "error_tests": [],
        "raw_output": "",
        "returncode": 0 if failed == 0 else 1,
        "parse_method": "junit_xml",
    }))


def _write_repair_meta(repo_dir: Path, repair_rounds: int = 1, validation_attempts: int = 2) -> None:
    """Write a repair_artifacts/repair_meta.json sidecar."""
    repair_dir = repo_dir / "repair_artifacts"
    repair_dir.mkdir(parents=True, exist_ok=True)
    (repair_dir / "repair_meta.json").write_text(json.dumps({
        "full_name": "owner/repo",
        "repair_rounds": repair_rounds,
        "validation_attempts": validation_attempts,
        "repair_history": [],
    }))


def _setup_root_path(tmp_path: Path, repos: list[dict]) -> str:
    """
    Set up a root_path with output/<owner>/<repo> directories.

    Each repo dict may have: owner, name, passed, total, failed, repair_rounds, write_result_row
    """
    root = tmp_path
    for r in repos:
        owner = r.get("owner", "owner")
        name = r.get("name", "repo")
        repo_dir = root / "output" / owner / name
        repo_dir.mkdir(parents=True, exist_ok=True)

        # Always write _result_row.json for it to be discovered
        if r.get("write_result_row", True):
            passed = r.get("passed", 1)
            total = r.get("total", 1)
            pr = round(passed / total, 4) if total > 0 else 0.0
            _write_result_row(repo_dir, pytest_pass_rate=pr, pytest_executed=True)

        if r.get("write_pytest_results", True):
            _write_pytest_results(
                repo_dir,
                passed=r.get("passed", 1),
                total=r.get("total", 1),
                failed=r.get("failed", 0),
            )

        if "repair_rounds" in r:
            _write_repair_meta(repo_dir, repair_rounds=r["repair_rounds"])

    return str(root)


# ---------------------------------------------------------------------------
# Stage E: backward-compatibility (no repair sidecar)
# ---------------------------------------------------------------------------

class TestRepairColumnBackwardCompat:
    """score_agent must return the same ESSR figures for runs that have no repair sidecar."""

    def test_repaired_count_zero_when_no_sidecar(self, tmp_path):
        """If no repair_meta.json exists, repaired_count must be 0."""
        root = _setup_root_path(tmp_path, [
            {"owner": "a", "name": "r1", "passed": 3, "total": 4},
            {"owner": "a", "name": "r2", "passed": 2, "total": 2},
        ])
        result = score_agent(root)
        assert result.get("repaired_count", 0) == 0, (
            f"Expected repaired_count=0 when no repair sidecars present, got {result.get('repaired_count')}"
        )

    def test_essr_unchanged_without_sidecar(self, tmp_path):
        """score_agent ESSR must equal the value computed from raw pytest results alone."""
        root = _setup_root_path(tmp_path, [
            {"owner": "a", "name": "r1", "passed": 3, "total": 4},
            {"owner": "b", "name": "r2", "passed": 1, "total": 1},
        ])
        result = score_agent(root)
        # ESSR = avg over executed: (3/4 + 1/1) / 2 = (0.75 + 1.0) / 2 = 0.875
        assert abs(result["ESSR_avg_pass_rate_official"] - 0.875) < 0.002, (
            f"ESSR should be 0.875, got {result['ESSR_avg_pass_rate_official']}"
        )

    def test_score_agent_has_repaired_fields(self, tmp_path):
        """score_agent must always return repaired_count and repaired_pass_rate keys."""
        root = _setup_root_path(tmp_path, [
            {"owner": "a", "name": "r1"},
        ])
        result = score_agent(root)
        assert "repaired_count" in result, "Missing repaired_count key in score_agent output"
        assert "repaired_pass_rate" in result, "Missing repaired_pass_rate key in score_agent output"

    def test_repaired_pass_rate_zero_when_no_repairs(self, tmp_path):
        """repaired_pass_rate must be 0.0 when no repos were repaired."""
        root = _setup_root_path(tmp_path, [
            {"owner": "a", "name": "r1"},
        ])
        result = score_agent(root)
        assert result.get("repaired_pass_rate", 0.0) == 0.0, (
            f"Expected repaired_pass_rate=0.0, got {result.get('repaired_pass_rate')}"
        )


# ---------------------------------------------------------------------------
# Stage E: repair sidecar detection
# ---------------------------------------------------------------------------

class TestRepairSidecarDetection:
    """Detect repair_meta.json and surface repair lift."""

    def test_repaired_count_increments_for_repaired_repo(self, tmp_path):
        """A repo with repair_rounds>0 in repair_meta.json must increment repaired_count."""
        root = _setup_root_path(tmp_path, [
            {"owner": "a", "name": "r1", "passed": 2, "total": 2, "repair_rounds": 1},
            {"owner": "a", "name": "r2", "passed": 1, "total": 1},  # no repair
        ])
        result = score_agent(root)
        assert result["repaired_count"] == 1, (
            f"Expected repaired_count=1 (one repo has repair_rounds>0), got {result['repaired_count']}"
        )

    def test_repair_rounds_zero_not_counted(self, tmp_path):
        """repair_meta.json with repair_rounds==0 must NOT count as repaired."""
        root = _setup_root_path(tmp_path, [
            {"owner": "a", "name": "r1", "passed": 1, "total": 1, "repair_rounds": 0},
        ])
        result = score_agent(root)
        assert result["repaired_count"] == 0, (
            f"repair_rounds=0 should not count as repaired, got {result['repaired_count']}"
        )

    def test_repaired_pass_rate_is_avg_over_repaired_repos(self, tmp_path):
        """repaired_pass_rate = avg pass_rate over repos with repair_rounds>0."""
        # r1: repaired, passes all (1.0), r2: repaired, passes half (0.5), r3: not repaired
        root = _setup_root_path(tmp_path, [
            {"owner": "a", "name": "r1", "passed": 2, "total": 2, "repair_rounds": 2},
            {"owner": "a", "name": "r2", "passed": 1, "total": 2, "failed": 1, "repair_rounds": 1},
            {"owner": "a", "name": "r3", "passed": 3, "total": 3},  # not repaired
        ])
        result = score_agent(root)
        # repaired_pass_rate = avg(1.0, 0.5) = 0.75
        assert abs(result["repaired_pass_rate"] - 0.75) < 0.002, (
            f"Expected repaired_pass_rate=0.75, got {result['repaired_pass_rate']}"
        )

    def test_multiple_repaired_repos_count(self, tmp_path):
        """Two repos repaired = repaired_count=2."""
        root = _setup_root_path(tmp_path, [
            {"owner": "a", "name": "r1", "repair_rounds": 1},
            {"owner": "a", "name": "r2", "repair_rounds": 2},
            {"owner": "a", "name": "r3"},
        ])
        result = score_agent(root)
        assert result["repaired_count"] == 2, (
            f"Expected repaired_count=2, got {result['repaired_count']}"
        )

    def test_rows_include_repair_rounds_field(self, tmp_path):
        """Each row in result['rows'] should include repair_rounds (or None when absent)."""
        root = _setup_root_path(tmp_path, [
            {"owner": "a", "name": "r1", "repair_rounds": 1},
            {"owner": "a", "name": "r2"},  # no sidecar
        ])
        result = score_agent(root)
        rows = result.get("rows", [])
        assert len(rows) == 2
        # At least one row should have a non-None repair_rounds value
        repair_round_values = [r.get("repair_rounds") for r in rows]
        assert any(v is not None and v > 0 for v in repair_round_values), (
            f"Expected at least one row with repair_rounds>0, got: {repair_round_values}"
        )


# ---------------------------------------------------------------------------
# Stage E: never-raises contract
# ---------------------------------------------------------------------------

class TestRepairColumnNeverRaises:
    """compute_essr must never raise even when repair sidecars are corrupted."""

    def test_corrupted_repair_meta_does_not_crash(self, tmp_path):
        """Corrupted repair_meta.json must not cause score_agent to raise."""
        root = _setup_root_path(tmp_path, [
            {"owner": "a", "name": "r1"},
        ])
        # Overwrite repair_meta.json with garbage
        repair_dir = tmp_path / "output" / "a" / "r1" / "repair_artifacts"
        repair_dir.mkdir(parents=True, exist_ok=True)
        (repair_dir / "repair_meta.json").write_text("NOT VALID JSON {{{")

        # Must not raise
        result = score_agent(str(tmp_path))
        assert isinstance(result, dict)
        assert "ESSR_avg_pass_rate_official" in result

    def test_empty_repair_meta_does_not_crash(self, tmp_path):
        """Empty repair_meta.json must not crash."""
        root = _setup_root_path(tmp_path, [
            {"owner": "a", "name": "r1"},
        ])
        repair_dir = tmp_path / "output" / "a" / "r1" / "repair_artifacts"
        repair_dir.mkdir(parents=True, exist_ok=True)
        (repair_dir / "repair_meta.json").write_text("{}")

        result = score_agent(str(tmp_path))
        assert isinstance(result, dict)

    def test_empty_root_path_returns_valid_dict(self, tmp_path):
        """score_agent on an empty directory must return a valid dict with 0 counts."""
        result = score_agent(str(tmp_path))
        assert isinstance(result, dict)
        assert result.get("n", 0) == 0
        assert result.get("repaired_count", 0) == 0
        assert result.get("repaired_pass_rate", 0.0) == 0.0


# ---------------------------------------------------------------------------
# official_pass_rate unit tests (sanity check existing function)
# ---------------------------------------------------------------------------

class TestOfficialPassRate:
    def test_simple_pass_rate(self):
        results = {"summary": {"total_tests": 4, "passed": 3, "skipped": 0}, "error_breakdown": {}}
        pr, pr_excl, passed, eff = official_pass_rate(results)
        assert pr == 0.75
        assert passed == 3
        assert eff == 4

    def test_zero_effective_total(self):
        results = {"summary": {"total_tests": 2, "passed": 0, "skipped": 2}, "error_breakdown": {}}
        pr, _, passed, eff = official_pass_rate(results)
        assert pr == 0.0
        assert eff == 0
