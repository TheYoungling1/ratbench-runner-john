"""DeepSeek pricing for the Claude Code lanes.

The numbers below are not invented: the usage blocks are what api.deepseek.com/anthropic actually
returned on 2026-09-04, and the rates are the vendor's off-peak table. This is a money path with a
31x cache spread and a 2x peak swing, so the arithmetic gets a test even though it is short.
"""
import datetime as dt
import json

from producers._claudecode_helpers import (
    deepseek_cost, is_deepseek, is_peak, settled_cost, summarize_stream,
)

OFF = dt.datetime(2026, 9, 4, 12, 0, tzinfo=dt.timezone.utc)     # Friday noon
PEAK = dt.datetime(2026, 9, 4, 2, 0, tzinfo=dt.timezone.utc)     # Friday 02:00

# MEASURED: the same 14k-token prefix sent twice. `input_tokens` excludes cache reads.
COLD = {"input_tokens": 14514, "cache_read_input_tokens": 0, "output_tokens": 16}
WARM = {"input_tokens": 50, "cache_read_input_tokens": 14464, "output_tokens": 16}


def test_peak_window_matches_the_vendor_table():
    # "01:00 - 04:00 and 06:00 - 10:00 UTC, Monday through Friday"
    for hour, peak in [(0, False), (1, True), (3, True), (4, False),
                       (5, False), (6, True), (9, True), (10, False)]:
        assert is_peak(dt.datetime(2026, 9, 4, hour, tzinfo=dt.timezone.utc)) is peak, hour
    assert is_peak(dt.datetime(2026, 9, 5, 2, tzinfo=dt.timezone.utc)) is False   # Saturday


def test_cache_hits_dominate_the_input_cost():
    cold = deepseek_cost("claude-sonnet-5", COLD["input_tokens"], 0, 16, when=OFF)
    warm = deepseek_cost("claude-sonnet-5", WARM["input_tokens"],
                         WARM["cache_read_input_tokens"], 16, when=OFF)
    assert cold == 14514 * 2.2e-7 + 16 * 6.6e-7
    # Same tokens, 31x cheaper input. Pricing every token as a miss is the expensive mistake.
    assert cold / warm > 20


def test_peak_doubles_and_opus_prices_as_pro():
    assert deepseek_cost("claude-sonnet-5", 14514, 0, 16, when=PEAK) == \
        2 * deepseek_cost("claude-sonnet-5", 14514, 0, 16, when=OFF)
    assert deepseek_cost("claude-opus-4-1", 1000, 0, 0, when=OFF) == 1000 * 6.6e-7


def test_missing_tokens_price_as_none_never_zero():
    assert deepseek_cost("claude-sonnet-5", None, 0, 16) is None
    assert deepseek_cost("claude-sonnet-5", 10, 0, None) is None
    assert settled_cost({}, "claude-sonnet-5", "") == (None, None)


def test_a_capped_run_still_prices_and_discards_the_cli_figure():
    # A capped run has no `result` event. Input comes from the per-response usage; output comes
    # from the message_delta that --include-partial-messages emits, since `assistant` events
    # always report output_tokens: 0 (measured against the live bridge).
    stream = "\n".join([
        json.dumps({"type": "assistant", "message": {
            "id": "m1", "content": [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": 2575, "cache_creation_input_tokens": 5284,
                      "cache_read_input_tokens": 22745, "output_tokens": 0}}}),
        json.dumps({"type": "stream_event", "event": {
            "type": "message_delta", "delta": {"stop_reason": "end_turn"},
            "usage": {"input_tokens": 2575, "output_tokens": 120}}}),
    ])
    info = summarize_stream(stream)
    assert info["input_miss_tokens"] == 2575 + 5284 and info["cache_read_tokens"] == 22745
    assert info["tokens_out"] == 120 and info["usage_partial"] is False
    cost, source = settled_cost(info, "claude-sonnet-5", "https://api.deepseek.com/anthropic")
    assert source == "computed"
    # The CLI would have called this same turn $0.0469 off the Anthropic rate card.
    assert 0 < cost < 0.0469 / 10


