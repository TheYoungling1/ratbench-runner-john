# tools/tests/test_backfill_meta.py — recovery of clobbered produce-failure packets.
#
# This tool REWRITES experimental records, so the risk is not that it under-recovers but that it
# writes something plausible and wrong. These tests pin the three ways that could happen: touching
# an intact packet, asserting status="error" over a run that actually succeeded, and inventing
# fields (note/produce_s) that are genuinely unrecoverable.
import json
import os

from tools.backfill_meta import backfill_run, plan_one

_INTACT = {
    "contract_version": 1, "producer": "claudecode-dockerfile", "status": "produced",
    "note": "", "conformance": "native", "unreplayed": False, "inline": None,
    "base_image": "claude-runner:latest", "head_sha": "abc", "full_name": "o/good",
    "repo_url": "https://github.com/o/good", "language": "nodejs",
    "tokens_in": 10, "tokens_out": 2, "llm_calls": 3, "turns_used": 4,
    "produce_s": 5.0, "total_tokens": 12, "cost_usd": 0.5,
}
# What the pre-fix _run_one left behind: _collect_meta's 9 keys, no status, no cost.
_CLOBBERED = {
    "pid": 123, "start_ts": 100.0, "end_ts": 160.0, "duration_s": 60.0,
    "failure_reason": "repo_error", "requested_model": "sonnet",
    "base_image": "claude-runner:latest", "head_sha": "", "free_disk_gb": 50.0,
}
_STREAM = "\n".join(json.dumps(o) for o in [
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                   "input": {"command": "ls"}}]}},
    {"type": "result", "subtype": "error_max_budget_usd", "total_cost_usd": 2.0325,
     "num_turns": 56, "usage": {"input_tokens": 100, "cache_creation_input_tokens": 200,
                                "cache_read_input_tokens": 700, "output_tokens": 40}},
])


def _mkrun(tmp_path, packets):
    """packets: {repo: (meta_dict|None, stream_text|None, has_produced_marker)}"""
    run = tmp_path / "run"
    for repo, (meta, stream, marker) in packets.items():
        d = run / "output" / repo.split("/")[0] / repo.split("/")[1]
        d.mkdir(parents=True)
        if meta is not None:
            (d / "_meta.json").write_text(json.dumps(meta))
        if stream is not None:
            (d / "claude_stream.jsonl").write_text(stream)
        if marker:
            (d / "run_produced.json").write_text("{}")
    return str(run)


def test_recovers_status_and_cost_from_the_stream(tmp_path):
    run = _mkrun(tmp_path, {"o/good": (_INTACT, None, True),
                            "o/bad": (dict(_CLOBBERED), _STREAM, False)})
    changed, money = backfill_run(run, apply=True)
    assert changed == 1 and round(money, 4) == 2.0325
    m = json.loads((tmp_path / "run/output/o/bad/_meta.json").read_text())
    assert m["status"] == "error"            # back in metrics.py's denominator
    assert m["cost_usd"] == 2.0325           # back in total_cost_usd
    assert m["turns_used"] == 56
    assert m["tokens_in"] == 1000            # input + cache_creation + cache_read
    assert m["tokens_out"] == 40


def test_keeps_the_surviving_runtime_keys(tmp_path):
    run = _mkrun(tmp_path, {"o/good": (_INTACT, None, True),
                            "o/bad": (dict(_CLOBBERED), _STREAM, False)})
    backfill_run(run, apply=True)
    m = json.loads((tmp_path / "run/output/o/bad/_meta.json").read_text())
    assert m["duration_s"] == 60.0 and m["pid"] == 123
    assert m["failure_reason"] == "repo_error"


