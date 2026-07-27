# tests/bench/test_error_aggregation.py
import json
import os
from dataclasses import asdict

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
