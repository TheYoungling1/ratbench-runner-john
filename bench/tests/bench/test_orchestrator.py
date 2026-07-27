# tests/bench/test_orchestrator.py
import json
import os
from dataclasses import asdict
from bench.schema import HarvestedEnv, RepoSpec, MeasureRow
from bench import unified_bench as ub


def _env(agent="v3", repo="o/r"):
    return HarvestedEnv(agent, RepoSpec(repo, f"https://github.com/{repo}"), "FROM x",
                        base_image="python:3.13-slim", meta={"tokens_in": 1, "tokens_out": 2})


def _fake_measure(env, *, docker, **kw):
    return MeasureRow(agent=env.agent, repo=env.repo.full_name, env_status="ok", build_ok=True,
                      status="executed", executed=True, ebsr=True, pass_rate=1.0, total=3, passed=3,
                      collect_clean=True, collect_rc=0)


def test_run_one_writes_row_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, "measure", _fake_measure)
    p1 = ub.run_one(_env(), str(tmp_path), docker=object())
    with open(p1) as f:
        assert json.load(f)["ebsr"] is True
    monkeypatch.setattr(ub, "measure", lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-ran")))
    p2 = ub.run_one(_env(), str(tmp_path), docker=object())
    assert p2 == p1


def test_run_one_writes_antivanish_row_on_measure_crash(tmp_path, monkeypatch):
    def _boom(env, *, docker, **kw):
        raise RuntimeError("docker daemon died")
    monkeypatch.setattr(ub, "measure", _boom)
    p = ub.run_one(_env(), str(tmp_path), docker=object())
    with open(p) as f:
        d = json.load(f)
    assert d["executed"] is False and d["ebsr"] is False and d["build_ok"] is False
    assert d["status"] == "measure_error"
    assert "docker daemon died" in d["meta"]["error"]


def test_measure_crash_keeps_the_producer_economy(tmp_path, monkeypatch):
    """A crash in MEASURE must not erase what PRODUCE already paid for.

    Measured on ccdf-full50-20260727-023223: mlflow/mlflow has an intact _meta.json
    (status="produced", cost_usd=1.8553) yet its row reads cost_usd=None — the anti-vanish branch
    rebuilt the row from scratch and dropped env.meta, so real spend vanished from total_cost_usd.
    Distinct from the produce-side denominator bug (runner/tests/test_meta_merge.py): here the
    _meta.json on disk is perfectly fine and it is the ROW that loses it."""
    env = HarvestedEnv("v3", RepoSpec("o/r", "https://github.com/o/r"), "FROM x",
                       base_image="python:3.13-slim",
                       meta={"tokens_in": 900, "tokens_out": 40, "llm_calls": 7,
                             "turns_used": 12, "cost_usd": 1.8553, "produce_s": 88.0,
                             "status": "produced"})
    monkeypatch.setattr(ub, "measure",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("docker daemon died")))
    with open(ub.run_one(env, str(tmp_path), docker=object())) as f:
        d = json.load(f)
    assert d["status"] == "measure_error"
    assert d["cost_usd"] == 1.8553
    assert d["tokens_in"] == 900 and d["turns_used"] == 12 and d["llm_calls"] == 7
    assert d["produce_s"] == 88.0
    # the crash detail must survive alongside the recovered meta, not replace it
    assert "docker daemon died" in d["meta"]["error"]
    assert d["meta"]["status"] == "produced"


def test_measure_crash_tolerates_absent_meta(tmp_path, monkeypatch):
    """Non-producer agents harvest with meta={}; the fallback must not KeyError on them."""
    env = HarvestedEnv("v3", RepoSpec("o/r", "https://github.com/o/r"), "FROM x",
                       base_image="python:3.13-slim", meta={})
    monkeypatch.setattr(ub, "measure",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with open(ub.run_one(env, str(tmp_path), docker=object())) as f:
        d = json.load(f)
    assert d["status"] == "measure_error" and d["cost_usd"] is None
    assert "boom" in d["meta"]["error"]


def test_aggregate_globs_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, "measure", _fake_measure)
    ub.run_one(_env(agent="v3", repo="o/r"), str(tmp_path), docker=object())
    out = ub.aggregate(str(tmp_path))
    assert out["v3"]["n"] == 1 and out["v3"]["EBSR"] == 1.0 and out["v3"]["ESSR_all"] == 1.0


def test_run_one_write_is_atomic_no_tmp_left(tmp_path, monkeypatch):
    monkeypatch.setattr(ub, "measure", _fake_measure)
    p = ub.run_one(_env(), str(tmp_path), docker=object())
    with open(p) as f:
        assert os.path.exists(p) and json.load(f)["ebsr"] is True
    # atomic publish leaves no .tmp sibling
    assert not os.path.exists(p + ".tmp")


def test_main_errors_without_harvest_unless_aggregate_only(tmp_path):
    import pytest
    with pytest.raises(SystemExit) as exc:
        ub.main(["--out", str(tmp_path)])
    assert exc.value.code == 2