def test_inherits_run_constants_from_an_intact_sibling(tmp_path):
    run = _mkrun(tmp_path, {"o/good": (_INTACT, None, True),
                            "o/bad": (dict(_CLOBBERED), _STREAM, False)})
    backfill_run(run, apply=True)
    m = json.loads((tmp_path / "run/output/o/bad/_meta.json").read_text())
    assert m["language"] == "nodejs"          # NOT the "python" default harvest would have used
    assert m["producer"] == "claudecode-dockerfile" and m["conformance"] == "native"


def test_does_not_invent_unrecoverable_fields(tmp_path):
    """note and produce_s cannot be reconstructed; a plausible value here would be fabrication."""
    run = _mkrun(tmp_path, {"o/good": (_INTACT, None, True),
                            "o/bad": (dict(_CLOBBERED), _STREAM, False)})
    backfill_run(run, apply=True)
    m = json.loads((tmp_path / "run/output/o/bad/_meta.json").read_text())
    assert m["note"] is None and m["produce_s"] is None
    assert m["backfilled"].startswith("tools/backfill_meta.py")


def test_leaves_intact_packets_byte_identical(tmp_path):
    run = _mkrun(tmp_path, {"o/good": (_INTACT, None, True),
                            "o/bad": (dict(_CLOBBERED), _STREAM, False)})
    p = tmp_path / "run/output/o/good/_meta.json"
    before = p.read_text()
    backfill_run(run, apply=True)
    assert p.read_text() == before


def test_refuses_a_statusless_packet_that_has_a_success_marker(tmp_path, capsys):
    """That combination is a different bug; asserting status="error" over it could turn a
    successful, paid-for run into a counted failure."""
    run = _mkrun(tmp_path, {"o/good": (_INTACT, None, True),
                            "o/weird": (dict(_CLOBBERED), _STREAM, True)})
    changed, _ = backfill_run(run, apply=True)
    assert changed == 0
    assert "SKIPPED" in capsys.readouterr().out
    assert "status" not in json.loads((tmp_path / "run/output/o/weird/_meta.json").read_text())


def test_dry_run_writes_nothing(tmp_path):
    run = _mkrun(tmp_path, {"o/good": (_INTACT, None, True),
                            "o/bad": (dict(_CLOBBERED), _STREAM, False)})
    p = tmp_path / "run/output/o/bad/_meta.json"
    before = p.read_text()
    changed, money = backfill_run(run, apply=False)
    assert changed == 1 and round(money, 4) == 2.0325     # reports what it WOULD do
    assert p.read_text() == before                        # ...and does not do it


def test_missing_stream_still_restores_the_denominator(tmp_path):
    """A repo that died before writing a stream must still rejoin n, just with null economy —
    that is strictly better than being dropped."""
    run = _mkrun(tmp_path, {"o/good": (_INTACT, None, True),
                            "o/bad": (dict(_CLOBBERED), None, False)})
    backfill_run(run, apply=True)
    m = json.loads((tmp_path / "run/output/o/bad/_meta.json").read_text())
    assert m["status"] == "error" and m["cost_usd"] is None


def test_mixed_language_run_refuses_to_guess(tmp_path):
    import pytest
    other = dict(_INTACT, full_name="o/two", language="rust")
    run = _mkrun(tmp_path, {"o/good": (_INTACT, None, True), "o/two": (other, None, True),
                            "o/bad": (dict(_CLOBBERED), _STREAM, False)})
    with pytest.raises(SystemExit, match="disagree on"):
        backfill_run(run, apply=True)


def test_plan_one_returns_none_for_an_intact_packet(tmp_path):
    d = tmp_path / "o" / "good"
    d.mkdir(parents=True)
    (d / "_meta.json").write_text(json.dumps(_INTACT))
    assert plan_one(str(d), {}) is None


def test_apply_leaves_no_tmp_file(tmp_path):
    run = _mkrun(tmp_path, {"o/good": (_INTACT, None, True),
                            "o/bad": (dict(_CLOBBERED), _STREAM, False)})
    backfill_run(run, apply=True)
    assert not os.path.exists(str(tmp_path / "run/output/o/bad/_meta.json.tmp"))
