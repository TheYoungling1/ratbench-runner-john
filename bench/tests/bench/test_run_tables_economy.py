# tests/bench/test_run_tables_economy.py — M4.5a: per_repo_table token/in-build columns read the
# produce-side _meta.json FIRST (so a produce-only run keeps repo2run's token total + live score),
# and fall back to agent_run_summary.json when _meta carries no economy — the byte-identity path
# every FROZEN historical run takes.
import json
import os

from bench.report import run_tables


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f)


def _row_of(run_dir, full_name):
    per_repo, _agg = run_tables.build(str(run_dir))
    by_full = {r["full"]: r for r in per_repo}
    return by_full[full_name]


def test_meta_economy_wins_over_absent_summary(tmp_path):
    # A produce-only run: _meta.json carries the token total + the method's inline live score, and
    # there is NO agent_run_summary.json. The tables must read tok/ib straight from _meta.
    out = tmp_path / "output" / "o" / "r"
    _write(str(out / "_result_row.json"), {"full_name": "o/r"})   # one row for score_agent to find
    _write(str(out / "_meta.json"),
           {"total_tokens": 777,
            "inline": {"command": "pytest", "success": True, "pass_rate": 0.5}})

    row = _row_of(tmp_path, "o/r")
    assert row["tok"] == 777
    assert row["ib"] == 0.5


def test_falls_back_to_summary_when_meta_has_no_economy(tmp_path):
    # A FROZEN-style run: OLD-format _meta.json (no economy/inline keys) + an agent_run_summary.json.
    # tok/ib must come from the summary, byte-identical to the pre-M4.5a behaviour.
    out = tmp_path / "output" / "o" / "r"
    _write(str(out / "_result_row.json"), {"full_name": "o/r"})
    _write(str(out / "_meta.json"),
           {"pid": 1, "start_ts": 0, "end_ts": 1, "duration_s": 1, "failure_reason": None,
            "requested_model": "x", "base_image": "python:3.10", "head_sha": "abc",
            "free_disk_gb": 10})       # OLD format: no total_tokens / tokens_in / tokens_out / inline
    _write(str(out / "agent_run_summary.json"),
           {"token_usage": {"total_tokens": 888},
            "best_in_sandbox_test_result": {"command": "pytest", "success": True, "pass_rate": 0.7}})

    row = _row_of(tmp_path, "o/r")
    assert row["tok"] == 888          # summary fallback (meta had no token economy)
    assert row["ib"] == 0.7           # summary best_in_sandbox_test_result
