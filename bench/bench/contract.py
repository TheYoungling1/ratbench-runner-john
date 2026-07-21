# bench/contract.py
from __future__ import annotations

# The /testbed contract guard (design §2.3). One shell probe inside the built image classifies the
# container against the conformance preconditions BEFORE the (unchanged) Repo2Run collect gate runs:
#   C_dir  — /testbed exists (a directory)                                   -> else non_conforming
#   C2     — /testbed non-empty                                              -> else empty_testbed
#   C1     — /testbed is a REAL git worktree whose toplevel IS /testbed      -> else non_conforming
#   C3     — count of python test files (test_*.py / *_test.py / conftest.py)-> DIAGNOSTIC ONLY
# C1 uses `git rev-parse` (not a bare `[ -e .git ]`, which a bogus/empty .git would pass) and requires
# the worktree toplevel to equal /testbed itself, so a /testbed nested inside a parent repo is rejected.
# C3 is recorded as `py_test_files` but never gates (option (a) minimal EBSR rule): a conforming repo
# with zero test files still runs the collect gate and is credited exactly as Repo2Run.

_VALID = ("conforming", "non_conforming", "empty_testbed")


def _probe_script(W: str) -> str:
    # C2 is checked BEFORE the git check so a genuinely empty /testbed is reachable as empty_testbed.
    # py_test_files is ALWAYS emitted (0 on the short-circuit paths) so callers read it uniformly.
    # echo emits whitespace-separated KEY=VALUE tokens for robust parsing.
    return (
        "set -o pipefail\n"
        f"if [ ! -d {W} ]; then echo STATUS=non_conforming reason=no_testbed py_test_files=0; exit 0; fi\n"
        f'if [ -z "$(ls -A {W} 2>/dev/null)" ]; then echo STATUS=empty_testbed reason=empty py_test_files=0; exit 0; fi\n'
        f'if [ "$(git -C {W} rev-parse --is-inside-work-tree 2>/dev/null)" != "true" ] || '
        f'[ "$(git -C {W} rev-parse --show-toplevel 2>/dev/null)" != "{W}" ]; then '
        "echo STATUS=non_conforming reason=not_git py_test_files=0; exit 0; fi\n"
        f"NT=$(find {W} \\( -name 'test_*.py' -o -name '*_test.py' -o -name 'conftest.py' \\) "
        "-not -path '*/.git/*' 2>/dev/null | wc -l | tr -d ' ')\n"
        "echo STATUS=conforming reason=ok py_test_files=$NT"
    )


def _parse_probe(out: str) -> dict:
    """Parse whitespace-separated KEY=VALUE tokens from the probe stdout (robust to blank lines/noise)."""
    tokens: dict = {}
    for line in (out or "").splitlines():
        for tok in line.split():
            if "=" in tok:
                key, _, val = tok.partition("=")
                tokens[key.strip()] = val.strip()
    status = tokens.get("STATUS", "")
    if status not in _VALID:
        # Unparseable / missing STATUS => fail-closed as non_conforming (denies EBSR, never a false
        # green). A genuine infra crash is handled one level up as measure_error.
        status = "non_conforming"
    try:
        py_test_files = int(tokens.get("py_test_files", 0))
    except (TypeError, ValueError):
        py_test_files = 0
    return {"status": status, "py_test_files": py_test_files, "reason": tokens.get("reason", "")}


def probe_testbed(docker, name: str, W: str = "/testbed") -> dict:
    """Run the /testbed conformance probe inside container `name`.

    Returns {"status": one of {conforming, non_conforming, empty_testbed},
             "py_test_files": int (diagnostic), "reason": str}.
    """
    result = docker.exec(name, ["bash", "-lc", _probe_script(W)])
    out = result[1] if isinstance(result, (tuple, list)) and len(result) >= 2 else ""
    return _parse_probe(out)
