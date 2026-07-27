# runner/tests/test_meta_merge.py — the produce-FAILURE _meta.json write.
#
# _ProducerModel.predict() calls write_env_packet UNCONDITIONALLY (benchmark.py:157), so a failed
# produce already has an authoritative _meta.json{status:"error", cost_usd, tokens...} on disk. Only
# SUCCESS writes run_produced.json, and _run_one's meta write keyed on that marker alone — so the
# failure path dumped _collect_meta's 9 keys over the top and destroyed 17, including `status` and
# `cost_usd`.
#
# Downstream that is fatal and silent: harvest.py:77 finds no `status`, eval_build/ was already
# removed for a non-"produced" status (producers/base.py:268-271) so df_path is None, and the packet
# resolves to `legacy_missing` (harvest.py:82) — which metrics.py:36 EXCLUDES from n. Measured on
# node-full50-20260727-104952: n read 48 instead of 50, EBSR 0.9583 instead of 0.92, and
# total_cost_usd $43.4387 instead of the true $47.4781 — the $4.0394 gap being exactly the two
# failed repos' real spend ($2.0325 + $2.0069, both error_max_budget_usd).
#
# The read side is CORRECT and must not be touched: `error` is a contract status
# (producers/base.py:183) that metrics.py already counts in n while denying it EBSR credit.
import json

from runner.benchmark import _collect_meta, _merged_meta

# What write_env_packet leaves on disk for a failed produce (producers/base.py:273-293).
_PRODUCER_META = {
    "contract_version": 1, "producer": "claudecode-dockerfile", "status": "error",
    "note": "no Dockerfile.gen", "conformance": "native", "unreplayed": False, "inline": None,
    "base_image": "claude-runner-rust:latest", "head_sha": "abc123",
    "full_name": "o/r", "repo_url": "https://github.com/o/r", "language": "rust",
    "tokens_in": 368817, "tokens_out": 4023, "llm_calls": 20, "turns_used": 10,
    "produce_s": 75.2, "total_tokens": 372840, "cost_usd": 2.0325045,
}

# What _run_one has in hand on the error path (benchmark.py:168-170 + `ok`). Note the ABSENCE of any
# cost/economy field — that is why this fix must read the file rather than thread values through.
_OUT = {"status": "error", "failure_reason": "repo_error", "error": "no Dockerfile.gen",
        "root_path": "/tmp/x", "full_name": "o/r", "requested_model": "sonnet",
        "base_image": "wrong-image:latest", "head_sha": ""}


def _write(tmp_path, obj):
    p = tmp_path / "_meta.json"
    p.write_text(json.dumps(obj) if not isinstance(obj, str) else obj)
    return str(p)


def test_produce_failure_keeps_status_and_cost(tmp_path):
    # The whole point: `status` keeps the repo in metrics.py's denominator, and `cost_usd` keeps its
    # spend in total_cost_usd. Losing either is what made node-50's headline wrong.
    meta = _merged_meta(_write(tmp_path, _PRODUCER_META), _OUT, 100.0, 160.0)
    assert meta["status"] == "error"
    assert meta["cost_usd"] == 2.0325045
    assert meta["tokens_in"] == 368817 and meta["turns_used"] == 10
    assert meta["producer"] == "claudecode-dockerfile" and meta["language"] == "rust"


def test_merge_adds_the_runtime_keys_collect_meta_contributes(tmp_path):
    # The merge must still record what _collect_meta uniquely knows — wall-clock and host state.
    meta = _merged_meta(_write(tmp_path, _PRODUCER_META), _OUT, 100.0, 160.0)
    assert meta["start_ts"] == 100.0 and meta["end_ts"] == 160.0
    assert meta["duration_s"] == 60.0
    assert "pid" in meta and "free_disk_gb" in meta
    assert meta["failure_reason"] == "repo_error"


def test_producer_base_image_wins_on_collision(tmp_path):
    # base_image/head_sha appear in BOTH dicts. The producer's describe what actually ran — here a
    # Rust workbench — so a stale value from `out` must not overwrite them.
    meta = _merged_meta(_write(tmp_path, _PRODUCER_META), _OUT, 100.0, 160.0)
    assert meta["base_image"] == "claude-runner-rust:latest"
    assert meta["head_sha"] == "abc123"


def test_producer_failure_reason_is_not_overwritten_when_already_set(tmp_path):
    producer = dict(_PRODUCER_META, failure_reason="budget_exhausted")
    meta = _merged_meta(_write(tmp_path, producer), _OUT, 100.0, 160.0)
    assert meta["failure_reason"] == "budget_exhausted"


def test_missing_meta_file_falls_back_to_collect_meta(tmp_path):
    # Models that never call write_env_packet leave no file. Behaviour must be exactly as before.
    missing = str(tmp_path / "_meta.json")
    assert _merged_meta(missing, _OUT, 100.0, 160.0) == _collect_meta(_OUT, 100.0, 160.0)


def test_malformed_meta_file_falls_back_and_does_not_raise(tmp_path):
    # A truncated/corrupt file must not abort the run's bookkeeping.
    meta = _merged_meta(_write(tmp_path, "{not json"), _OUT, 100.0, 160.0)
    assert meta == _collect_meta(_OUT, 100.0, 160.0)        # i.e. the plain _collect_meta shape


def test_non_dict_meta_file_falls_back(tmp_path):
    # json.load succeeds on a bare list; .get would then raise. Guard on the TYPE, not on parseability.
    meta = _merged_meta(_write(tmp_path, ["not", "a", "dict"]), _OUT, 100.0, 160.0)
    assert meta == _collect_meta(_OUT, 100.0, 160.0)
