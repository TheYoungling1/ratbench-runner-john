# tests/bench/test_pytest_config_restore.py
"""EBSR/ESSR must measure the repository's OWN pytest suite: collection honours the repo's config
(no explicit path, so `testpaths` applies), and a system cannot narrow that suite by editing or
adding pytest config — the pristine files at the pinned SHA are restored before collection and
again before the full run."""
import subprocess

from bench.languages.python import PythonLanguage
from bench.measure import measure
from bench.schema import HarvestedEnv, RepoSpec

W = "/testbed"
SHA = "a" * 40
_JUNIT = '<testsuites><testsuite tests="1"><testcase classname="t" name="a"/></testsuite></testsuites>'


def test_collect_commands_pass_no_explicit_path():
    # An explicit positional path makes pytest ignore `testpaths` and collect the whole tree.
    lang = PythonLanguage()
    for cmd in (lang.gate_cmd(W), lang.collect_cmd(W), lang.run_cmd(W, f"{W}/logs/junit.xml")):
        assert "python -m pytest " in cmd                  # still bypasses a `pytest` wrapper exe
        assert f"{W};" not in cmd and f"{W} " not in cmd and not cmd.endswith(W)


class _Docker:
    def __init__(self, restore_resp=(0, "REF=pinned", False)):
        self.calls, self.restore_resp = [], restore_resp

    def build(self, tag, ctx, timeout=None):
        return 0, "log"

    def image_size_mb(self, tag):
        return 1.0

    def run_detached(self, tag, name, workdir):
        pass

    def exec(self, name, argv, timeout=None):
        cmd = argv[-1]
        self.calls.append(cmd)
        if "py_test_files" in cmd:
            return 0, "STATUS=conforming py_test_files=1", False
        if "cat-file -e" in cmd:                             # the config restore script
            return self.restore_resp
        if "junit.xml" in cmd and "cat " in cmd:
            return 0, _JUNIT, False
        return 0, "", False

    def rm(self, name, tag):
        pass


def _env(meta=None):
    return HarvestedEnv("a", RepoSpec("o/r", "https://github.com/o/r"), "FROM x", base_image=None,
                        status="ok", meta=meta if meta is not None else {"head_sha": SHA})


def _idx(calls, needle):
    return [i for i, c in enumerate(calls) if needle in c]


def test_config_restored_before_collection_and_before_run():
    d = _Docker()
    measure(_env(), docker=d)
    restores = _idx(d.calls, "cat-file -e")
    gate, coll = _idx(d.calls, "--disable-warnings")[0], _idx(d.calls, "--co ")[0]
    run = _idx(d.calls, "--junit-xml")[0]
    assert len(restores) == 2
    assert restores[0] < gate < coll < restores[1] < run
    assert all(SHA in d.calls[i] for i in restores)            # restored to the PINNED commit


def test_restore_outcome_is_recorded_and_failure_surfaced():
    row = measure(_env(), docker=_Docker(restore_resp=(1, "FAILED restore pytest.ini", False)))
    rec = row.meta["pytest_config_restore"]
    assert [r["stage"] for r in rec] == ["collect"]        # fails closed: no run stage follows
    assert all(r["rc"] == 1 and "FAILED restore pytest.ini" in r["log"] for r in rec)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _sandbox(tmp_path):
    tb = tmp_path / "testbed"
    (tb / "tests").mkdir(parents=True)
    (tb / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (tb / "tests" / "conftest.py").write_text("# pristine\n")
    (tb / "sub").mkdir()
    (tb / "sub" / "pytest.ini").write_text("[pytest]\n")      # nested: pytest never reads it
    _git(tb, "init", "-q")
    _git(tb, "-c", "user.email=x@y", "-c", "user.name=x", "add", "-A")
    _git(tb, "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-qm", "pin")
    sha = subprocess.run(["git", "-C", str(tb), "rev-parse", "HEAD"], capture_output=True,
                         text=True, check=True).stdout.strip()
    return tb, sha


def _run(tb, sha):
    cmd = PythonLanguage().config_restore_cmd(str(tb), sha)
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)


def test_restore_script_neutralises_agent_config(tmp_path):
    tb, sha = _sandbox(tmp_path)
    # The agent narrows the suite in every way the evaluator must undo:
    (tb / "pytest.ini").write_text("[pytest]\ntestpaths = tests/unit\n")      # tracked, edited
    _git(tb, "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-qam", "agent narrows")
    (tb / "tests" / "conftest.py").unlink()                                   # tracked, deleted
    (tb / "tox.ini").write_text("[pytest]\naddopts = -k nothing\n")           # untracked, root
    (tb / "tests" / "unit").mkdir()
    (tb / "tests" / "unit" / "conftest.py").write_text("collect_ignore = ['x']\n")  # untracked, nested
    (tmp_path / "pytest.ini").write_text("[pytest]\n")                        # untracked, ancestor
    (tb / "sub" / "pytest.ini").write_text("[pytest]\nchanged\n")             # nested ini: irrelevant

    p = _run(tb, sha)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "REF=" + sha in p.stdout
    assert (tb / "pytest.ini").read_text() == "[pytest]\ntestpaths = tests\n"
    assert (tb / "tests" / "conftest.py").read_text() == "# pristine\n"
    assert not (tb / "tox.ini").exists()
    assert not (tb / "tests" / "unit" / "conftest.py").exists()
    assert not (tmp_path / "pytest.ini").exists()
    assert (tb / "sub" / "pytest.ini").read_text() == "[pytest]\nchanged\n"


def test_restore_falls_back_to_head_and_says_so(tmp_path):
    tb, _ = _sandbox(tmp_path)
    (tb / "pytest.ini").write_text("[pytest]\ntestpaths = tests/unit\n")
    p = _run(tb, "b" * 40)                                  # pinned sha not in the object store
    assert p.returncode == 0
    assert "REF=HEAD (pinned_sha_absent)" in p.stdout
    assert (tb / "pytest.ini").read_text() == "[pytest]\ntestpaths = tests\n"


def test_restore_fails_loudly_without_git(tmp_path):
    tb = tmp_path / "testbed"
    tb.mkdir()
    p = _run(tb, "")
    assert p.returncode != 0


def test_restore_failure_disqualifies_row_before_collection():
    # A restore that failed means the suite measured may be the system's, not the repo's: fail closed.
    d = _Docker(restore_resp=(1, "FAILED restore pytest.ini", False))
    row = measure(_env(), docker=d)
    assert row.status == "config_restore_failed" and row.ebsr is False and row.executed is False
    assert not _idx(d.calls, "--disable-warnings") and not _idx(d.calls, "--junit-xml")


def test_config_restore_failed_is_disqualified_in_metrics():
    from bench.metrics import _conforming
    from bench.schema import MeasureRow
    assert not _conforming(MeasureRow(agent="a", repo="o/r", env_status="ok", build_ok=True,
                                  status="config_restore_failed"))


def test_pytest_env_overrides_are_cleared():
    # PYTEST_ADDOPTS / PYTEST_PLUGINS (Dockerfile ENV or runtime handoff) could narrow the suite.
    lang = PythonLanguage()
    for cmd in (lang.gate_cmd(W), lang.collect_cmd(W), lang.run_cmd(W, f"{W}/logs/junit.xml")):
        assert "unset PYTEST_ADDOPTS PYTEST_PLUGINS;" in cmd
        assert cmd.index("unset PYTEST_ADDOPTS") < cmd.index("python -m pytest")