def test_a_capped_run_without_the_deltas_declines_to_price():
    """The partial-messages flag is what makes a capped run priceable; without it, output is
    unknown and the row must say so rather than sum the zeros."""
    stream = json.dumps({"type": "assistant", "message": {
        "id": "m1", "content": [{"type": "text", "text": "x"}],
        "usage": {"input_tokens": 2575, "cache_read_input_tokens": 22745, "output_tokens": 0}}})
    info = summarize_stream(stream)
    assert info["tokens_in"] == 2575 + 22745 and info["tokens_out"] is None
    assert settled_cost(info, "claude-sonnet-5", "https://api.deepseek.com/anthropic") == \
        (None, "partial")


def test_off_deepseek_the_cli_figure_stands_and_is_labelled():
    assert is_deepseek("https://api.deepseek.com/anthropic")
    assert not is_deepseek("https://api.anthropic.com") and not is_deepseek("")
    info = summarize_stream(json.dumps(
        {"type": "result", "total_cost_usd": 0.0469, "num_turns": 1,
         "usage": {"input_tokens": 2575, "output_tokens": 120}}))
    assert settled_cost(info, "claude-sonnet-5", "https://api.anthropic.com") == (0.0469, "cli")


# ── leaked DeepSeek markup ───────────────────────────────────────────────────────────────────
# The bytes are the real ones that broke the SWE-agent arm (see
# producers/tests/test_sweagent_repo2run.py::_DSML_RESPONSE). On this lane the tool protocol is
# typed, so a leak cannot break parsing — it just costs a turn on a command that never ran, which
# is invisible without a count.
_DSML = "I'll look at the repo.\n\n<｜｜DSML｜｜bash>\nls -la /repo\n</｜｜DSML｜｜bash>"


def _assistant(text, mid="m1"):
    return json.dumps({"type": "assistant", "message": {
        "id": mid, "content": [{"type": "text", "text": text}]}})


def test_leaked_dsml_is_counted_and_tagged_in_the_trajectory():
    info = summarize_stream(_assistant(_DSML))
    assert info["dsml_text_blocks"] == 1
    assert "say[DSML]:" in info["actions"]          # greppable in claude_actions.log


def test_clean_prose_counts_zero_and_is_not_tagged():
    info = summarize_stream(_assistant("I'll install pytest and freezegun."))
    assert info["dsml_text_blocks"] == 0
    assert "[DSML]" not in info["actions"]


def test_the_count_is_per_block_across_a_whole_stream():
    stream = "\n".join([_assistant(_DSML, "m1"), _assistant("fine", "m2"),
                        _assistant(_DSML, "m3")])
    assert summarize_stream(stream)["dsml_text_blocks"] == 2


# ── a truncated stream knows what it does not know ───────────────────────────────────────────
# Only the final `result` event reports output tokens; per-assistant usage always says 0. A capped
# or walled run has no result event, so output is UNKNOWN. Calling it zero would price the run
# 25-30% light, because output is $0.66/1M against ~98%-cached input at $0.007/1M.

def _capped_stream():
    return json.dumps({"type": "assistant", "message": {
        "id": "m1", "content": [{"type": "text", "text": "working"}],
        "usage": {"input_tokens": 647, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 32128, "output_tokens": 0}}})


def test_a_capped_run_reports_input_but_not_invented_output():
    info = summarize_stream(_capped_stream())
    assert info["tokens_in"] == 647 + 32128        # input IS recoverable
    assert info["tokens_out"] is None              # output is not — never 0
    assert info["total_tokens"] is None
    assert info["usage_partial"] is True


def test_a_partial_run_declines_to_price_and_says_why():
    info = summarize_stream(_capped_stream())
    assert settled_cost(info, "claude-sonnet-5", "https://api.deepseek.com/anthropic") == \
        (None, "partial")


def test_a_complete_run_is_not_flagged_partial():
    info = summarize_stream(_capped_stream() + "\n" + json.dumps(
        {"type": "result", "num_turns": 1, "total_cost_usd": 0.3,
         "usage": {"input_tokens": 647, "cache_read_input_tokens": 32128, "output_tokens": 5852}}))
    assert info["usage_partial"] is False and info["tokens_out"] == 5852
    cost, src = settled_cost(info, "claude-sonnet-5", "https://api.deepseek.com/anthropic")
    assert src == "computed" and cost > 0
