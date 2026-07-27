# producers/tests/test_claudecode_stream.py — summarize_stream (pure; no docker, no keys).
import json

import pytest

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


# ── totality: the stream is untrusted input and the parser must never raise ────────────────
#
# `_persist_stream` calls summarize_stream OUTSIDE its try/except, and produce()'s guard turns
# any escaping exception into status="error" — i.e. a parse crash would DISCARD a Dockerfile the
# agent already spent real money producing. Truthiness checks are not enough: a truthy non-dict
# `message` sails past `or {}` and then raises on .get().

_MALFORMED_SHAPES = [
    {"type": "assistant", "message": "a bare string, not a dict"},
    {"type": "assistant", "message": {"content": 7}},
    {"type": "assistant", "message": {"content": "not a list"}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": 42}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "input": "not a dict"}]}},
    {"type": "assistant", "message": {"content": ["a bare string block"]}},
    {"type": "user", "message": True},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": 3.5}]}},
    {"type": "rate_limit_event", "rate_limit_info": "not a dict"},
    {"type": "result", "usage": "not a dict"},
    {"type": "result", "usage": {"input_tokens": "lots", "output_tokens": None}},
    {"type": "result", "modelUsage": ["not", "a", "dict"]},
    {"type": "result", "num_turns": None, "total_cost_usd": None},
]


@pytest.mark.parametrize("event", _MALFORMED_SHAPES)
def test_malformed_event_shapes_never_raise(event):
    summarize_stream(json.dumps(event))          # must not raise


def test_malformed_events_do_not_stop_later_parsing():
    # One corrupt event must not cost us the result event that carries cost/turns.
    stream = "\n".join([json.dumps({"type": "assistant", "message": "bare string"}),
                        json.dumps(_RESULT)])
    s = summarize_stream(stream)
    assert s["cost_usd"] == 0.0468775 and s["turns"] == 2


def test_non_numeric_usage_degrades_to_zero_not_crash():
    s = summarize_stream(json.dumps(
        {"type": "result", "usage": {"input_tokens": "lots", "output_tokens": 5,
                                     "cache_read_input_tokens": None}}))
    assert s["tokens_in"] == 0 and s["tokens_out"] == 5 and s["total_tokens"] == 5


def test_bool_usage_value_is_not_counted_as_one():
    # bool is an int subclass in Python; True must not silently become 1 token.
    s = summarize_stream(json.dumps({"type": "result", "usage": {"input_tokens": True}}))
    assert s["tokens_in"] == 0


# ── action-log readability: tool_result content is normally a LIST of blocks ───────────────

def test_tool_result_list_content_is_flattened_not_repr_dumped():
    # The realistic Claude Code shape. str() on the list would emit Python repr noise
    # ("[{'type': 'text', ...}]") into the trajectory log for nearly every real run.
    stream = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": False,
         "content": [{"type": "text", "text": "Successfully installed numpy-1.26.0"}]}]}})
    actions = summarize_stream(stream)["actions"]
    assert "Successfully installed numpy-1.26.0" in actions
    assert "'type'" not in actions and "[{" not in actions


def test_tool_result_error_is_tagged():
    stream = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": True,
         "content": [{"type": "text", "text": "command not found"}]}]}})
    assert "[ERR] command not found" in summarize_stream(stream)["actions"]


def test_bash_tool_input_renders_the_command_not_json():
    stream = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "apt-get install -y libpq-dev"}}]}})
    actions = summarize_stream(stream)["actions"]
    assert "[1] Bash: apt-get install -y libpq-dev" in actions


def test_unknown_tool_input_falls_back_to_json():
    stream = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Weird", "input": {"a": 1}}]}})
    assert '{"a": 1}' in summarize_stream(stream)["actions"]


def test_result_is_error_true_is_preserved():
    s = summarize_stream(json.dumps({"type": "result", "is_error": True}))
    assert s["is_error"] is True
