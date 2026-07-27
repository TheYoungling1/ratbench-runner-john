# producers/tests/test_claudecode_stream.py — summarize_stream (pure; no docker, no keys).
import json

from producers._claudecode_helpers import summarize_stream

# A realistic 2-turn stream: init -> assistant(tool_use) -> user(tool_result) -> assistant(text)
# -> result. Field shapes copied from a live `claude --output-format stream-json` capture.
_RESULT = {
    "type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
    "total_cost_usd": 0.0468775, "stop_reason": "end_turn", "session_id": "1bd8a54b-x",
    "usage": {"input_tokens": 2575, "output_tokens": 4,
              "cache_creation_input_tokens": 5284, "cache_read_input_tokens": 22745},
    "modelUsage": {"claude-sonnet-5": {"inputTokens": 2575, "outputTokens": 4,
                                       "costUSD": 0.0463125}},
}
_STREAM = "\n".join(json.dumps(o) for o in [
    {"type": "system", "subtype": "init", "session_id": "1bd8a54b-x", "cwd": "/testbed"},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "pip install -e ."}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": False, "content": "ok"}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Wrote the Dockerfile."}]}},
    _RESULT,
])


def test_extracts_cost_turns_and_stop_reason():
    s = summarize_stream(_STREAM)
    assert s["cost_usd"] == 0.0468775
    assert s["turns"] == 2
    assert s["is_error"] is False
    assert s["stop_reason"] == "end_turn"


def test_token_accounting_matches_ccdf_costs_definition():
    # tokens_in = input + cache_creation + cache_read (the definition ccdf_costs.py uses, so
    # ccdf numbers stay comparable with the earlier ccdf baselines).
    s = summarize_stream(_STREAM)
    assert s["tokens_in"] == 2575 + 5284 + 22745
    assert s["tokens_out"] == 4
    assert s["total_tokens"] == 2575 + 5284 + 22745 + 4


def test_counts_llm_calls_and_tool_calls():
    s = summarize_stream(_STREAM)
    assert s["llm_calls"] == 2        # two `assistant` events
    assert s["tool_calls"] == 1       # one tool_use block


def test_action_log_records_tool_use_and_text():
    s = summarize_stream(_STREAM)
    assert "Bash" in s["actions"]
    assert "pip install -e ." in s["actions"]
    assert "Wrote the Dockerfile." in s["actions"]


def test_per_model_usage_is_preserved():
    # Claude Code silently mixes models (a haiku for side tasks); a single rate card would
    # mis-price the run, so the per-model breakdown must survive.
    s = summarize_stream(_STREAM)
    assert s["model_usage"]["claude-sonnet-5"]["costUSD"] == 0.0463125


def test_truncated_stream_degrades_to_none_not_raise():
    # A budget-capped or timed-out run emits no `result` event. Everything the agent DID do
    # must still be recoverable, and no field may raise.
    truncated = _STREAM.split("\n")[0] + "\n{not json at all\n" + "\n".join(
        _STREAM.split("\n")[1:3])
    s = summarize_stream(truncated)
    assert s["cost_usd"] is None and s["turns"] is None and s["tokens_in"] is None
    assert s["tool_calls"] == 1       # the work it did before the wall is still counted


def test_empty_stream_is_safe():
    s = summarize_stream("")
    assert s["cost_usd"] is None and s["llm_calls"] == 0 and s["actions"] == ""


def test_rate_limit_event_is_flagged():
    stream = json.dumps({"type": "rate_limit_event",
                         "rate_limit_info": {"status": "rejected"}})
    assert summarize_stream(stream)["rate_limited"] is True


def test_allowed_rate_limit_event_is_not_flagged():
    stream = json.dumps({"type": "rate_limit_event",
                         "rate_limit_info": {"status": "allowed"}})
    assert summarize_stream(stream)["rate_limited"] is False
