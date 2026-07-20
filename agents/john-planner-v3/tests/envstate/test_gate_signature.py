"""Unit tests for src/envstate/gate_signature.py (design: residual-giveup-fix.md §B/§G.1).

Pure string-in/string-out module — no src.envstate dependency, no orchestrator
wiring. These tests pin the two properties the no-progress detector depends on:

  outcome_signature: STABLE across a re-run of the identical failing suite
                      (volatile tokens stripped), SENSITIVE to any real change
                      (new failure, a test passing, a different exception).
  next_stall:         advances/resets the consecutive-identical-failure counter.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.envstate.gate_signature import next_stall, outcome_signature

# ---------------------------------------------------------------------------
# outcome_signature
# ---------------------------------------------------------------------------


def test_passed_true_is_always_pass_sentinel():
    assert outcome_signature(True, "") == "pass"
    assert outcome_signature(True, "anything at all, even a FAILED line") == "pass"


def test_stability_across_volatile_tokens():
    """Same FAILED lines, different durations/addresses/tmp-paths/line-numbers/
    xdist tags -> identical signature."""
    out_a = (
        "FAILED tests/x.py::test_y - AssertionError\n"
        "0x7fabc1234000 in frame at /private/tmp/pytest-of-x/test_y0/foo line 42 [gw0]\n"
        "=== 1 failed, 3 passed in 3.42s ==="
    )
    out_b = (
        "FAILED tests/x.py::test_y - AssertionError\n"
        "0xdeadbeef in frame at /tmp/pytest-of-y/test_y1/bar line 99 [gw3]\n"
        "=== 1 failed, 3 passed in 12.9s ==="
    )
    assert outcome_signature(False, out_a) == outcome_signature(False, out_b)


def test_sensitivity_new_failure():
    base = "FAILED tests/x.py::test_y - AssertionError\n=== 1 failed, 3 passed in 1s ==="
    more = (
        "FAILED tests/x.py::test_y - AssertionError\n"
        "FAILED tests/x.py::test_z - KeyError\n"
        "=== 2 failed, 2 passed in 1s ==="
    )
    assert outcome_signature(False, base) != outcome_signature(False, more)


def test_sensitivity_a_test_starts_passing():
    four_failed = (
        "FAILED tests/x.py::test_a - AssertionError\n"
        "FAILED tests/x.py::test_b - AssertionError\n"
        "FAILED tests/x.py::test_c - AssertionError\n"
        "FAILED tests/x.py::test_d - AssertionError\n"
        "=== 4 failed in 1s ==="
    )
    three_failed = (
        "FAILED tests/x.py::test_a - AssertionError\n"
        "FAILED tests/x.py::test_b - AssertionError\n"
        "FAILED tests/x.py::test_c - AssertionError\n"
        "=== 3 failed, 1 passed in 1s ==="
    )
    assert outcome_signature(False, four_failed) != outcome_signature(False, three_failed)


def test_sensitivity_different_exception():
    assertion_err = "FAILED tests/x.py::test_y - AssertionError\n=== 1 failed in 1s ==="
    runtime_err = "FAILED tests/x.py::test_y - RuntimeError\n=== 1 failed in 1s ==="
    assert outcome_signature(False, assertion_err) != outcome_signature(False, runtime_err)


def test_fallback_path_no_pytest_summary_stable_across_volatiles():
    crash_a = "Traceback (most recent call last):\nSegmentation fault at 0x7fabc1234000 pid: 4821"
    crash_b = "Traceback (most recent call last):\nSegmentation fault at 0xdeadbeef pid:9911"
    assert outcome_signature(False, crash_a) == outcome_signature(False, crash_b)


def test_fallback_path_different_crash_is_different():
    crash_a = "Traceback (most recent call last):\nSegmentation fault"
    crash_b = "Traceback (most recent call last):\nBus error"
    assert outcome_signature(False, crash_a) != outcome_signature(False, crash_b)


def test_pass_vs_fail_always_different_and_fail_prefix():
    fail_sig = outcome_signature(False, "FAILED tests/x.py::test_y - AssertionError")
    pass_sig = outcome_signature(True, "1 passed")
    assert fail_sig != pass_sig
    assert pass_sig == "pass"
    assert fail_sig.startswith("fail:")


def test_no_summary_empty_output_is_stable_fail():
    assert outcome_signature(False, "") == outcome_signature(False, "")
    assert outcome_signature(False, "").startswith("fail:")


# ---------------------------------------------------------------------------
# next_stall
# ---------------------------------------------------------------------------


def test_next_stall_first_sight_of_failure():
    assert next_stall(None, "fail:a", 0) == 1


def test_next_stall_extends_on_identical_signature():
    assert next_stall("fail:a", "fail:a", 1) == 2
    assert next_stall("fail:a", "fail:a", 2) == 3


def test_next_stall_resets_on_changed_signature():
    assert next_stall("fail:a", "fail:b", 2) == 1


def test_next_stall_resets_on_pass():
    assert next_stall("fail:a", "pass", 2) == 0
    assert next_stall(None, "pass", 0) == 0
