# tests/bench/test_error_verdict.py
import collections
import json
import os

import pytest

from bench.measure import _MEASURABLE
from bench.schema import MeasureRow
from bench.verdict import (bucket, error_surface, legacy_status, resolve_status,
                           status_flags, verdict)

_FIXTURE = os.path.join(os.path.dirname(__file__), "data", "py50_rows.json")


def _row(**kw):
    base = dict(agent="a", repo="o/r", env_status="ok", build_ok=True, collect_rc=0,
                collect_clean=True, collected_node_ids=("tests/a.py::t",), executed=True,
                pass_rate=1.0, timed_out=False, meta={})
    base.update(kw)
    return MeasureRow(**base)


def _real_rows():
    with open(_FIXTURE) as f:
        return [MeasureRow(**{k: (tuple(v) if isinstance(v, list) else v) for k, v in d.items()})
                for d in json.load(f)]


# ---- env_status: the ported predicate was `!= "ok"` and this repo does not say "ok" ----
@pytest.mark.parametrize("env_status", _MEASURABLE)
def test_every_measurable_env_status_can_reach_a_surface(env_status):
    # measure.py:19 _MEASURABLE == ("ok", "legacy_ok", "produced"); measure.py:245 tests exactly
    # this set before deciding `missing`. The fork wrote `env_status != "ok"`, which scored a
    # `produced` row — 45 of the 50 real rows — as unobserved/no_env with a successful build.
    assert error_surface(_row(env_status=env_status)) == ("full", "")
    assert legacy_status(_row(env_status=env_status)) == "executed"


def test_a_non_measurable_env_status_is_still_unobserved():
    assert error_surface(_row(env_status="legacy_missing", build_ok=False)) \
        == ("unobserved", "no_env")
    assert legacy_status(_row(env_status="legacy_missing")) == "missing"


def test_the_real_corpus_is_not_uniformly_unobserved():
    """CANARY. The `!= "ok"` bug made all 50 rows unobserved/no_env and every legacy_status
    `missing`, and every unit test in this file still passed because they all pass
    env_status="ok". Only real rows catch a vocabulary mismatch."""
    rows = _real_rows()
    surfaces = collections.Counter(error_surface(r)[0] for r in rows)
    assert surfaces["full"] > 0 and surfaces["unobserved"] < len(rows)
    assert len(set(legacy_status(r) for r in rows)) > 1


# ---- error_surface -------------------------------------------------------------------
def test_per_module_failure_with_tests_collected_is_FULL_not_masked():
    # THE case that distinguishes surface from status: measure.py sets collect_error whenever
    # collect_clean is false, regardless of how many tests were collected (spec section 4)
    r = _row(collect_rc=2, collect_clean=False,
             collected_node_ids=tuple(f"t{i}.py::x" for i in range(500)))
    assert error_surface(r) == ("full", "")


def test_rc4_with_nothing_collected_is_masked_startup_abort():
    assert error_surface(_row(collect_rc=4, collect_clean=False, collected_node_ids=())) \
        == ("masked", "startup_abort")


def test_rc2_collecting_nothing_is_masked_not_full():
    # 9/50 baseline repos exit rc=2 having collected ZERO tests; an rc-in-{3,4} detector
    # scores these full and under-counts masking by more than half
    assert error_surface(_row(collect_rc=2, collect_clean=False, collected_node_ids=())) \
        == ("masked", "nothing_collected")


def test_rc5_no_tests_collected_is_masked_not_a_clean_full_surface():
    assert error_surface(_row(collect_rc=5, collect_clean=True, collected_node_ids=())) \
        == ("masked", "nothing_collected")


def test_build_failure_is_unobserved_not_clean():
    assert error_surface(_row(build_ok=False)) == ("unobserved", "build_failed")


def test_missing_env_is_unobserved():
    assert error_surface(_row(env_status="missing", build_ok=False)) == ("unobserved", "no_env")


# ---- legacy_status: MUST use this repo's vocabulary ----------------------------------
@pytest.mark.parametrize("kw,expected", [
    (dict(env_status="missing", build_ok=False), "missing"),
    (dict(build_ok=False, meta={"error": "CalledProcessError(125, ['docker','run'])"}),
     "measure_error"),
    (dict(build_ok=False, meta={"error": "OSError(28, 'No space left on device')"}),
     "measure_error"),
    (dict(build_ok=False), "build_fail"),
    (dict(timed_out=True), "timed_out"),
    # NOT "collect_error": measure.py assigns non_conforming/empty_testbed/gate_fail at gates
    # BEFORE the :344 block, from inputs that are never persisted. A collect-unclean legacy row
    # could be any of the four, so naming one would be a silent mislabelling (spec 3.1.1).
    (dict(collect_clean=False), "unknown_conformance"),
    (dict(executed=True), "executed"),
    (dict(executed=False), "no_tests_collected"),
])
def test_legacy_status_emits_only_existing_vocabulary(kw, expected):
    assert legacy_status(_row(**kw)) == expected


