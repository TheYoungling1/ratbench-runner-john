"""tools/ccdf_watch.py: failure classification and the credit-burst stop rule."""
import json

from tools.ccdf_watch import classify_failure, credit_burst, parse_done_lines


def _repo(tmp_path, meta, stream=""):
    d = tmp_path / "output" / "o" / "r"
    d.mkdir(parents=True)
    (d / "_meta.json").write_text(json.dumps(meta))
    (d / "claude_stream.jsonl").write_text(stream)
    return d


def test_credit_error_is_infra_and_retryable(tmp_path):
    d = _repo(tmp_path, {"status": "error", "llm_calls": 1},
              '{"type":"result","is_error":true,"api_error_status":402,'
              '"result":"API Error: 402 This request would exceed your available credits"}')
    assert classify_failure(d) == "credit"


def test_rate_limit_and_server_errors(tmp_path):
    d = _repo(tmp_path, {"status": "error", "llm_calls": 3},
              '{"type":"result","is_error":true,"api_error_status":429}')
    assert classify_failure(d) == "rate_limit"
    (d / "claude_stream.jsonl").write_text('{"type":"result","api_error_status":503}')
    assert classify_failure(d) == "api_5xx"


def test_agent_failures_are_not_infra(tmp_path):
    d = _repo(tmp_path, {"status": "error", "llm_calls": 100,
                         "note": "agent wrote no /testbed/Dockerfile.gen"})
    assert classify_failure(d) == "turn_cap"
    (d / "_meta.json").write_text(json.dumps({"status": "error", "llm_calls": 6,
                                              "dsml_text_blocks": 1}))
    assert classify_failure(d) == "dsml"


def test_credit_burst_needs_consecutive_credit_failures():
    assert credit_burst(["ok", "credit", "credit", "credit"], n=3)
    assert not credit_burst(["credit", "credit", "ok"], n=3)
    assert not credit_burst(["credit", "credit"], n=3)


def test_parse_done_lines():
    log = ("[scheduler/start ] a/b  log=x\n"
           "[scheduler/done  ] a/b  status=success\n"
           "[scheduler/done  ] c/d  status=error\n")
    assert parse_done_lines(log) == [("a/b", "success"), ("c/d", "error")]


def test_measuring_does_not_match_its_own_pgrep_shell():
    # No unified_bench runs under pytest; the pgrep's own `sh -c` must not count.
    from tools.ccdf_watch import measuring
    assert measuring() is False
