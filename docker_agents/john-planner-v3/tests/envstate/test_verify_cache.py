"""Unit tests for VerifyTestCache — the container-generation-keyed memo for
VERIFY_TEST_CMD (design: testgate-certify.md §1/§5.1).

Drives the cache directly with a fake executor + a mutable gen box, so the
invalidation contract is tested without a full run_v3.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.envstate.verify_cache import VerifyTestCache


def _make(gen_box, results):
    calls = {"n": 0}

    def exec_test():
        r = results[min(calls["n"], len(results) - 1)]
        calls["n"] += 1
        return r

    cache = VerifyTestCache(exec_test=exec_test, gen=lambda: gen_box[0])
    return cache, calls


def test_second_call_same_generation_is_memoized():
    gen = [0]
    cache, calls = _make(gen, [(False, "fail-A")])
    assert cache.run() == (False, "fail-A")
    assert cache.run() == (False, "fail-A")     # same gen -> memo hit
    assert calls["n"] == 1                       # executed exactly once


def test_mutation_between_two_calls_forces_rerun():
    # THE anti-stale test: a container mutation (gen bump) between two gate calls
    # must invalidate the memo, even though the executor's second result differs.
    gen = [0]
    cache, calls = _make(gen, [(False, "fail-before"), (True, "pass-after")])
    assert cache.run() == (False, "fail-before")
    gen[0] += 1                                   # simulate reset/install
    assert cache.run() == (True, "pass-after")    # re-run, NOT the stale pass/fail
    assert calls["n"] == 2


def test_first_call_always_executes():
    gen = [7]
    cache, calls = _make(gen, [(True, "x")])
    assert cache.run() == (True, "x")
    assert calls["n"] == 1
