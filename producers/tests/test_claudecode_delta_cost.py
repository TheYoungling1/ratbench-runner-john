"""OpenRouter's settled cost arrives on `message_delta` stream events in current CLI versions.

Seen live 2026-11-10 (deepseek/deepseek-v4-pro-0813 through the thinking proxy): every response's
`usage.cost` rode on `stream_event.event.usage`, none on the `assistant` message, so the run
recorded cost_usd=None for a model with no fallback rate card.
"""
import json

from producers._claudecode_helpers import settled_cost, summarize_stream


def _delta(cost):
    return json.dumps({"type": "stream_event", "event": {
        "type": "message_delta", "usage": {"output_tokens": 5, "cost": cost}}})


def _assistant(mid, cost=None):
    usage = {"input_tokens": 10, "output_tokens": 0}
    if cost is not None:
        usage["cost"] = cost
    return json.dumps({"type": "assistant", "message": {"id": mid, "usage": usage, "content": []}})


def test_cost_on_message_delta_is_summed():
    stream = "\n".join([_assistant("a"), _delta(0.25), _assistant("b"), _delta(0.5)])
    info = summarize_stream(stream)
    assert info["provider_cost"] == 0.75
    assert settled_cost(info, "deepseek/deepseek-v4-pro-0813", "https://openrouter.ai") == (
        0.75, "provider")


def test_cost_reported_in_both_places_is_not_double_counted():
    stream = "\n".join([_assistant("a", 0.25), _delta(0.25)])
    assert summarize_stream(stream)["provider_cost"] == 0.25
