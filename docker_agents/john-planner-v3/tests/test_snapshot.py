from src.envstate.snapshot import probe_env, EnvSnapshot


def _fake_exec(table):
    """Return an exec_readonly(cmd)->(rc, out) backed by a substring table."""
    def run(cmd):
        for key, (rc, out) in table.items():
            if key in cmd:
                return rc, out
        return 1, ""
    return run


def test_parses_installed_and_env():
    table = {
        "pip list --format=freeze": (0, "flask==3.0.0\nsetuptools==69.0.0\n"),
        "python --version": (0, "Python 3.12.1"),
        "uname -m": (0, "x86_64"),
    }
    snap = probe_env(_fake_exec(table))
    names = {f.name.lower(): f.detail for f in snap.installed}
    assert names["flask"] == "3.0.0"
    assert "setuptools" in names                # included via pip list --format=freeze
    assert snap.env["python_version"] == "Python 3.12.1"
    assert snap.env["arch"] == "x86_64"


def test_total_failure_returns_empty_snapshot():
    snap = probe_env(lambda cmd: (1, ""))
    assert snap == EnvSnapshot()
    assert snap.env == {}


def test_skips_malformed_freeze_lines():
    table = {"pip list --format=freeze": (0, "-e git+https://x\n\nflask==3.0.0\n"), "uname -m": (0, "x86_64")}
    snap = probe_env(_fake_exec(table))
    assert [f.name for f in snap.installed] == ["flask"]