def test_legacy_status_never_invents_the_forks_vocabulary():
    from bench.verdict import LEGACY_STATUSES
    forbidden = {"no_env", "setup_failed", "zero_pass", "partial", "success", "infra_error"}
    assert not (set(LEGACY_STATUSES) & forbidden)


def test_unknown_conformance_is_the_only_new_name():
    from bench.verdict import LEGACY_STATUSES
    existing = {"unmeasurable", "error", "missing", "measure_error", "build_fail",
                "non_conforming", "empty_testbed", "no_tests_collected", "collect_error",
                "timed_out", "executed", "gate_fail"}
    assert set(LEGACY_STATUSES) - existing == {"unknown_conformance"}


def test_backfill_never_claims_a_status_it_cannot_know():
    # a non_conforming row still runs collect, so collect_clean is usually False. Labelling it
    # collect_error would be confidently wrong.
    assert legacy_status(_row(collect_clean=False)) != "collect_error"


# ---- resolve_status ------------------------------------------------------------------
def test_measured_status_wins_and_is_not_marked_derived():
    assert resolve_status(_row(status="gate_fail")) == ("gate_fail", False)


def test_absent_or_legacy_status_is_backfilled_and_marked():
    assert resolve_status(_row(status="legacy_ok", build_ok=False)) == ("build_fail", True)
    assert resolve_status(_row(status="ok", timed_out=True)) == ("timed_out", True)


# ---- bucket --------------------------------------------------------------------------
def test_bucket_passes_non_executed_status_through():
    assert bucket("collect_error", 0.0) == "collect_error"
    assert bucket("build_fail", 0.0) == "build_fail"


def test_bucket_splits_executed_on_pass_rate():
    assert bucket("executed", 0.0) == "zero_pass"
    assert bucket("executed", 0.5) == "partial"
    assert bucket("executed", 0.8) == "success"
    assert bucket("executed", 0.79) == "partial"


# ---- flags ---------------------------------------------------------------------------
def test_status_flags_carry_what_the_ordering_hid():
    r = _row(timed_out=True, build_ok=False, collect_clean=False)
    assert legacy_status(r) == "build_fail"          # build_ok checked before timed_out
    assert "timed_out" in status_flags(r)            # ...but the flag preserves it


def test_collect_error_flag_requires_evidence_that_collect_RAN():
    # `collect_clean` DEFAULTS to False, so it is False on every row that died before collect.
    # `collect_rc is None` is the observation guard. On the real fixture this fabricated a
    # collect_error flag on all 7 rows that never reached collect.
    r = _row(build_ok=False, collect_rc=None, collect_clean=False, status="timed_out")
    assert "collect_error" not in status_flags(r)


def test_collect_error_flag_still_fires_when_collect_did_run():
    r = _row(collect_rc=2, collect_clean=False, status="timed_out")
    assert "collect_error" in status_flags(r)


def test_build_fail_flag_requires_a_buildable_env():
    # measure.py:245 short-circuits before docker.build when env_status is not measurable, so
    # build_ok=False there is a default, not an observed build failure.
    r = _row(env_status="legacy_missing", build_ok=False, collect_rc=None, collect_clean=False)
    assert "build_fail" not in status_flags(r)


def test_no_flag_contradicts_the_scalar_status_on_the_real_corpus():
    for r in _real_rows():
        chosen, _ = resolve_status(r)
        assert chosen not in status_flags(r), r.repo


# ---- unconverged: threaded through 9 call sites, so it needs its own coverage ----------
def test_unconverged_flag_requires_an_explicit_turn_cap():
    # num_turn is never written into bench_meta, so a meta fallback would silently read as
    # "everything converged"
    r = _row(turns_used=30, pass_rate=0.0)
    assert "unconverged" not in verdict(r).status_flags
    assert "unconverged" in verdict(r, turn_cap=30).status_flags


def test_unconverged_is_false_when_the_run_succeeded_at_the_cap():
    assert "unconverged" not in verdict(_row(turns_used=30, pass_rate=1.0), turn_cap=30).status_flags


def test_unconverged_tolerates_missing_turns_used():
    assert "unconverged" not in verdict(_row(turns_used=None, pass_rate=0.0), turn_cap=30).status_flags


def test_unconverged_is_a_flag_never_a_bucket():
    v = verdict(_row(turns_used=30, pass_rate=0.5, status="executed"), turn_cap=30)
    assert v.bucket == "partial" and "unconverged" in v.status_flags


def test_verdict_combines_everything():
    v = verdict(_row(collect_rc=4, collect_clean=False, collected_node_ids=(),
                     executed=False, pass_rate=0.0))
    # collect_clean False on a legacy row is AMBIGUOUS between collect_error / non_conforming /
    # empty_testbed / gate_fail, so the backfill names the ambiguity
    assert v.status == "unknown_conformance" and v.bucket == "unknown_conformance"
    assert (v.error_surface, v.surface_reason) == ("masked", "startup_abort")
    assert v.status_derived is True and v.repo == "o/r"
