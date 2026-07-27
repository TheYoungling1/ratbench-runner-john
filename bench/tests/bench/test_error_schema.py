# tests/bench/test_error_schema.py
import dataclasses
import json
from dataclasses import asdict

import pytest

from bench.schema import ArmErrorReport, ErrorEvent, MeasureRow, RepoVerdict


def test_error_event_defaults_and_frozen():
    e = ErrorEvent(token="ModuleNotFoundError", group="wrapt", group_kind="module",
                   category="module_not_found", source="collect")
    assert e.occurrences == 1 and e.raw == ""
    # FrozenInstanceError's message is "cannot assign to field 'token'" - it does NOT
    # contain the word "frozen". Match on the exception type.
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.token = "x"


def test_repo_verdict_uses_ratbench_status_vocabulary():
    v = RepoVerdict(agent="a", repo="o/r", status="collect_error", bucket="collect_error",
                    error_surface="masked", surface_reason="startup_abort")
    assert v.status_derived is False and v.status_flags == ()


def test_arm_error_report_defaults_are_empty():
    r = ArmErrorReport(agent="a", n_repos=0)
    assert r.token_repos == {} and r.crosstab == {} and r.n_status_derived == 0
    assert r.source == "collect"


def test_new_measure_row_fields_default_empty():
    row = MeasureRow(agent="a", repo="o/r", env_status="ok", build_ok=True)
    assert row.error_surface == "" and row.error_surface_reason == ""
    assert row.status_flags == () and row.repo_toplevel == ()
    assert row.status == "ok"          # existing default, unchanged


def test_old_row_json_without_new_fields_still_rehydrates():
    # unified_bench.py:53 does MeasureRow(agent=..., **d) over stored row.json.
    # The 100 rows in /opt/ratbench/remeasure_50 predate ALL of these fields, and `status` too.
    old = {"repo": "o/r", "env_status": "ok", "build_ok": True, "collect_rc": 2,
           "collect_errors": ["E   ModuleNotFoundError: No module named 'x'"],
           "pass_rate": 0.0, "meta": {}}
    d = {k: (tuple(v) if isinstance(v, list) else v) for k, v in json.loads(json.dumps(old)).items()}
    row = MeasureRow(agent="baseline", **d)
    # asdict() PRESERVES tuple-typed fields as tuples — `() == []` is False, so asserting a
    # list here can never pass regardless of implementation.
    assert row.error_surface == "" and asdict(row)["status_flags"] == ()
