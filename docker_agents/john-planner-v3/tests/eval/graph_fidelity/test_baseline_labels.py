"""TDD tests for src/eval/graph_fidelity/baseline_labels.py.

Two concerns under test, kept separate per the module's own split:
  - `rescore()` — pure re-score of one repo's raw pytest-results JSON. Offline, no VM, no
    network. This is the honest-scoring surface (§2/§7 of the graph-fidelity eval loop doc):
    it MUST delegate to `scripts.compute_essr.official_pass_rate`, never reimplement scoring.
  - `build_labels()` — thin aggregation over a raw-cache directory tree, no VM contact (the
    VM fetch itself, `fetch_baseline_raw`/`fetch_dataset`, is exercised manually against the
    real VM during bootstrap, not unit-tested here).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.eval.graph_fidelity.baseline_labels import (
    BASELINE_RUN_DIRS,
    build_labels,
    rescore,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> Dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# rescore() — pure, offline
# ---------------------------------------------------------------------------

class TestRescoreAllPass:
    def test_all_pass_gives_pass_rate_one(self):
        result = rescore(_load_fixture("all_pass.json"))
        assert result["pass_rate"] == 1.0
        assert result["honest_pass"] is True
        assert result["present"] is True


class TestRescoreBelowTau:
    def test_below_tau_is_honest_fail(self):
        result = rescore(_load_fixture("below_tau.json"))
        # 6/10 = 0.6 < tau (0.8)
        assert result["pass_rate"] == 0.6
        assert result["honest_pass"] is False
        assert result["present"] is True

    def test_at_tau_boundary_is_honest_pass(self):
        result = rescore(_load_fixture("at_tau.json"))
        # 8/10 = 0.8 == tau -> honest_pass (>=, not >)
        assert result["pass_rate"] == 0.8
        assert result["honest_pass"] is True


class TestRescoreEmptyOrMissing:
    def test_empty_summary_is_honest_fail(self):
        result = rescore(_load_fixture("empty_results.json"))
        assert result["pass_rate"] == 0.0
        assert result["honest_pass"] is False
        # the file existed and parsed -> present, just scored honestly at 0
        assert result["present"] is True

    def test_missing_file_is_present_false_not_invented(self):
        # None represents "no results file for this repo under this baseline" (§3/§6).
        result = rescore(None)
        assert result["present"] is False
        assert result["pass_rate"] == 0.0
        assert result["honest_pass"] is False

    def test_malformed_results_does_not_raise(self):
        # A present-but-garbage results blob must not crash the harness.
        result = rescore({"summary": None, "error_breakdown": {}})
        assert result["honest_pass"] is False


class TestRescoreDelegatesToComputeEssr:
    """Honest-scoring guardrail: rescore must not reimplement pytest scoring — it must call
    the repo's single scoring authority, scripts.compute_essr.official_pass_rate."""

    def test_rescore_calls_official_pass_rate(self):
        fixture = _load_fixture("all_pass.json")
        with patch(
            "src.eval.graph_fidelity.baseline_labels.official_pass_rate",
            wraps=__import__("scripts.compute_essr", fromlist=["official_pass_rate"]).official_pass_rate,
        ) as mocked:
            rescore(fixture)
            mocked.assert_called_once_with(fixture)


# ---------------------------------------------------------------------------
# build_labels() — thin aggregation over a raw-cache tree (no VM)
# ---------------------------------------------------------------------------

def _write_raw(raw_dir: Path, baseline: str, org: str, name: str, fixture: Optional[str]) -> None:
    if fixture is None:
        return
    repo_dir = raw_dir / baseline / org / name
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "run_pytest_results.json").write_text(json.dumps(_load_fixture(fixture)))


class TestBuildLabels:
    def test_all_15_repo_ids_present(self, tmp_path):
        dataset_repos = [
            {"full_name": "MemTensor/MemOS"},
            {"full_name": "fastapi/typer"},
        ]
        raw_dir = tmp_path / "_raw"
        for baseline in BASELINE_RUN_DIRS:
            _write_raw(raw_dir, baseline, "MemTensor", "MemOS", "all_pass.json")
        # fastapi/typer: present nowhere
        labels = build_labels(dataset_repos, raw_dir)
        assert set(labels.keys()) == {"MemTensor__MemOS", "fastapi__typer"}

    def test_present_false_when_baseline_produced_no_json(self, tmp_path):
        dataset_repos = [{"full_name": "org/repo"}]
        raw_dir = tmp_path / "_raw"
        _write_raw(raw_dir, "rat", "org", "repo", "all_pass.json")
        # repo2run, ccdf, radical: no results file written for this repo
        labels = build_labels(dataset_repos, raw_dir)
        entry = labels["org__repo"]
        assert entry["rat"]["present"] is True
        assert entry["repo2run"]["present"] is False
        assert entry["ccdf"]["present"] is False
        assert entry["radical"]["present"] is False

    def test_feasible_is_rat_honest_pass(self, tmp_path):
        dataset_repos = [{"full_name": "org/passer"}, {"full_name": "org/failer"}]
        raw_dir = tmp_path / "_raw"
        _write_raw(raw_dir, "rat", "org", "passer", "all_pass.json")
        _write_raw(raw_dir, "rat", "org", "failer", "below_tau.json")
        labels = build_labels(dataset_repos, raw_dir)
        assert labels["org__passer"]["feasible"] is True
        assert labels["org__failer"]["feasible"] is False

    def test_entry_has_all_four_baselines_and_feasible_keys(self, tmp_path):
        dataset_repos = [{"full_name": "org/repo"}]
        raw_dir = tmp_path / "_raw"
        labels = build_labels(dataset_repos, raw_dir)
        entry = labels["org__repo"]
        assert set(entry.keys()) == set(BASELINE_RUN_DIRS.keys()) | {"feasible"}
