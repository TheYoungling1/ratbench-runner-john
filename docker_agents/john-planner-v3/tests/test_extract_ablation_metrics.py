"""
Unit tests for scripts/extract_ablation_metrics.py

Conventions (per project rules):
- unittest.TestCase only; no pytest fixtures/markers
- SimpleNamespace fakes; __new__-bypass where needed
- tempfile dirs for filesystem glob tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# Make the scripts/ directory importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from extract_ablation_metrics import (  # noqa: E402
    _infer_arm,
    _infer_replicate,
    compute_essr,
    extract_row,
    write_csv,
    write_json,
    _collect_rows,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _envstate_summary() -> dict:
    """A realistic EnvState-arm agent_run_summary.json (Arms A/B/C schema)."""
    return {
        "repo_url": "https://github.com/example/repo",
        "configuration_success": True,
        "verified_test_commands": ["pytest tests/ -q"],
        "successful_actions": [{"id": f"a{i}"} for i in range(10)],
        "failed_actions": [{"id": "f0"}],
        "action_ledger": [
            {"task_id": "t1", "action": "pip install ."},
            {"task_id": "t2", "action": "pytest", "maintainer_interpretation": {"ok": True}},
            {"task_id": "t1", "action": "apt-get install curl"},
        ],
        "token_usage": {
            "supervisor": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            "worker":     {"input_tokens": 200, "output_tokens": 80, "total_tokens": 280},
            "reflection": {"input_tokens": 20,  "output_tokens": 5,  "total_tokens": 25},
            "planner":    {"input_tokens": 0,   "output_tokens": 0,  "total_tokens": 0},
            "total":      {"input_tokens": 320, "output_tokens": 135, "total_tokens": 455},
        },
        "cleanroom": {"passed": True, "details": []},
        "envstate": {"stop_reason": "no_more_tasks", "final_revision": 3},
        "error": None,
        "steps": 28,
    }


def _arm0_summary() -> dict:
    """A legacy Arm 0 agent_run_summary.json (planner/total buckets only, no EnvState block)."""
    return {
        "repo_url": "https://github.com/example/repo",
        "configuration_success": False,
        "verified_test_commands": None,
        "verified_test_command": "python -m pytest tests/",
        "successful_actions": [{"id": f"a{i}"} for i in range(5)],
        "failed_actions": [],
        "action_ledger": [],
        # Legacy token schema: only planner + total; no supervisor/worker/reflection.
        "token_usage": {
            "planner": {"input_tokens": 500, "output_tokens": 60, "total_tokens": 560},
            "total":   {"input_tokens": 500, "output_tokens": 60, "total_tokens": 560},
        },
        # Arm 0 has no cleanroom, no envstate block, no final_rev.
        "error": None,
        "steps": 75,
    }


def _results_json(success: bool = True) -> dict:
    """Minimal results/<id>.json structure."""
    return {
        "environment_build_success": success,
        "dockerfile_generation_success": success,
        "execution_status": "success" if success else "test_failure",
        "dataset_entry": {"paper_build_success": True, "instance_id": "foo__bar"},
        "agent_run": {"duration_seconds": 120.5},
    }


# ---------------------------------------------------------------------------
# Path inference tests
# ---------------------------------------------------------------------------

class TestInferArm(unittest.TestCase):
    def test_arm0_from_path(self):
        p = Path("/outputs/ablation/arm0_bare_react/rep1/workplaces/foo/agent_run_summary.json")
        self.assertEqual(_infer_arm(p), "0")

    def test_armA_from_path(self):
        p = Path("/outputs/ablation/armA_fullstate/rep2/results/foo.json")
        self.assertEqual(_infer_arm(p), "A")

    def test_armB_from_path(self):
        p = Path("/outputs/ablation/armB_supervisor/rep3/results/foo.json")
        self.assertEqual(_infer_arm(p), "B")

    def test_armC_from_path(self):
        p = Path("/outputs/ablation/armC_matched_planner/rep5/results/foo.json")
        self.assertEqual(_infer_arm(p), "C")

    def test_no_arm_returns_none(self):
        p = Path("/outputs/other_dir/rep1/results/foo.json")
        self.assertIsNone(_infer_arm(p))

    def test_case_insensitive(self):
        p = Path("/outputs/ARMA_fullstate/rep1/results/foo.json")
        self.assertEqual(_infer_arm(p), "A")


class TestInferReplicate(unittest.TestCase):
    def test_rep_number_extracted(self):
        p = Path("/outputs/armB_supervisor/rep3/results/foo.json")
        self.assertEqual(_infer_replicate(p), 3)

    def test_rep1(self):
        p = Path("/path/to/arm0_bare_react/rep1/workplaces/id/summary.json")
        self.assertEqual(_infer_replicate(p), 1)

    def test_no_rep_returns_none(self):
        p = Path("/outputs/armB_supervisor/results/foo.json")
        self.assertIsNone(_infer_replicate(p))


# ---------------------------------------------------------------------------
# extract_row: EnvState arm (full schema)
# ---------------------------------------------------------------------------

class TestExtractRowEnvStateArm(unittest.TestCase):
    def setUp(self):
        self.summary = _envstate_summary()
        self.results = _results_json(success=True)
        self.row = extract_row(
            summary=self.summary,
            results=self.results,
            arm="B",
            instance_id="example__repo",
            replicate=2,
        )

    def test_identity_fields(self):
        self.assertEqual(self.row["instance_id"], "example__repo")
        self.assertEqual(self.row["arm"], "B")
        self.assertEqual(self.row["replicate"], 2)

    def test_primary_outcome_from_results(self):
        self.assertEqual(self.row["environment_build_success"], 1)
        self.assertEqual(self.row["dockerfile_generation_success"], 1)
        self.assertEqual(self.row["execution_status"], "success")

    def test_configuration_success_from_summary(self):
        self.assertEqual(self.row["configuration_success"], 1)

    def test_cleanroom_passed(self):
        self.assertEqual(self.row["cleanroom_passed"], 1)

    def test_verified_test_commands(self):
        self.assertEqual(self.row["verified_test_commands"], ["pytest tests/ -q"])

    def test_token_buckets(self):
        self.assertEqual(self.row["total_tokens"], 455)
        self.assertEqual(self.row["supervisor_tokens"], 150)
        self.assertEqual(self.row["worker_tokens"], 280)
        self.assertEqual(self.row["reflection_tokens"], 25)

    def test_n_actions_from_successful_plus_failed(self):
        # 10 successful + 1 failed = 11
        self.assertEqual(self.row["n_actions"], 11)

    def test_n_tasks_proxy_distinct_task_ids(self):
        # action_ledger has task_ids t1, t2, t1 → 2 distinct
        self.assertEqual(self.row["n_tasks_proxy"], 2)

    def test_no_more_tasks_flag(self):
        self.assertEqual(self.row["no_more_tasks"], 1)

    def test_n_maintainer_count(self):
        # Only the t2 entry has maintainer_interpretation
        self.assertEqual(self.row["n_maintainer"], 1)

    def test_final_rev(self):
        self.assertEqual(self.row["final_rev"], 3)

    def test_duration_seconds(self):
        self.assertAlmostEqual(self.row["duration_seconds"], 120.5)

    def test_paper_build_success(self):
        self.assertTrue(self.row["paper_build_success"])

    def test_no_error(self):
        self.assertEqual(self.row["has_error"], 0)


# ---------------------------------------------------------------------------
# extract_row: Arm 0 legacy summary (no EnvState block)
# ---------------------------------------------------------------------------

class TestExtractRowArm0Legacy(unittest.TestCase):
    def setUp(self):
        self.summary = _arm0_summary()
        self.results = _results_json(success=False)
        self.row = extract_row(
            summary=self.summary,
            results=self.results,
            arm="0",
            instance_id="example__repo",
            replicate=1,
        )

    def test_arm_and_instance(self):
        self.assertEqual(self.row["arm"], "0")
        self.assertEqual(self.row["instance_id"], "example__repo")

    def test_success_from_results(self):
        self.assertEqual(self.row["environment_build_success"], 0)

    def test_configuration_success_false(self):
        self.assertEqual(self.row["configuration_success"], 0)

    def test_cleanroom_is_null_not_error(self):
        # Arm 0 has no cleanroom block → must be null, not raise
        self.assertIsNone(self.row["cleanroom_passed"])

    def test_supervisor_tokens_is_null(self):
        # Arm 0 legacy schema has no supervisor bucket
        self.assertIsNone(self.row["supervisor_tokens"])

    def test_worker_tokens_is_null(self):
        self.assertIsNone(self.row["worker_tokens"])

    def test_reflection_tokens_is_null(self):
        self.assertIsNone(self.row["reflection_tokens"])

    def test_planner_tokens_present(self):
        # Arm 0 has the legacy planner bucket
        self.assertEqual(self.row["planner_tokens"], 560)

    def test_total_tokens_present(self):
        self.assertEqual(self.row["total_tokens"], 560)

    def test_final_rev_is_null(self):
        self.assertIsNone(self.row["final_rev"])

    def test_no_more_tasks_is_null(self):
        # Arm 0 has no orchestrator → no_more_tasks = null
        self.assertIsNone(self.row["no_more_tasks"])

    def test_n_actions_from_successful_failed(self):
        # 5 successful + 0 failed = 5
        self.assertEqual(self.row["n_actions"], 5)

    def test_verified_test_command_fallback(self):
        # Arm 0 summary may carry singular 'verified_test_command'; must be tolerated.
        self.assertEqual(self.row["verified_test_commands"], ["python -m pytest tests/"])

    def test_no_error(self):
        self.assertEqual(self.row["has_error"], 0)


# ---------------------------------------------------------------------------
# extract_row: error / missing cases → success=0, kept in denominator
# ---------------------------------------------------------------------------

class TestExtractRowErrorCases(unittest.TestCase):
    def test_none_summary_produces_zero_success(self):
        row = extract_row(
            summary=None,
            results=None,
            arm="B",
            instance_id="missing__repo",
            replicate=1,
        )
        self.assertEqual(row["environment_build_success"], 0)
        self.assertEqual(row["configuration_success"], 0)
        self.assertEqual(row["instance_id"], "missing__repo")

    def test_error_field_in_summary_sets_has_error(self):
        summary = _arm0_summary()
        summary["error"] = "some crash"
        row = extract_row(
            summary=summary,
            results=None,
            arm="0",
            instance_id="crash__repo",
            replicate=1,
        )
        self.assertEqual(row["has_error"], 1)
        # environment_build_success comes from results (None here) → 0
        self.assertEqual(row["environment_build_success"], 0)

    def test_empty_dict_summary_no_crash(self):
        row = extract_row(
            summary={},
            results={},
            arm="A",
            instance_id="empty__repo",
            replicate=2,
        )
        self.assertEqual(row["environment_build_success"], 0)
        self.assertIsNone(row["supervisor_tokens"])

    def test_malformed_token_usage_tolerated(self):
        summary = _envstate_summary()
        summary["token_usage"] = "not_a_dict"
        row = extract_row(
            summary=summary,
            results=_results_json(),
            arm="B",
            instance_id="tok__repo",
            replicate=1,
        )
        self.assertIsNone(row["total_tokens"])
        self.assertIsNone(row["supervisor_tokens"])


# ---------------------------------------------------------------------------
# ESSR ÷ ALL denominator test (failures stay in denominator)
# ---------------------------------------------------------------------------

class TestComputeESSR(unittest.TestCase):
    def test_failures_stay_in_denominator(self):
        rows = [
            {"arm": "B", "environment_build_success": 1},
            {"arm": "B", "environment_build_success": 0},  # failure
            {"arm": "B", "environment_build_success": 0},  # failure
            {"arm": "0", "environment_build_success": 1},
            {"arm": "0", "environment_build_success": 1},
        ]
        essr = compute_essr(rows)
        # Arm B: 1 success / 3 assigned = 0.3333
        self.assertEqual(essr["B"]["assigned"], 3)
        self.assertEqual(essr["B"]["success"], 1)
        self.assertAlmostEqual(essr["B"]["essr_div_all"], 1 / 3, places=3)
        # Arm 0: 2 success / 2 assigned = 1.0
        self.assertEqual(essr["0"]["assigned"], 2)
        self.assertEqual(essr["0"]["success"], 2)
        self.assertAlmostEqual(essr["0"]["essr_div_all"], 1.0, places=3)

    def test_all_failures_denominator_nonzero(self):
        rows = [
            {"arm": "A", "environment_build_success": 0},
            {"arm": "A", "environment_build_success": 0},
        ]
        essr = compute_essr(rows)
        self.assertEqual(essr["A"]["assigned"], 2)
        self.assertEqual(essr["A"]["success"], 0)
        self.assertEqual(essr["A"]["essr_div_all"], 0.0)

    def test_empty_rows(self):
        essr = compute_essr([])
        self.assertEqual(essr, {})


# ---------------------------------------------------------------------------
# Filesystem glob test using tempfile dirs
# ---------------------------------------------------------------------------

class TestCollectRows(unittest.TestCase):
    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_glob_finds_summaries_and_pairs_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # Arm B, rep1, instance foo__bar
            arm_b_rep1 = root / "armB_supervisor" / "rep1"
            summary_path = arm_b_rep1 / "workplaces" / "foo__bar" / "agent_run_summary.json"
            results_path = arm_b_rep1 / "results" / "foo__bar.json"
            self._write_json(summary_path, _envstate_summary())
            self._write_json(results_path, _results_json(success=True))

            # Arm 0, rep1, instance baz__qux
            arm0_rep1 = root / "arm0_bare_react" / "rep1"
            summary_path0 = arm0_rep1 / "workplaces" / "baz__qux" / "agent_run_summary.json"
            # No results JSON for this one — should tolerate gracefully.
            self._write_json(summary_path0, _arm0_summary())

            rows = _collect_rows(root)

        self.assertEqual(len(rows), 2)

        by_id = {r["instance_id"]: r for r in rows}

        # EnvState arm row
        b_row = by_id["foo__bar"]
        self.assertEqual(b_row["arm"], "B")
        self.assertEqual(b_row["replicate"], 1)
        self.assertEqual(b_row["environment_build_success"], 1)
        self.assertEqual(b_row["supervisor_tokens"], 150)

        # Arm 0 row — missing results json → environment_build_success = 0
        a0_row = by_id["baz__qux"]
        self.assertEqual(a0_row["arm"], "0")
        self.assertEqual(a0_row["replicate"], 1)
        self.assertEqual(a0_row["environment_build_success"], 0)
        # Cleanroom must be null, not an error
        self.assertIsNone(a0_row["cleanroom_passed"])
        self.assertIsNone(a0_row["supervisor_tokens"])

    def test_write_csv_and_json_roundtrip(self):
        rows = [
            extract_row(_envstate_summary(), _results_json(True), "B", "foo__bar", 1),
            extract_row(_arm0_summary(), _results_json(False), "0", "baz__qux", 1),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "out.csv"
            json_path = root / "out.json"
            write_csv(rows, csv_path)
            write_json(rows, json_path)

            self.assertTrue(csv_path.exists())
            self.assertTrue(json_path.exists())

            loaded = json.loads(json_path.read_text())
            self.assertEqual(len(loaded), 2)
            ids = {r["instance_id"] for r in loaded}
            self.assertIn("foo__bar", ids)
            self.assertIn("baz__qux", ids)


if __name__ == "__main__":
    unittest.main()
