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
                      executed=True, ebsr=True, pass_rate=1.0, total=3, passed=3, collect_clean=True)


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
    assert "docker daemon died" in d["meta"]["error"]


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
