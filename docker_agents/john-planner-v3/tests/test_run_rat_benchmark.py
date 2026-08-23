"""
Offline tests for run_rat_benchmark.py.

Techniques used:
- monkeypatch DockerAgentModel.predict to a fake that writes per-repo JSONs
- fake child scripts via monkeypatching subprocess.Popen in _run_child
- tmpdir fixtures for full isolation
- no Docker, no LLM, no network
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# ── stub out external RAT-repo imports BEFORE importing run_rat_benchmark ─────
_RAT_STUBS = [
    "eval",
    "eval.common",
    "eval.common.scorers",
    "eval.models",
    "eval.models.dockeragent_model",
    "eval.models.rat_model",
    "eval.models.repo2run_model",
    "dotenv",
]
for _mod in _RAT_STUBS:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_scorers_mock = sys.modules["eval.common.scorers"]
_scorers_mock.success_scorer = MagicMock(return_value={"success": True})
_scorers_mock.pytest_pass_rate_scorer = MagicMock(
    return_value={"pytest_pass_rate": 0.8, "pass_rate_exclude_code_issues": 0.8}
)
_scorers_mock.pytest_collect_scorer = MagicMock(return_value={"pytest_collect_success": True})

sys.modules["eval.models.dockeragent_model"].DockerAgentModel = MagicMock()
sys.modules["dotenv"].load_dotenv = MagicMock()

# ── point env vars BEFORE any import of run_rat_benchmark ────────────────────
os.environ["RAT_ROOT"] = "/tmp/runanything/src"
os.environ["DOCKERAGENT_ROOT"] = "/Users/john/rat-bench-integration"

# Insert repo root at the front so we import the LOCAL run_rat_benchmark.py,
# not the copy in rat-bench-integration (which has its own unrelated edits).
_REPO_ROOT = str(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)

# Clear any cached version so the stubs take effect.
sys.modules.pop("run_rat_benchmark", None)

import run_rat_benchmark as rrb  # noqa: E402  (must come after env setup)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures & helpers
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_REPOS = [
    {"full_name": "org/alpha", "_tier": "smoke", "_category": "cat_a"},
    {"full_name": "org/beta",  "_tier": "smoke", "_category": "cat_b"},
    {"full_name": "org/gamma", "_tier": "extended", "_category": "cat_a"},
    {"full_name": "org/delta", "_tier": "extended", "_category": "cat_b"},
    {"full_name": "org/eps",   "_tier": "smoke", "_category": "cat_a"},
]


@pytest.fixture()
def repos_json_dict(tmp_path):
    """Write a {"repos":[...]} dict JSON and return its path."""
    p = tmp_path / "repos_dict.json"
    p.write_text(json.dumps({"repos": SAMPLE_REPOS}))
    return str(p)


@pytest.fixture()
def repos_json_list(tmp_path):
    """Write a bare-list JSON and return its path."""
    p = tmp_path / "repos_list.json"
    p.write_text(json.dumps(SAMPLE_REPOS))
    return str(p)


@pytest.fixture()
def root_path(tmp_path):
    """Return a fresh root_path for each test."""
    rp = tmp_path / "rat_run"
    rp.mkdir()
    return str(rp)


def _fake_predict(full_name: str, root_path: str, *, success: bool = True) -> dict:
    """Simulate what a real predict() + RAT pytest tools produce on disk."""
    out_dir = Path(root_path) / "output" / full_name
    out_dir.mkdir(parents=True, exist_ok=True)

    if success:
        pytest_results = {
            "summary": {
                "total_tests": 10,
                "passed": 8,
                "failed": 1,
                "errors": 1,
                "skipped": 0,
            },
            "error_breakdown": {},
        }
        collect_results = {"success": True, "collected": 10}
        (out_dir / "run_pytest_results.json").write_text(json.dumps(pytest_results))
        (out_dir / "run_pytest_collect_results.json").write_text(json.dumps(collect_results))
        return {"status": "success", "root_path": root_path, "full_name": full_name}
    else:
        return {
            "status": "error",
            "failure_reason": "repo_error",
            "root_path": root_path,
            "full_name": full_name,
        }


def _make_fake_model(root_path: str, success: bool = True):
    """Return a mock DockerAgentModel whose predict() writes the right disk artifacts."""

    class FakeModel:
        def __init__(self, *args, **kwargs):
            self._root_path = root_path

        def predict(self, full_name: str) -> dict:
            return _fake_predict(full_name, self._root_path, success=success)

    return FakeModel()


# ─────────────────────────────────────────────────────────────────────────────
# 1. load_repos: dict format
# ─────────────────────────────────────────────────────────────────────────────

def test_load_repos_dict_format(repos_json_dict):
    repos = rrb.load_repos(repos_json_dict)
    assert isinstance(repos, list)
    assert len(repos) == len(SAMPLE_REPOS)
    assert repos[0]["full_name"] == "org/alpha"


# ─────────────────────────────────────────────────────────────────────────────
# 2. load_repos: bare list format
# ─────────────────────────────────────────────────────────────────────────────

def test_load_repos_bare_list_format(repos_json_list):
    repos = rrb.load_repos(repos_json_list)
    assert isinstance(repos, list)
    assert len(repos) == len(SAMPLE_REPOS)
    assert repos[1]["full_name"] == "org/beta"


# ─────────────────────────────────────────────────────────────────────────────
# 3. selection: tier filter
# ─────────────────────────────────────────────────────────────────────────────

def test_select_repos_tier_filter(repos_json_dict):
    repos = rrb._select_repos(repos_json_dict, tier="smoke", category=None, offset=0, limit=None)
    assert all(r["_tier"] == "smoke" for r in repos)
    assert len(repos) == 3  # alpha, beta, eps


# ─────────────────────────────────────────────────────────────────────────────
# 4. selection: category filter
# ─────────────────────────────────────────────────────────────────────────────

def test_select_repos_category_filter(repos_json_dict):
    repos = rrb._select_repos(repos_json_dict, tier="all", category="cat_a", offset=0, limit=None)
    assert all(r["_category"] == "cat_a" for r in repos)
    assert {r["full_name"] for r in repos} == {"org/alpha", "org/gamma", "org/eps"}


# ─────────────────────────────────────────────────────────────────────────────
# 5. selection: offset+limit slice ordering
# ─────────────────────────────────────────────────────────────────────────────

def test_select_repos_offset_limit(repos_json_dict):
    repos = rrb._select_repos(repos_json_dict, tier="all", category=None, offset=1, limit=2)
    assert len(repos) == 2
    # Must preserve original list order: index 1 and 2 from SAMPLE_REPOS
    assert repos[0]["full_name"] == SAMPLE_REPOS[1]["full_name"]
    assert repos[1]["full_name"] == SAMPLE_REPOS[2]["full_name"]


# ─────────────────────────────────────────────────────────────────────────────
# 6. selection: empty selection returns n==0 cleanly
# ─────────────────────────────────────────────────────────────────────────────

def test_select_repos_empty_selection(repos_json_dict):
    repos = rrb._select_repos(repos_json_dict, tier="smoke", category="nonexistent_cat",
                               offset=0, limit=None)
    assert repos == []


# ─────────────────────────────────────────────────────────────────────────────
# 7. _run_one: writes _result_row.json + _meta.json, NOT rat_results.json
# ─────────────────────────────────────────────────────────────────────────────

def test_run_one_writes_per_repo_files_not_shared(root_path, monkeypatch):
    full_name = "org/alpha"
    model = _make_fake_model(root_path, success=True)

    row = rrb._run_one(full_name, model, root_path, "cat_a")

    out_dir = Path(root_path) / "output" / full_name
    assert (out_dir / "_result_row.json").exists(), "_result_row.json must exist"
    assert (out_dir / "_meta.json").exists(), "_meta.json must exist"
    # rat_results.json must NOT be written by _run_one
    assert not (Path(root_path) / "rat_results.json").exists(), (
        "_run_one must NOT write rat_results.json"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 8. scorer keys present in the row
# ─────────────────────────────────────────────────────────────────────────────

def test_run_one_scorer_keys_in_row(root_path):
    full_name = "org/alpha"
    model = _make_fake_model(root_path, success=True)

    row = rrb._run_one(full_name, model, root_path, "cat_a")

    for key in ("success", "pytest_collect_success", "pytest_pass_rate",
                "pass_rate_exclude_code_issues"):
        assert key in row, f"Expected scorer key '{key}' in result row"


# ─────────────────────────────────────────────────────────────────────────────
# 9. resume-skip: pre-existing run_pytest_results.json skips the repo
# ─────────────────────────────────────────────────────────────────────────────

def test_run_one_resume_skip(root_path):
    full_name = "org/alpha"
    out_dir = Path(root_path) / "output" / full_name
    out_dir.mkdir(parents=True, exist_ok=True)

    # Pre-write the done marker AND a result row
    _fake_predict(full_name, root_path, success=True)
    row_data = {"status": "success", "root_path": root_path, "full_name": full_name,
                "success": True, "pytest_collect_success": True,
                "pytest_pass_rate": 0.8, "pass_rate_exclude_code_issues": 0.8,
                "_category": "cat_a"}
    (out_dir / "_result_row.json").write_text(json.dumps(row_data))

    call_count = 0

    class CountingModel:
        def predict(self, full_name: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"status": "success", "root_path": root_path, "full_name": full_name}

    rrb._run_one(full_name, CountingModel(), root_path, "cat_a")
    assert call_count == 0, "predict() must NOT be called when run_pytest_results.json exists"


# ─────────────────────────────────────────────────────────────────────────────
# 10. scheduler: never exceeds N concurrent children
# ─────────────────────────────────────────────────────────────────────────────

def test_scheduler_concurrency_limit(tmp_path, repos_json_list):
    """
    Verify the scheduler never has more than `concurrency` children in flight.
    We do this by replacing subprocess.Popen with a fake that measures peak
    in-flight count (holding a slot open briefly).
    """
    root_path = str(tmp_path / "rat_run")
    os.makedirs(root_path, exist_ok=True)
    concurrency = 2

    # Build a small repo list
    repos = [
        {"full_name": f"org/repo{i}", "_category": "cat_a"}
        for i in range(5)
    ]

    peak_concurrent = [0]
    current_concurrent = [0]

    class FakeProc:
        def __init__(self, full_name):
            self.full_name = full_name
            self.returncode = 0
            self.pid = os.getpid()

        def wait(self, timeout=None):
            # Measure peak; simulate brief work
            current_concurrent[0] += 1
            peak_concurrent[0] = max(peak_concurrent[0], current_concurrent[0])
            time.sleep(0.02)
            current_concurrent[0] -= 1
            # Write a fake _result_row.json so _run_child doesn't synthesize an error row
            out_dir = Path(root_path) / "output" / self.full_name
            out_dir.mkdir(parents=True, exist_ok=True)
            row = {
                "status": "success",
                "full_name": self.full_name,
                "root_path": root_path,
                "_category": "cat_a",
                "success": True,
                "pytest_collect_success": False,
                "pytest_pass_rate": 0.0,
                "pass_rate_exclude_code_issues": 0.0,
            }
            (out_dir / "_result_row.json").write_text(json.dumps(row))
            (out_dir / "run.log").write_bytes(b"")

    original_popen = subprocess.Popen

    def fake_popen(cmd, stdout=None, stderr=None, start_new_session=False, **kwargs):
        # Extract full_name from --only argument
        try:
            idx = cmd.index("--only")
            full_name = cmd[idx + 1]
        except (ValueError, IndexError):
            full_name = "org/unknown"
        proc = FakeProc(full_name)
        return proc

    with patch("subprocess.Popen", side_effect=fake_popen):
        # Also patch os.killpg / os.getpgid in case scheduler tries to kill
        with patch("os.killpg", return_value=None):
            rrb.scheduler(
                repos=repos,
                root_path=root_path,
                llm="fake-llm",
                timeout=30,
                num_turn=1,
                repos_json=repos_json_list,
                concurrency=concurrency,
            )

    assert peak_concurrent[0] <= concurrency, (
        f"Peak concurrent={peak_concurrent[0]} exceeded concurrency={concurrency}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 10b. scheduler: a wave slower than the poll window must NOT crash the scheduler
# ─────────────────────────────────────────────────────────────────────────────

def test_scheduler_survives_wave_slower_than_poll_window(tmp_path, repos_json_list):
    """Regression: when no future completes within the poll window, the scheduler
    must keep waiting — not raise. The old code used
    ``list(as_completed(futures_map, timeout=30))``, which raises
    ``concurrent.futures.TimeoutError`` once the window elapses with futures still
    unfinished. In production that killed the K=8 run at 8/50 the moment the first
    slow build wave failed to finish inside 30s. Here we shrink the window to 0.05s
    and make every child take 0.2s, so every window elapses with work in flight.
    """
    root_path = str(tmp_path / "rat_run")
    os.makedirs(root_path, exist_ok=True)
    repos = [{"full_name": f"org/slow{i}", "_category": "cat_a"} for i in range(4)]

    class SlowProc:
        def __init__(self, full_name):
            self.full_name = full_name
            self.returncode = 0
            self.pid = os.getpid()

        def wait(self, timeout=None):
            time.sleep(0.2)  # longer than the 0.05s poll window
            out_dir = Path(root_path) / "output" / self.full_name
            out_dir.mkdir(parents=True, exist_ok=True)
            row = {
                "status": "success",
                "full_name": self.full_name,
                "root_path": root_path,
                "_category": "cat_a",
                "success": True,
                "pytest_collect_success": False,
                "pytest_pass_rate": 0.0,
                "pass_rate_exclude_code_issues": 0.0,
            }
            (out_dir / "_result_row.json").write_text(json.dumps(row))
            (out_dir / "run.log").write_bytes(b"")

    def fake_popen(cmd, **kwargs):
        idx = cmd.index("--only")
        return SlowProc(cmd[idx + 1])

    with patch("subprocess.Popen", side_effect=fake_popen):
        with patch("os.killpg", return_value=None):
            rrb.scheduler(
                repos=repos,
                root_path=root_path,
                llm="fake-llm",
                timeout=30,
                num_turn=1,
                repos_json=repos_json_list,
                concurrency=2,
                poll_interval=0.05,
            )

    for i in range(4):
        assert (
            Path(root_path) / "output" / f"org/slow{i}" / "_result_row.json"
        ).exists(), f"org/slow{i} was never processed — scheduler aborted early"


# ─────────────────────────────────────────────────────────────────────────────
# 11. scheduler: child exit != 0 / missing row -> status:error row
# ─────────────────────────────────────────────────────────────────────────────

def test_scheduler_child_nonzero_exit_yields_error_row(tmp_path, repos_json_list):
    """Child exits non-zero and writes no _result_row.json → synthesized error row."""
    root_path = str(tmp_path / "rat_run")
    os.makedirs(root_path, exist_ok=True)

    repos = [{"full_name": "org/failing", "_category": "cat_x"}]

    class BadProc:
        returncode = 1
        pid = os.getpid()

        def wait(self, timeout=None):
            # Write a log file but NO _result_row.json
            out_dir = Path(root_path) / "output" / "org/failing"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "run.log").write_bytes(b"child crashed")

    with patch("subprocess.Popen", return_value=BadProc()):
        with patch("os.killpg", return_value=None):
            rrb.scheduler(
                repos=repos,
                root_path=root_path,
                llm="fake-llm",
                timeout=30,
                num_turn=1,
                repos_json=repos_json_list,
                concurrency=1,
            )

    row_path = Path(root_path) / "output" / "org/failing" / "_result_row.json"
    assert row_path.exists(), "Synthesized error row must be written"
    row = json.loads(row_path.read_text())
    assert row["status"] == "error"


# ─────────────────────────────────────────────────────────────────────────────
# 12. scheduler: child exceeds wall-clock -> killed, status:timeout + failure_reason harness_timeout
# ─────────────────────────────────────────────────────────────────────────────

def test_scheduler_child_timeout_yields_timeout_row(tmp_path, repos_json_list):
    """Child hangs past hard_wall → killed, synthesized timeout row."""
    root_path = str(tmp_path / "rat_run")
    os.makedirs(root_path, exist_ok=True)

    repos = [{"full_name": "org/slow", "_category": "cat_y"}]

    class HangingProc:
        pid = os.getpid()
        returncode = -9

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
            # second wait() after kill
            return

    with patch("subprocess.Popen", return_value=HangingProc()):
        with patch("os.killpg", return_value=None):
            with patch("os.getpgid", return_value=os.getpid()):
                rrb.scheduler(
                    repos=repos,
                    root_path=root_path,
                    llm="fake-llm",
                    timeout=5,
                    num_turn=1,
                    repos_json=repos_json_list,
                    concurrency=1,
                )

    row_path = Path(root_path) / "output" / "org/slow" / "_result_row.json"
    assert row_path.exists(), "Synthesized timeout row must be written"
    row = json.loads(row_path.read_text())
    assert row["status"] == "timeout"
    assert row.get("failure_reason") == "harness_timeout"


# ─────────────────────────────────────────────────────────────────────────────
# 13. parallel children do NOT clobber rat_results.json (regression test for CQ-1)
# ─────────────────────────────────────────────────────────────────────────────

def test_parallel_children_do_not_write_rat_results_json(tmp_path, repos_json_list):
    """
    _run_one (the child code path) must never write rat_results.json.
    Simulate two concurrent _run_one calls and confirm rat_results.json is absent.
    """
    root_path = str(tmp_path / "rat_run")
    os.makedirs(root_path, exist_ok=True)

    repos = [
        {"full_name": "org/p1", "_category": "cat_a"},
        {"full_name": "org/p2", "_category": "cat_b"},
    ]

    for r in repos:
        model = _make_fake_model(root_path, success=True)
        rrb._run_one(r["full_name"], model, root_path, r["_category"])

    # rat_results.json must NOT exist at this point
    assert not (Path(root_path) / "rat_results.json").exists(), (
        "CQ-1 regression: _run_one must never write rat_results.json"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 14. aggregate-from-rows == sequential aggregation (same rat_results.json shape)
# ─────────────────────────────────────────────────────────────────────────────

def test_aggregate_produces_correct_rat_results_json(root_path):
    """aggregate() should glob all _result_row.json files and write rat_results.json."""
    repos = [
        {"full_name": "org/a", "_category": "cat_a"},
        {"full_name": "org/b", "_category": "cat_b"},
    ]

    # Pre-write result rows as _run_one would
    for r in repos:
        model = _make_fake_model(root_path, success=True)
        rrb._run_one(r["full_name"], model, root_path, r["_category"])

    rows = rrb.aggregate(root_path)

    rat_results_path = Path(root_path) / "rat_results.json"
    assert rat_results_path.exists(), "aggregate() must write rat_results.json"

    written = json.loads(rat_results_path.read_text())
    # rat_results.json is now {"runner_commit": <str>, "rows": [...]}
    assert isinstance(written, dict), "rat_results.json must be a dict with runner_commit + rows"
    assert "runner_commit" in written, "rat_results.json must contain 'runner_commit'"
    assert isinstance(written["runner_commit"], str)
    rows_written = written["rows"]
    assert isinstance(rows_written, list)
    assert len(rows_written) == 2
    # All four scorer keys must be present in every row
    for row in rows_written:
        for key in ("success", "pytest_collect_success", "pytest_pass_rate",
                    "pass_rate_exclude_code_issues"):
            assert key in row, f"Scorer key '{key}' missing from aggregated row"


# ─────────────────────────────────────────────────────────────────────────────
# 15. worker_main / --only mode: writes _result_row.json + _meta.json, NOT rat_results.json
# ─────────────────────────────────────────────────────────────────────────────

def test_worker_main_only_mode(tmp_path, repos_json_dict, monkeypatch):
    """
    worker_main() is the --only code path.  It must write per-repo files and
    must NOT write rat_results.json.
    """
    root_path = str(tmp_path / "rat_run")
    full_name = "org/alpha"

    def fake_docker_model_cls(root_path, timeout, llm, num_turn):
        return _make_fake_model(root_path, success=True)

    monkeypatch.setattr(rrb, "DockerAgentModel", fake_docker_model_cls)

    rrb.worker_main(
        full_name=full_name,
        root_path=root_path,
        llm="fake-llm",
        timeout=30,
        num_turn=1,
        repos_json=repos_json_dict,
    )

    out_dir = Path(root_path) / "output" / full_name
    assert (out_dir / "_result_row.json").exists(), "_result_row.json must exist after worker_main"
    assert (out_dir / "_meta.json").exists(), "_meta.json must exist after worker_main"
    assert not (Path(root_path) / "rat_results.json").exists(), (
        "worker_main (--only) must NOT write rat_results.json"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 16. _make_model returns the right class for each model name
# ─────────────────────────────────────────────────────────────────────────────

def test_make_model_dockeragent(monkeypatch, tmp_path):
    """_make_model('dockeragent', ...) returns a DockerAgentModel instance."""
    root_path = str(tmp_path / "rat_run")

    # Monkeypatch DockerAgentModel at the rrb module level (same target tests already use)
    instances = []

    def fake_docker_cls(**kwargs):
        obj = MagicMock()
        instances.append(("dockeragent", kwargs))
        return obj

    monkeypatch.setattr(rrb, "DockerAgentModel", fake_docker_cls)

    rrb._make_model("dockeragent", root_path, timeout=60, llm="test-llm", num_turn=5)

    assert len(instances) == 1
    assert instances[0][0] == "dockeragent"
    kw = instances[0][1]
    assert kw["root_path"] == root_path
    assert kw["timeout"] == 60
    assert kw["llm"] == "test-llm"
    assert kw["num_turn"] == 5


def test_make_model_rat(monkeypatch, tmp_path):
    """_make_model('rat', ...) lazy-imports and returns a RATModel instance."""
    root_path = str(tmp_path / "rat_run")

    fake_rat_instance = MagicMock()
    fake_rat_instance.predict = MagicMock(return_value={"status": "success"})

    captured = []

    class FakeRATModel:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def predict(self, full_name):
            return {"status": "success"}

    # Patch the lazy import inside _make_model by injecting into sys.modules
    import types
    fake_module = types.ModuleType("eval.models.rat_model")
    fake_module.RATModel = FakeRATModel
    monkeypatch.setitem(sys.modules, "eval.models.rat_model", fake_module)

    model = rrb._make_model("rat", root_path, timeout=120, llm="rat-llm", num_turn=10)

    assert len(captured) == 1
    assert captured[0]["root_path"] == root_path
    assert captured[0]["timeout"] == 120
    assert captured[0]["llm"] == "rat-llm"
    assert captured[0]["num_turn"] == 10
    assert captured[0]["save_mode"] == "none"
    assert isinstance(model, FakeRATModel)


def test_make_model_repo2run(monkeypatch, tmp_path):
    """_make_model('repo2run', ...) lazy-imports and returns a Repo2RunModel instance."""
    root_path = str(tmp_path / "rat_run")

    captured = []

    class FakeRepo2RunModel:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def predict(self, full_name):
            return {"status": "success"}

    import types
    fake_module = types.ModuleType("eval.models.repo2run_model")
    fake_module.Repo2RunModel = FakeRepo2RunModel
    monkeypatch.setitem(sys.modules, "eval.models.repo2run_model", fake_module)

    model = rrb._make_model("repo2run", root_path, timeout=300, llm="r2r-llm", num_turn=20)

    assert len(captured) == 1
    assert captured[0]["root_path"] == root_path
    assert captured[0]["timeout"] == 300
    assert captured[0]["llm"] == "r2r-llm"
    assert captured[0]["num_turn"] == 20
    assert isinstance(model, FakeRepo2RunModel)


# ─────────────────────────────────────────────────────────────────────────────
# 17. --model is forwarded into _child_cmd
# ─────────────────────────────────────────────────────────────────────────────

def test_child_cmd_includes_model_flag():
    """_child_cmd must append --model <value> so worker children use the same model."""
    for model_name in ("dockeragent", "rat", "repo2run"):
        cmd = rrb._child_cmd(
            full_name="org/testrepo",
            root_path="/tmp/test_root",
            llm="test-llm",
            timeout=60,
            num_turn=3,
            repos_json="/tmp/repos.json",
            model=model_name,
        )
        assert "--model" in cmd, f"--model not found in _child_cmd output for {model_name}"
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == model_name, (
            f"Expected model value {model_name!r}, got {cmd[model_idx + 1]!r}"
        )


def test_child_cmd_propagates_arm_v1(monkeypatch):
    """Worker children must carry --arm v1 when the scheduler resolved v1.

    Regression: the scheduler resolves --arm into DOCKERAGENT_ENABLE_V1 before
    fanning out, but children re-parse args (arm defaults to arm0) and RESET the
    env var to "0" — silently downgrading every worker to legacy ReAct. _child_cmd
    must forward the arm so the child keeps DOCKERAGENT_ENABLE_V1=1.
    """
    monkeypatch.setenv("DOCKERAGENT_ENABLE_V1", "1")
    # GRAPH_SCHEDULER must be absent/0 so re-derivation resolves to "v1" not "v3".
    monkeypatch.delenv("DOCKERAGENT_ENABLE_GRAPH_SCHEDULER", raising=False)
    cmd = rrb._child_cmd(
        full_name="org/testrepo", root_path="/tmp/test_root", llm="test-llm",
        timeout=60, num_turn=3, repos_json="/tmp/repos.json",
    )
    assert "--arm" in cmd, "_child_cmd must forward --arm to the worker"
    assert cmd[cmd.index("--arm") + 1] == "v1"


def test_child_cmd_arm0_when_v1_disabled(monkeypatch):
    """With v1 disabled in the environment, the worker arm is arm0."""
    monkeypatch.setenv("DOCKERAGENT_ENABLE_V1", "0")
    # GRAPH_SCHEDULER must be absent/0 so re-derivation resolves to "arm0".
    monkeypatch.delenv("DOCKERAGENT_ENABLE_GRAPH_SCHEDULER", raising=False)
    cmd = rrb._child_cmd(
        full_name="org/testrepo", root_path="/tmp/test_root", llm="test-llm",
        timeout=60, num_turn=3, repos_json="/tmp/repos.json",
    )
    assert cmd[cmd.index("--arm") + 1] == "arm0"


def test_child_cmd_v3_when_full_stack(monkeypatch):
    for v in ("V1", "DEP_GRAPH", "DEP_EMIT", "RUNTIME_FEEDBACK",
              "GRAPH_SCHEDULER", "RUNTIME_PIN", "SERVICE_PROVISION"):
        monkeypatch.setenv(f"DOCKERAGENT_ENABLE_{v}", "1")
    cmd = rrb._child_cmd(
        full_name="o/r", root_path="/tmp/x", llm="deepseek-chat",
        timeout=10, num_turn=5, repos_json="d.json", model="dockeragent",
    )
    assert cmd[cmd.index("--arm") + 1] == "v3"


def test_child_cmd_v1_when_only_v1(monkeypatch):
    monkeypatch.setenv("DOCKERAGENT_ENABLE_V1", "1")
    for v in ("DEP_GRAPH", "DEP_EMIT", "RUNTIME_FEEDBACK",
              "GRAPH_SCHEDULER", "RUNTIME_PIN", "SERVICE_PROVISION"):
        monkeypatch.setenv(f"DOCKERAGENT_ENABLE_{v}", "0")
    cmd = rrb._child_cmd(
        full_name="o/r", root_path="/tmp/x", llm="deepseek-chat",
        timeout=10, num_turn=5, repos_json="d.json", model="dockeragent",
    )
    assert cmd[cmd.index("--arm") + 1] == "v1"


# ─────────────────────────────────────────────────────────────────────────────
# 18. Portable defaults: no hardcoded machine-specific paths
# ─────────────────────────────────────────────────────────────────────────────

def test_this_dir_points_at_the_repo():
    """_THIS_DIR must resolve to the directory actually holding run_rat_benchmark.py,
    so DOCKERAGENT_ROOT / --repos-json defaults follow a fresh clone anywhere."""
    assert os.path.isfile(os.path.join(rrb._THIS_DIR, "run_rat_benchmark.py"))


def test_default_rat_root_prefers_repo_local(monkeypatch):
    """When <repo>/runanything/src exists, it wins over every other candidate."""
    repo = os.path.join("some", "repo")
    repo_local = os.path.join(repo, "runanything", "src")
    monkeypatch.setattr(os.path, "isdir", lambda p: p == repo_local)
    assert rrb._default_rat_root(repo) == repo_local


def test_default_rat_root_prefers_sibling_when_no_repo_local(monkeypatch):
    """A sibling 'runanything/src' next to the repo is the second choice."""
    repo = os.path.join("some", "repo")
    sibling = os.path.join("some", "runanything", "src")
    monkeypatch.setattr(os.path, "isdir", lambda p: p == sibling)
    assert rrb._default_rat_root(repo) == sibling


def test_default_rat_root_uses_legacy_tmp_when_only_tmp_exists(monkeypatch):
    """The legacy /tmp/runanything/src is honoured when it is the only one present."""
    repo = os.path.join("some", "repo")
    monkeypatch.setattr(os.path, "isdir", lambda p: p == "/tmp/runanything/src")
    assert rrb._default_rat_root(repo) == "/tmp/runanything/src"


def test_default_rat_root_falls_back_to_repo_local_when_none_exist(monkeypatch):
    """With nothing on disk, fall back to the repo-local path (a clear place to unzip RAT)."""
    repo = os.path.join("some", "repo")
    monkeypatch.setattr(os.path, "isdir", lambda p: False)
    assert rrb._default_rat_root(repo) == os.path.join(repo, "runanything", "src")
