"""Execute PythonLanguage.ensure_cmd + run_cmd under bash with `python` replaced by a shim.

The harness must not change the repo's own test stack. Three measured false zeros came from it:
  - EvalAI: `pip install pytest pytest-timeout` upgraded its pinned pytest 5.4.2 to 9.x (pytest-timeout
    needs pytest>=7), and pytest 9 rejects its `[pytest]` setup.cfg section -> collection failed.
  - Checkmk: pytest-timeout 2.4 registers --session-timeout, which Checkmk's conftest also defines.
  - Enterr: SeleniumBase's plugin raises when --timeout is on the command line.
"""
import shutil
import subprocess

import pytest

from bench.languages import get_language

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# State lives in $SHIM_STATE: one file per installed package. $SHIM_PYTEST_MAJOR is the repo's
# pytest major version; $SHIM_PROBE_RC is what `pytest --timeout ... --help` exits with when the
# plugin is installed (non-zero = the repo cannot load it). Every pip/pytest call is logged.
_SHIM = r"""#!/bin/sh
S="$SHIM_STATE"
echo "$*" >> "$S/log"
case "$1 $2" in
  "-m pip")
    op=$3; shift 3
    for a in "$@"; do case "$a" in -*) ;; *)
      if [ "$op" = install ]; then touch "$S/pkg_$a"; else rm -f "$S/pkg_$a"; fi;; esac; done
    exit 0;;
  "-m pytest")
    [ -f "$S/pkg_pytest" ] || exit 1
    case "$*" in *--version*) exit 0;; esac
    case "$*" in *--help*)
      # Real pytest 5.4.2 prints help and exits 0 even with an unknown --timeout (EvalAI).
      [ -f "$S/pkg_pytest-timeout" ] || exit 0
      exit "${SHIM_PROBE_RC:-0}";; esac
    exit 0;;
esac
if [ "$1" = "-c" ]; then
  case "$2" in
    *"import pytest_timeout"*) [ -f "$S/pkg_pytest-timeout" ]; exit $?;;
    *"__version__"*) [ "${SHIM_PYTEST_MAJOR:-8}" -ge 7 ]; exit $?;;
  esac
  exit 1
fi
exit 0
"""


def _run(tmp_path, *, pytest_major=8, has=("pytest",), probe_rc=0):
    bindir, state, W = tmp_path / "bin", tmp_path / "state", tmp_path / "testbed"
    for d in (bindir, state, W):
        d.mkdir()
    (bindir / "python").write_text(_SHIM)
    (bindir / "python").chmod(0o755)
    for pkg in has:
        (state / f"pkg_{pkg}").touch()
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "SHIM_STATE": str(state),
           "SHIM_PYTEST_MAJOR": str(pytest_major), "SHIM_PROBE_RC": str(probe_rc)}
    lang = get_language("python")
    for cmd in (lang.ensure_cmd(str(W)), lang.run_cmd(str(W), f"{W}/logs/junit.xml")):
        subprocess.run(["bash", "-c", cmd], env=env, check=True, cwd=W)
    log = (state / "log").read_text().splitlines()
    installed = {p.name[4:] for p in state.glob("pkg_*")}
    run = [line for line in log if line.startswith("-m pytest -q")]
    return log, installed, run[-1]


def test_modern_pytest_gets_timeout_without_touching_pytest(tmp_path):
    log, installed, run = _run(tmp_path)
    assert installed == {"pytest", "pytest-timeout"}
    assert any("install" in l and "--no-deps" in l and "pytest-timeout" in l for l in log)
    assert not any("install" in l and l.split()[-1] == "pytest" for l in log)
    assert "--timeout=120" in run


def test_old_pytest_is_left_alone_and_runs_without_timeout(tmp_path):
    # EvalAI: pinned pytest 5.4.2 — nothing may be installed, pytest must not be upgraded.
    log, installed, run = _run(tmp_path, pytest_major=5)
    assert installed == {"pytest"}
    assert not any(l.startswith("-m pip install") for l in log)
    assert "--timeout" not in run


def test_missing_pytest_is_installed(tmp_path):
    log, installed, run = _run(tmp_path, has=())
    assert "pytest" in installed
    assert "--timeout=120" in run


def test_plugin_the_repo_cannot_load_is_removed_again(tmp_path):
    # Checkmk (--session-timeout clash) / Enterr (SeleniumBase refuses --timeout).
    log, installed, run = _run(tmp_path, probe_rc=1)
    assert installed == {"pytest"}
    assert any(l.startswith("-m pip uninstall") and "pytest-timeout" in l for l in log)
    assert "--timeout" not in run


def test_repo_own_timeout_plugin_is_never_uninstalled(tmp_path):
    log, installed, run = _run(tmp_path, has=("pytest", "pytest-timeout"), probe_rc=1)
    assert installed == {"pytest", "pytest-timeout"}
    assert not any(l.startswith("-m pip") for l in log)
    assert "--timeout" not in run
