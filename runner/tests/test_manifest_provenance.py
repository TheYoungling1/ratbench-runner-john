# runner/tests/test_manifest_provenance.py — the additive reproducibility fields (design items
# 1/2/5/6): sweagent_commit, copy_sweagent_config, host_platform, dataset_provenance.
#
# All four are pure/injectable and unit-tested with stubs — no real venv, git, or docker.
import hashlib
import json
import os
import subprocess

from runner import manifest


class _FakeRun:
    """Records every call and replays canned CompletedProcess results in order."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if self.results:
            return self.results.pop(0)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _cp(rc=0, stdout=""):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")


# ── sweagent_commit ─────────────────────────────────────────────────────────────────────────

def test_sweagent_commit_unknown_for_a_non_sweagent_arm():
    fake = _FakeRun()
    assert manifest.sweagent_commit("dockeragent", "/opt/sweagent_venv/bin/python", runner=fake) == "unknown"
    assert fake.calls == []          # never even tries — this arm never touches that venv


def test_sweagent_commit_resolves_via_the_installed_package_root():
    fake = _FakeRun(_cp(0, "/opt/sweagent_venv/lib/sweagent\n"), _cp(0, "abc123def\n"))
    got = manifest.sweagent_commit("sweagent_repo2run", "/opt/sweagent_venv/bin/python", runner=fake)
    assert got == "abc123def"
    assert fake.calls[0][0] == "/opt/sweagent_venv/bin/python"
    assert fake.calls[1] == ["git", "-C", "/opt/sweagent_venv/lib/sweagent", "rev-parse", "HEAD"]


def test_sweagent_commit_also_covers_the_native_sweagent_arm():
    fake = _FakeRun(_cp(0, "/pkg\n"), _cp(0, "deadbeef\n"))
    assert manifest.sweagent_commit("sweagent", "/opt/sweagent_venv/bin/python", runner=fake) == "deadbeef"


def test_sweagent_commit_unknown_when_the_venv_python_fails():
    fake = _FakeRun(_cp(1, ""))
    got = manifest.sweagent_commit("sweagent_repo2run", "/missing/python", runner=fake)
    assert got == "unknown"


def test_sweagent_commit_unknown_when_git_fails():
    fake = _FakeRun(_cp(0, "/pkg\n"), _cp(128, ""))
    got = manifest.sweagent_commit("sweagent_repo2run", "/opt/sweagent_venv/bin/python", runner=fake)
    assert got == "unknown"


def test_sweagent_commit_unknown_when_the_runner_raises():
    def boom(*a, **k):
        raise FileNotFoundError("no such interpreter")

    got = manifest.sweagent_commit("sweagent_repo2run", "/opt/sweagent_venv/bin/python", runner=boom)
    assert got == "unknown"


# ── copy_sweagent_config ─────────────────────────────────────────────────────────────────────

def test_copy_sweagent_config_writes_the_file_and_hashes_it(tmp_path):
    src = tmp_path / "src" / "sweagent_repo2run_config.yaml"
    src.parent.mkdir()
    src.write_text("agent:\n  model: {}\n")
    out_dir = tmp_path / "run"

    result = manifest.copy_sweagent_config(str(src), str(out_dir))
    dest = out_dir / result["path"]
    assert dest.is_file() and dest.read_text() == src.read_text()
    assert result["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()


def test_copy_sweagent_config_missing_source_degrades_to_none(tmp_path):
    result = manifest.copy_sweagent_config(str(tmp_path / "nope.yaml"), str(tmp_path / "run"))
    assert result == {"path": None, "sha256": None}


# ── host_platform ────────────────────────────────────────────────────────────────────────────

def test_host_platform_has_the_expected_keys():
    host = manifest.host_platform()
    assert set(host) == {"machine", "system", "python_version"}
    assert all(isinstance(v, str) and v for v in host.values())


# ── dataset_provenance ───────────────────────────────────────────────────────────────────────

def test_dataset_provenance_hashes_a_bare_list(tmp_path):
    p = tmp_path / "repos.json"
    p.write_text(json.dumps([{"full_name": "o/r1"}, {"full_name": "o/r2"}]))
    result = manifest.dataset_provenance(str(p))
    assert result["path"] == str(p)
    assert result["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert result["repo_count"] == 2


def test_dataset_provenance_hashes_the_wrapped_dict_form(tmp_path):
    p = tmp_path / "repos.json"
    p.write_text(json.dumps({"repos": [{"full_name": "o/r1"}]}))
    result = manifest.dataset_provenance(str(p))
    assert result["repo_count"] == 1


def test_dataset_provenance_missing_file_keeps_the_path_but_nulls_the_rest(tmp_path):
    p = tmp_path / "nope.json"
    result = manifest.dataset_provenance(str(p))
    assert result == {"path": str(p), "sha256": None, "repo_count": None}


def test_dataset_provenance_malformed_json_still_hashes_but_no_repo_count(tmp_path):
    p = tmp_path / "repos.json"
    p.write_text("{ not json")
    result = manifest.dataset_provenance(str(p))
    assert result["sha256"] is not None and result["repo_count"] is None


def test_dataset_provenance_empty_path_is_all_none():
    assert manifest.dataset_provenance("") == {"path": "", "sha256": None, "repo_count": None}


# ── manifest_fields_from_env: the new keys are additive, existing ones unchanged ──────────────

def test_manifest_fields_from_env_new_keys_default_to_none(monkeypatch):
    monkeypatch.delenv("RUN_VARIETY", raising=False)
    fields = manifest.manifest_fields_from_env(
        model="dockeragent", tier="smoke", num_turn=30, concurrency=None, status="running")
    assert fields["sweagent_commit"] is None
    assert fields["sweagent_config"] is None
    assert fields["host"] is None
    assert fields["dataset"] is None
    # existing keys unchanged
    assert fields["model"] == "dockeragent" and fields["status"] == "running"


def test_manifest_fields_from_env_carries_the_new_values_through():
    fields = manifest.manifest_fields_from_env(
        model="sweagent_repo2run", tier="smoke", num_turn=100, concurrency=4, status="running",
        sweagent_commit="abc123", sweagent_config={"path": "configs/x.yaml", "sha256": "deadbeef"},
        host={"machine": "arm64", "system": "Darwin", "python_version": "3.10.0"},
        dataset={"path": "/x.json", "sha256": "aaa", "repo_count": 50})
    assert fields["sweagent_commit"] == "abc123"
    assert fields["sweagent_config"]["sha256"] == "deadbeef"
    assert fields["host"]["machine"] == "arm64"
    assert fields["dataset"]["repo_count"] == 50


def test_write_manifest_roundtrips_the_new_fields(tmp_path):
    path = manifest.write_manifest(str(tmp_path), **manifest.manifest_fields_from_env(
        model="sweagent_repo2run", tier="smoke", num_turn=100, concurrency=None, status="running",
        sweagent_commit="abc123", sweagent_config={"path": "configs/x.yaml", "sha256": "deadbeef"},
        host=manifest.host_platform(), dataset={"path": "/x.json", "sha256": "aaa", "repo_count": 1}))
    on_disk = json.load(open(path))
    assert on_disk["sweagent_commit"] == "abc123"
    assert on_disk["dataset"]["repo_count"] == 1


def test_sweagent_commit_survives_the_import_banner_on_stdout():
    """`import sweagent` prints a banner to stdout, so stdout is never just the value. The first
    implementation captured ALL of stdout as the package path, fed banner-plus-path to `git -C`,
    and silently reported "unknown" for every real run while passing its own banner-free stubs."""
    SHA = "3ea751c087f32b16e039a2233dd6eefecef325d5"
    banner = ("\U0001f44b INFO     This is SWE-agent version 1.1.0\n"
              "            (hash='%s') with SWE-ReX version 1.4.0\n" % SHA)

    fake = _FakeRun(_cp(0, banner + "/opt/swe-agent/sweagent\n"), _cp(0, banner + SHA + "\n"))
    got = manifest.sweagent_commit("sweagent_repo2run", "/venv/bin/python", runner=fake)
    assert got == SHA
    # the path handed to git must be the path, not the banner
    assert fake.calls[1] == ["git", "-C", "/opt/swe-agent/sweagent", "rev-parse", "HEAD"]


# ── dirty was hardcoded False; every run claimed a clean tree ────────────────────────────────
# harness_commit only means something if the reader knows whether the tree matched it. A constant
# False is worse than no field: it asserts reproducibility that was never checked.

def test_dirty_is_measured_not_asserted(monkeypatch):
    from runner import manifest

    calls = {}

    def fake(cmd, **kw):
        calls["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=" M runner/live/claudecode.py\n", stderr="")

    assert manifest.worktree_dirty("/repo", runner=fake) is True
    assert "--porcelain" in calls["cmd"] and "--untracked-files=no" in calls["cmd"]


def test_untracked_files_are_not_dirt():
    # runs/, scratch datasets and editor droppings live in the tree constantly; counting them
    # would pin every run to dirty=True, which is the same lie inverted.
    clean = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    from runner import manifest
    assert manifest.worktree_dirty("/repo", runner=clean) is False


def test_unreadable_git_state_is_dirty_not_clean():
    from runner import manifest

    def boom(cmd, **kw):
        raise OSError("no git")

    assert manifest.worktree_dirty("/repo", runner=boom) is True
    fail = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 128, stdout="", stderr="not a repo")
    assert manifest.worktree_dirty("/repo", runner=fail) is True
