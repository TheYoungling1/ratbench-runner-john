# tests/bench/test_error_aggregation.py
import json
import os
from dataclasses import asdict

import pytest

from bench.schema import MeasureRow
from bench.unified_bench import aggregate, aggregate_errors, load_rows, main

MNF = "E   ModuleNotFoundError: No module named 'wrapt'"


def _write(root, agent, repo, **kw):
    base = dict(agent=agent, repo=repo, env_status="ok", build_ok=True, status="executed",
                collect_rc=0, collect_clean=True, collected_node_ids=("tests/a.py::t",),
                executed=True, collect_errors=(MNF,), pass_rate=0.5, meta={})
    base.update(kw)
    p = os.path.join(root, agent, *repo.split("/"), "row.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(asdict(MeasureRow(**base)), f, default=list)


def test_load_rows_groups_by_agent(tmp_path):
    _write(str(tmp_path), "baseline", "o/1")
    _write(str(tmp_path), "repaired", "o/1")
    assert set(load_rows(str(tmp_path))) == {"baseline", "repaired"}


def test_load_rows_ignores_unknown_fields_from_a_newer_checkout(tmp_path):
    _write(str(tmp_path), "baseline", "o/1")
    p = os.path.join(str(tmp_path), "baseline", "o", "1", "row.json")
    d = json.load(open(p)); d["a_field_from_the_future"] = 1
    json.dump(d, open(p, "w"))
    assert load_rows(str(tmp_path))["baseline"][0].repo == "o/1"


def test_load_rows_marks_a_status_it_backfilled(tmp_path):
    # A row with no build/execute signal is backfilled to "missing" — indistinguishable by value
    # from a measured "missing", so load_rows stamps the marker and resolve_status honours it.
    from bench.verdict import resolve_status
    _write(str(tmp_path), "baseline", "o/1", build_ok=False, executed=False, collect_rc=None,
           collect_clean=False, env_status="missing")
    p = os.path.join(str(tmp_path), "baseline", "o", "1", "row.json")
    d = json.load(open(p)); d.pop("status"); json.dump(d, open(p, "w"))

    row = load_rows(str(tmp_path))["baseline"][0]
    assert row.status == "missing"                       # existing backfill, unchanged
    assert row.status_backfilled is True                  # ...but marked as invented
    assert resolve_status(row)[1] is True                 # ...so it counts as derived


def test_aggregate_errors_is_keyed_by_agent_then_source(tmp_path):
    _write(str(tmp_path), "baseline", "o/1")
    _write(str(tmp_path), "baseline", "o/2", collect_rc=4, collect_clean=False,
           collected_node_ids=(), status="collect_error")
    out = aggregate_errors(str(tmp_path))
    assert set(out["baseline"]) == {"collect", "run"}
    assert out["baseline"]["collect"]["n_masked"] == 1
    assert out["baseline"]["run"]["token_repos"] == {}


def test_metrics_output_shape_is_unchanged(tmp_path):
    _write(str(tmp_path), "baseline", "o/1")
    m = aggregate(str(tmp_path))
    assert set(m) == {"baseline"} and "errors" not in m["baseline"]


def test_main_writes_errors_json_next_to_metrics_json(tmp_path):
    _write(str(tmp_path), "baseline", "o/1")
    assert main(["--out", str(tmp_path), "--aggregate-only"]) == 0
    assert os.path.isfile(os.path.join(str(tmp_path), "metrics.json"))
    assert "baseline" in json.load(open(os.path.join(str(tmp_path), "errors.json")))


def test_unknown_agent_in_delta_fails_before_writing_anything(tmp_path):
    _write(str(tmp_path), "baseline", "o/1")
    assert main(["--out", str(tmp_path), "--aggregate-only", "--delta", "baseline:nope"]) == 2
    assert not os.path.exists(os.path.join(str(tmp_path), "errors.json"))
    assert not os.path.exists(os.path.join(str(tmp_path), "metrics.json"))


def test_metrics_json_is_written_even_if_classification_raises(tmp_path, monkeypatch):
    _write(str(tmp_path), "baseline", "o/1")
    import bench.unified_bench as ub
    monkeypatch.setattr(ub, "aggregate_errors",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert main(["--out", str(tmp_path), "--aggregate-only"]) == 0
    assert os.path.isfile(os.path.join(str(tmp_path), "metrics.json"))
    assert not os.path.exists(os.path.join(str(tmp_path), "errors.json"))


def test_a_stale_errors_json_is_REMOVED_when_classification_raises(tmp_path, monkeypatch):
    # metrics.json is rewritten unconditionally, so a surviving errors.json from an earlier run
    # would be read as part of the same report — with exit 0 and nothing signalling the mismatch.
    # The test above only passes because its directory is fresh.
    _write(str(tmp_path), "baseline", "o/1")
    stale = os.path.join(str(tmp_path), "errors.json")
    assert main(["--out", str(tmp_path), "--aggregate-only"]) == 0
    assert os.path.isfile(stale)                       # a real errors.json now exists

    import bench.unified_bench as ub
    monkeypatch.setattr(ub, "aggregate_errors",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert main(["--out", str(tmp_path), "--aggregate-only"]) == 0
    assert os.path.isfile(os.path.join(str(tmp_path), "metrics.json"))
    assert not os.path.exists(stale)                   # ...and is gone, not stale


def test_bad_delta_fails_before_ANY_measuring_in_the_full_path(tmp_path, monkeypatch):
    # The --aggregate-only test above cannot catch this: in the full path the validation used to
    # sit AFTER run_one(), so a typo cost an entire docker measure pass before returning 2.
    # `discover` explodes if reached, so "we got to the measure stage" fails loudly.
    import bench.unified_bench as ub
    monkeypatch.setattr(ub, "discover",
                        lambda *a, **k: pytest.fail("reached discover(): validation was too late"))
    rc = main(["--out", str(tmp_path), "--harvest", "baseline=/nonexistent",
               "--delta", "baseline:nope"])
    assert rc == 2
    assert os.listdir(str(tmp_path)) == []


def test_delta_may_name_an_arm_measured_EARLIER_not_in_this_harvest(tmp_path):
    # run_one resumes, so measuring one new arm while --delta compares two already-measured arms
    # is a valid invocation. An early check against --harvest alone would reject it outright.
    import bench.unified_bench as ub
    _write(str(tmp_path), "baseline", "o/1")
    _write(str(tmp_path), "repaired", "o/1")
    monkey = []
    ub_discover = ub.discover
    try:
        ub.discover = lambda *a, **k: monkey.append(1) or []
        rc = main(["--out", str(tmp_path), "--harvest", "newarm=/nonexistent",
                   "--delta", "baseline:repaired"])
    finally:
        ub.discover = ub_discover
    assert monkey == [1], "early --delta check rejected a valid resumed run"
    assert rc == 0 and os.path.isfile(os.path.join(str(tmp_path), "errors.json"))


def test_malformed_delta_is_rejected(tmp_path):
    _write(str(tmp_path), "baseline", "o/1")
    # NOT in this list: "" — it is falsy, so an empty --delta reads as "no delta requested",
    # same as omitting the flag. That is deliberate, not a gap.
    for bad in ("baseline", "baseline:", ":repaired"):
        assert main(["--out", str(tmp_path), "--aggregate-only", "--delta", bad]) == 2
    assert not os.path.exists(os.path.join(str(tmp_path), "metrics.json"))
