# tests/bench/test_contract.py
"""The /testbed conformance probe (bench/contract.py).

Two layers of coverage:
  * REAL bash + git — run the actual probe script over a temp dir (W pointed at tmp_path) so the true
    C_dir/C2/C1/C3 branch logic is exercised, including the hardened C1 (`git rev-parse`, not a bare
    `[ -e .git ]`) and the C2-before-C1 ordering that makes empty_testbed reachable.
  * A canned docker stub — feed probe_testbed a fixed STATUS line to verify the status mapping for
    every taxonomy value.
"""
import os
import shutil
import subprocess

import pytest

from bench.contract import probe_testbed, _parse_probe


class _BashDocker:
    """docker-shaped stub whose .exec runs the argv locally via subprocess (bash -lc)."""

    def exec(self, name, argv, timeout=None):
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr), False


class _CannedDocker:
    """docker-shaped stub that returns a fixed probe stdout regardless of the command."""

    def __init__(self, out):
        self.out = out

    def exec(self, name, argv, timeout=None):
        return 0, self.out, False


def _git_init(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)


_needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="requires a local bash")
_needs_git = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None, reason="requires local bash + git")


# ---- real-shell probe over a temp /testbed --------------------------------------------------------

@_needs_git
def test_conforming_real_git_worktree_counts_py_test_files(tmp_path):
    _git_init(tmp_path)                                       # a REAL worktree (C1 via git rev-parse)
    (tmp_path / "test_a.py").write_text("def test_ok(): pass\n")
    (tmp_path / "b_test.py").write_text("def test_ok(): pass\n")
    (tmp_path / "conftest.py").write_text("\n")
    (tmp_path / "regular.py").write_text("x = 1\n")           # not a test file
    (tmp_path / ".git" / "test_ignore.py").write_text("x\n")  # excluded by -not -path '*/.git/*'
    # git rev-parse --show-toplevel returns the canonicalized path, so compare against realpath.
    r = probe_testbed(_BashDocker(), "n", W=os.path.realpath(str(tmp_path)))
    assert r["status"] == "conforming" and r["reason"] == "ok"
    assert r["py_test_files"] == 3     # test_a.py, b_test.py, conftest.py (NOT regular.py / .git/*)


@_needs_git
def test_conforming_worktree_with_zero_test_files_is_still_conforming(tmp_path):
    # C3 is DIAGNOSTIC ONLY — a real worktree with no test files is conforming with py_test_files=0.
    _git_init(tmp_path)
    (tmp_path / "app.py").write_text("x = 1\n")
    r = probe_testbed(_BashDocker(), "n", W=os.path.realpath(str(tmp_path)))
    assert r["status"] == "conforming" and r["py_test_files"] == 0


@_needs_bash
def test_non_conforming_when_absent(tmp_path):
    # /testbed does not exist -> non_conforming.
    r = probe_testbed(_BashDocker(), "n", W=str(tmp_path / "nope"))
    assert r["status"] == "non_conforming"


@_needs_git
def test_non_conforming_when_bogus_git_dir(tmp_path):
    # Non-empty dir with a BOGUS/empty .git (no real worktree) -> non_conforming. The old
    # `[ -e /testbed/.git ]` check would have wrongly passed this; `git rev-parse` rejects it.
    (tmp_path / "test_a.py").write_text("def test_ok(): pass\n")
    (tmp_path / ".git").mkdir()                               # empty .git => not a real worktree
    r = probe_testbed(_BashDocker(), "n", W=os.path.realpath(str(tmp_path)))
    assert r["status"] == "non_conforming" and r["reason"] == "not_git"


@_needs_git
def test_non_conforming_when_files_but_no_git(tmp_path):
    (tmp_path / "test_a.py").write_text("def test_ok(): pass\n")   # files, but never a git repo
    r = probe_testbed(_BashDocker(), "n", W=os.path.realpath(str(tmp_path)))
    assert r["status"] == "non_conforming" and r["reason"] == "not_git"


@_needs_bash
def test_empty_testbed_when_dir_is_empty(tmp_path):
    # A genuinely empty /testbed is now reachable as empty_testbed (C2 checked before the git check).
    r = probe_testbed(_BashDocker(), "n", W=str(tmp_path))
    assert r["status"] == "empty_testbed" and r["reason"] == "empty"


# ---- status mapping via a canned probe line (covers every taxonomy value) -------------------------

def test_maps_non_conforming_line():
    r = probe_testbed(_CannedDocker("STATUS=non_conforming reason=not_git py_test_files=0"), "n")
    assert r["status"] == "non_conforming" and r["py_test_files"] == 0


def test_maps_empty_testbed_line():
    r = probe_testbed(_CannedDocker("STATUS=empty_testbed reason=empty py_test_files=0"), "n")
    assert r["status"] == "empty_testbed"


def test_maps_conforming_line():
    r = probe_testbed(_CannedDocker("STATUS=conforming reason=ok py_test_files=4"), "n")
    assert r["status"] == "conforming" and r["py_test_files"] == 4


# ---- parser robustness ----------------------------------------------------------------------------

def test_parse_is_robust_to_noise_and_defaults_closed():
    out = "warning: something\nSTATUS=conforming reason=ok py_test_files=7\n"
    r = _parse_probe(out)
    assert r["status"] == "conforming" and r["py_test_files"] == 7
    # Unparseable / missing STATUS -> fail-closed to non_conforming (never a false green).
    assert _parse_probe("")["status"] == "non_conforming"
    assert _parse_probe("garbage with no tokens")["status"] == "non_conforming"
    # A bogus py_test_files value degrades to 0 rather than raising.
    assert _parse_probe("STATUS=conforming py_test_files=notanint")["py_test_files"] == 0
