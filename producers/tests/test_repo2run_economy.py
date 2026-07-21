# producers/tests/test_repo2run_economy.py — M4.5a: repo2run harvests its own token total +
# live pytest score into the ProducedEnv / _meta.json (the old runner/models wrapper is deleted
# in a later step, so the harvest must live in the producer now).
#
# The real repo2run tool is never invoked: the pure helpers are fed a synthetic output/<name>/,
# and the produce->_meta.json thread is driven by an injected runner stub.
import json
import os

from producers.base import ProduceContext, RepoSpec, write_env_packet
from producers.repo2run import (Repo2RunProducer, _harvest_inline, _harvest_total_tokens)


def _ctx(tmp_path):
    return ProduceContext(llm="deepseek/deepseek-v4-flash", workdir=str(tmp_path))


def _write(output_dir, name, payload):
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, name), "w") as f:
        json.dump(payload, f)


# ── pure harvest helpers (no live tool) ──────────────────────────────────────

def test_harvest_total_tokens_reads_last_cost_tokens(tmp_path):
    out = str(tmp_path / "output" / "o" / "r")
    # LAST entry with a cost_tokens key holds the running total (a later entry may lack it).
    _write(out, "track.json", [{"cost_tokens": 10}, {"cost_tokens": 12345}, {"note": "no tokens"}])
    assert _harvest_total_tokens(out) == 12345


def test_harvest_total_tokens_missing_file_is_none(tmp_path):
    # Anti-vanish: no track.json => None, never a raise.
    assert _harvest_total_tokens(str(tmp_path / "nope")) is None


def test_harvest_total_tokens_malformed_is_none(tmp_path):
    out = str(tmp_path / "output" / "o" / "r")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "track.json"), "w") as f:
        f.write("{ not json")
    assert _harvest_total_tokens(out) is None


def test_harvest_total_tokens_non_numeric_is_none(tmp_path):
    # A non-numeric cost_tokens must degrade to None — NOT be persisted as a bogus `tok` that
    # would suppress the agent_run_summary.json fallback (bool is an int subclass => also rejected).
    out = str(tmp_path / "output" / "o" / "r")
    _write(out, "track.json", [{"cost_tokens": "lots"}, {"cost_tokens": True}])
    assert _harvest_total_tokens(out) is None


def test_harvest_inline_pass_rate_and_success(tmp_path):
    out = str(tmp_path / "output" / "o" / "r")
    # eff = total - skipped = 10 - 2 = 8; passed/eff = 4/8 = 0.5; success => passed>0, failed/errors==0.
    _write(out, "run_pytest_results.json",
           {"summary": {"total_tests": 10, "passed": 4, "failed": 0, "errors": 0, "skipped": 2}})
    assert _harvest_inline(out) == {"command": "pytest", "success": True, "pass_rate": 0.5}


def test_harvest_inline_failed_is_not_success(tmp_path):
    out = str(tmp_path / "output" / "o" / "r")
    _write(out, "run_pytest_results.json",
           {"summary": {"total_tests": 10, "passed": 8, "failed": 2, "errors": 0, "skipped": 0}})
    r = _harvest_inline(out)
    assert r["success"] is False and r["pass_rate"] == 0.8


def test_harvest_inline_missing_file_is_none(tmp_path):
    assert _harvest_inline(str(tmp_path / "nope")) is None


# ── produce() threads economy + inline, and write_env_packet emits them ───────

def test_produce_threads_economy_and_inline_into_meta(tmp_path):
    inline = {"command": "pytest", "success": True, "pass_rate": 0.9}

    def _stub(repo, ctx, **kw):
        return {"dockerfile": "FROM x\nRUN mkdir /repo && cp -r /x/. /repo\nWORKDIR /repo",
                "base_image": "python:3.10", "head_sha": "abc",
                "economy": {"total_tokens": 12345}, "inline": inline}

    p = Repo2RunProducer(llm="deepseek/deepseek-v4-flash", runner=_stub)
    env = p.produce(RepoSpec("o/r", "https://github.com/o/r"), _ctx(tmp_path))

    assert env.status == "produced"
    assert env.economy["total_tokens"] == 12345
    assert env.inline == inline
    assert env.economy["produce_s"] is not None          # still set alongside the harvested total

    out_root = tmp_path / "packet"
    out_root.mkdir()
    repo_dir = write_env_packet(str(out_root), env)
    meta = json.load(open(os.path.join(repo_dir, "_meta.json")))
    assert meta["total_tokens"] == 12345
    assert meta["inline"] == inline
