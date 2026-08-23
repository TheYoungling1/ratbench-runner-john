"""Parity tests: RunOracle vs Synthesizer for is_test_command and analyze_test_run."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest
from src.synthesizer import Synthesizer
from src.run_oracle import RunOracle

_S = Synthesizer()
_T = RunOracle()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cmp_is_test(cmd):
    """Assert is_test_command parity and return the value."""
    sv = _S.is_test_command(cmd)
    tv = _T.is_test_command(cmd)
    assert sv == tv, f"is_test_command({cmd!r}): Synthesizer={sv}, RunOracle={tv}"
    return tv


def _cmp_analyze(cmd, obs=""):
    """Assert analyze_test_run parity and return the value."""
    sv = _S.analyze_test_run(cmd, obs)
    tv = _T.analyze_test_run(cmd, obs)
    obs_short = repr(obs)[:40]
    assert sv == tv, (
        f"analyze_test_run({cmd!r}, obs={obs_short}):\n"
        f"  Synthesizer => {sv}\n"
        f"  RunOracle  => {tv}"
    )
    return tv


# ---------------------------------------------------------------------------
# is_test_command — 10 cases
# ---------------------------------------------------------------------------

def test_plain_pytest():
    assert _cmp_is_test("pytest -q") is True


def test_python_m_pytest():
    assert _cmp_is_test("python -m pytest tests/") is True


def test_python3_m_pytest():
    assert _cmp_is_test("python3 -m pytest tests/") is True


def test_venv_run_pytest():
    # venv-wrapped — still IS a test command (wrapping is checked separately)
    assert _cmp_is_test("poetry run pytest") is True


def test_collect_only():
    # --collect-only is still a pytest command
    assert _cmp_is_test("pytest --collect-only") is True


def test_ls_not_test():
    assert _cmp_is_test("ls -la") is False


def test_pip_show_not_test():
    assert _cmp_is_test("pip show requests") is False


def test_piped_pytest_grep():
    # piped command — pytest part makes it a test command
    assert _cmp_is_test("pytest -q | grep PASSED") is True


def test_heredoc_not_test():
    cmd = "cat <<'EOF'\nhello\nEOF"
    assert _cmp_is_test(cmd) is False


def test_go_test():
    assert _cmp_is_test("go test ./...") is True


# ---------------------------------------------------------------------------
# analyze_test_run — 10 cases spanning confidence + reason paths
# ---------------------------------------------------------------------------

_PASS_OBS = "5 passed in 0.12s"
_FAIL_OBS = "2 failed, 3 passed"
_COLLECT_OBS = "collected 10 items"
_SKIP_OBS = "3 skipped"
_HELP_OBS = "usage: pytest [options]\noptional arguments:\n  -h, --help  show this help"
_EMPTY_OBS = "collected 0 items"


def test_analyze_passing_pytest():
    r = _cmp_analyze("pytest -q", _PASS_OBS)
    assert r["is_effective_test_run"] is True
    assert r["confidence"] == "high"


def test_analyze_collect_only_output():
    # collect-only command with output showing only collected items
    r = _cmp_analyze("pytest --collect-only", _COLLECT_OBS)
    # parity checked; result may or may not be effective — just verify match


def test_analyze_all_skipped():
    r = _cmp_analyze("pytest -q", _SKIP_OBS)
    # parity is the only requirement here


def test_analyze_help_text():
    r = _cmp_analyze("pytest -q", _HELP_OBS)
    assert r["reason"] == "help_or_usage_output"


def test_analyze_failure_obs():
    r = _cmp_analyze("pytest -q", _FAIL_OBS)
    assert r["reason"] == "test_failure_signal"


def test_analyze_empty_run():
    r = _cmp_analyze("pytest -q", _EMPTY_OBS)
    assert r["reason"] == "no_tests_executed"


def test_analyze_non_test_cmd():
    r = _cmp_analyze("ls -la", _PASS_OBS)
    assert r["is_test_command"] is False
    assert r["is_effective_test_run"] is False


def test_analyze_piped_truncated():
    r = _cmp_analyze("pytest -q | tail -5", _PASS_OBS)
    assert r["reason"] == "truncated_test_output"


def test_analyze_no_obs():
    r = _cmp_analyze("pytest -q", "")
    assert r["is_effective_test_run"] is False


def test_analyze_go_test_pass_obs():
    obs = "ok  \tgithub.com/user/repo\t0.123s"
    r = _cmp_analyze("go test ./...", obs)
    assert r["is_effective_test_run"] is True
