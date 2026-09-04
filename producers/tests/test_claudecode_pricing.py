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
    # No `result` event: the per-response usage is all there is.
    stream = json.dumps({"type": "assistant", "message": {
        "id": "m1", "content": [{"type": "text", "text": "x"}],
        "usage": {"input_tokens": 2575, "cache_creation_input_tokens": 5284,
                  "cache_read_input_tokens": 22745, "output_tokens": 120}}})
    info = summarize_stream(stream)
    assert info["input_miss_tokens"] == 2575 + 5284 and info["cache_read_tokens"] == 22745
    cost, source = settled_cost(info, "claude-sonnet-5", "https://api.deepseek.com/anthropic")
    assert source == "computed"
    # The CLI would have called this same turn $0.0469 off the Anthropic rate card.
    assert 0 < cost < 0.0469 / 10


def test_off_deepseek_the_cli_figure_stands_and_is_labelled():
    assert is_deepseek("https://api.deepseek.com/anthropic")
    assert not is_deepseek("https://api.anthropic.com") and not is_deepseek("")
    info = summarize_stream(json.dumps(
        {"type": "result", "total_cost_usd": 0.0469, "num_turns": 1,
         "usage": {"input_tokens": 2575, "output_tokens": 120}}))
    assert settled_cost(info, "claude-sonnet-5", "https://api.anthropic.com") == (0.0469, "cli")
