"""TDD tests for src/eval/graph_fidelity/qualitative_judge.py.

Offline and deterministic per the loop doc's TDD guardrail: the LLM client is always a fake
(`_FakeClient` below) that mimics the OpenAI-compatible `client.chat.completions.create(...)`
shape the rest of the repo already uses (see `src/image_selector.py`,
`src/envstate/llm_response.py`) — no real API call is ever made.

Covers: clean parse -> correct JudgeResult; fenced/noisy output -> still parses; 3-judge
consensus merges findings + majority verdict; unparseable output -> safe fallback (no crash,
low-confidence "match").
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.eval.graph_fidelity.qualitative_judge import JudgeResult, judge


# ---------------------------------------------------------------------------
# Fake OpenAI-compatible client (mirrors the shape ImageSelector / complete_with_retry use)
# ---------------------------------------------------------------------------

class _FakeUsage:
    prompt_tokens = 10
    completion_tokens = 5
    total_tokens = 15


class _FakeMessage:
    def __init__(self, content: Optional[str]):
        self.content = content


class _FakeChoice:
    def __init__(self, content: Optional[str]):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: Optional[str]):
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage()


class _FakeCompletions:
    """Returns one canned response per call, in order; repeats the last if exhausted."""

    def __init__(self, contents: List[Optional[str]]):
        self._contents = list(contents)
        self.calls: List[dict] = []

    def create(self, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, **kwargs})
        if self._contents:
            content = self._contents.pop(0)
        else:
            content = None
        return _FakeResponse(content)


class _FakeChat:
    def __init__(self, contents: List[Optional[str]]):
        self.completions = _FakeCompletions(contents)


class _FakeClient:
    def __init__(self, contents: List[Optional[str]]):
        self.chat = _FakeChat(contents)


def _make_client(*contents: Optional[str]) -> _FakeClient:
    return _FakeClient(list(contents))


_ARGS = dict(
    repo_id="org__repo",
    recipe_text="FROM python:3.11\nRUN apt-get install -y libgl1\n",
    baseline_outcome={"rat": {"honest_pass": True}, "repo2run": {"honest_pass": False}},
    our_graph_summary={"nodes_by_tier": {"SYSTEM_LIB": [], "PACKAGE": ["opencv-python"]}},
    setup_sh_text="#!/bin/bash\npip install opencv-python\n",
    run_error_class="ImportError: libGL.so.1",
)


# ---------------------------------------------------------------------------
# Clean parse
# ---------------------------------------------------------------------------

class TestCleanParse:
    def test_clean_json_produces_correct_judge_result(self):
        raw = (
            '{"verdict": "minor_gaps", '
            '"missing_needs": [{"tier": "SYSTEM_LIB", "id": "libGL", "why": "opencv needs it", '
            '"would_cause_failure": true}], '
            '"spurious": [], "mis_tiered": [], "content_errors": [], '
            '"likely_failure_cause": "missing libgl1", "pass_by_luck": false, '
            '"confidence": 0.9}'
        )
        client = _make_client(raw)

        result = judge(**_ARGS, client=client)

        assert isinstance(result, JudgeResult)
        assert result.verdict == "minor_gaps"
        assert len(result.missing_needs) == 1
        assert result.missing_needs[0]["id"] == "libGL"
        assert result.missing_needs[0]["would_cause_failure"] is True
        assert result.spurious == ()
        assert result.mis_tiered == ()
        assert result.content_errors == ()
        assert result.likely_failure_cause == "missing libgl1"
        assert result.pass_by_luck is False
        assert result.confidence == 0.9
        assert result.n_judges == 1

    def test_default_model_and_single_call(self):
        client = _make_client('{"verdict": "match", "confidence": 1.0}')
        judge(**_ARGS, client=client)
        assert len(client.chat.completions.calls) == 1
        call = client.chat.completions.calls[0]
        assert call["model"] == "claude-haiku-4-5"
        # system + user messages, per the tight-prompt design
        assert call["messages"][0]["role"] == "system"
        assert call["messages"][1]["role"] == "user"
        assert _ARGS["repo_id"] in call["messages"][1]["content"]

    def test_custom_model_is_forwarded(self):
        client = _make_client('{"verdict": "match", "confidence": 1.0}')
        judge(**_ARGS, client=client, model="claude-sonnet-5")
        assert client.chat.completions.calls[0]["model"] == "claude-sonnet-5"


# ---------------------------------------------------------------------------
# Fenced / noisy output
# ---------------------------------------------------------------------------

class TestNoisyOutputStillParses:
    def test_code_fenced_json_parses(self):
        raw = (
            "Here is my analysis:\n"
            "```json\n"
            '{"verdict": "major_gaps", "missing_needs": [], "spurious": '
            '[{"tier": "PACKAGE", "id": "requests", "why": "not actually needed"}], '
            '"mis_tiered": [], "content_errors": [], '
            '"likely_failure_cause": "spurious dep", "pass_by_luck": false, '
            '"confidence": 0.75}\n'
            "```\n"
            "Let me know if you have questions."
        )
        client = _make_client(raw)

        result = judge(**_ARGS, client=client)

        assert result.verdict == "major_gaps"
        assert len(result.spurious) == 1
        assert result.spurious[0]["id"] == "requests"
        assert result.confidence == 0.75

    def test_leading_and_trailing_prose_parses(self):
        raw = (
            "Sure, I'll grade this now.\n"
            '{"verdict": "match", "confidence": 0.6, "pass_by_luck": true}\n'
            "That's my final answer."
        )
        client = _make_client(raw)

        result = judge(**_ARGS, client=client)

        assert result.verdict == "match"
        assert result.pass_by_luck is True
        assert result.confidence == 0.6


# ---------------------------------------------------------------------------
# 3-judge consensus
# ---------------------------------------------------------------------------

class TestConsensus:
    def test_majority_verdict_wins(self):
        # 2x minor_gaps, 1x major_gaps -> majority is minor_gaps
        r1 = '{"verdict": "minor_gaps", "confidence": 0.8}'
        r2 = '{"verdict": "minor_gaps", "confidence": 0.7}'
        r3 = '{"verdict": "major_gaps", "confidence": 0.9}'
        client = _make_client(r1, r2, r3)

        result = judge(**_ARGS, client=client, n_judges=3)

        assert result.verdict == "minor_gaps"
        assert result.n_judges == 3
        assert len(client.chat.completions.calls) == 3

    def test_tie_breaks_toward_more_severe_verdict(self):
        # 1x match, 1x major_gaps -> tie broken toward major_gaps (more cautious)
        r1 = '{"verdict": "match", "confidence": 0.5}'
        r2 = '{"verdict": "major_gaps", "confidence": 0.5}'
        client = _make_client(r1, r2)

        result = judge(**_ARGS, client=client, n_judges=2)

        assert result.verdict == "major_gaps"

    def test_findings_are_unioned_and_deduplicated_across_judges(self):
        r1 = (
            '{"verdict": "minor_gaps", "confidence": 0.9, '
            '"missing_needs": [{"tier": "SYSTEM_LIB", "id": "libGL", "why": "x", '
            '"would_cause_failure": true}]}'
        )
        r2 = (
            '{"verdict": "minor_gaps", "confidence": 0.85, '
            '"missing_needs": [{"tier": "SYSTEM_LIB", "id": "libGL", "why": "x", '
            '"would_cause_failure": true}, '
            '{"tier": "TOOL", "id": "pg_config", "why": "y", "would_cause_failure": false}]}'
        )
        r3 = '{"verdict": "minor_gaps", "confidence": 0.8, "missing_needs": []}'
        client = _make_client(r1, r2, r3)

        result = judge(**_ARGS, client=client, n_judges=3)

        # libGL appears in judges 1 and 2 (identical dict) -> deduped to one entry;
        # pg_config appears only in judge 2 -> included once. Total = 2, not 3.
        assert len(result.missing_needs) == 2
        ids = {f["id"] for f in result.missing_needs}
        assert ids == {"libGL", "pg_config"}

    def test_low_confidence_judge_findings_excluded_when_others_are_high_confidence(self):
        # A low-confidence judge claims a spurious node no one else sees; with high-confidence
        # judges present, the low-confidence judge's findings are excluded from the merge.
        r1 = '{"verdict": "match", "confidence": 0.95, "spurious": []}'
        r2 = '{"verdict": "match", "confidence": 0.9, "spurious": []}'
        r3 = (
            '{"verdict": "match", "confidence": 0.1, '
            '"spurious": [{"tier": "PACKAGE", "id": "hallucinated", "why": "noise"}]}'
        )
        client = _make_client(r1, r2, r3)

        result = judge(**_ARGS, client=client, n_judges=3)

        assert result.spurious == ()

    def test_pass_by_luck_majority_vote(self):
        r1 = '{"verdict": "match", "confidence": 0.8, "pass_by_luck": true}'
        r2 = '{"verdict": "match", "confidence": 0.8, "pass_by_luck": true}'
        r3 = '{"verdict": "match", "confidence": 0.8, "pass_by_luck": false}'
        client = _make_client(r1, r2, r3)

        result = judge(**_ARGS, client=client, n_judges=3)

        assert result.pass_by_luck is True

    def test_pass_by_luck_tie_leans_true(self):
        r1 = '{"verdict": "match", "confidence": 0.8, "pass_by_luck": true}'
        r2 = '{"verdict": "match", "confidence": 0.8, "pass_by_luck": false}'
        client = _make_client(r1, r2)

        result = judge(**_ARGS, client=client, n_judges=2)

        assert result.pass_by_luck is True

    def test_confidence_is_mean_across_judges(self):
        r1 = '{"verdict": "match", "confidence": 1.0}'
        r2 = '{"verdict": "match", "confidence": 0.5}'
        client = _make_client(r1, r2)

        result = judge(**_ARGS, client=client, n_judges=2)

        assert result.confidence == 0.75

    def test_likely_failure_cause_taken_from_highest_confidence_judge(self):
        r1 = '{"verdict": "match", "confidence": 0.2, "likely_failure_cause": "low-conf guess"}'
        r2 = '{"verdict": "match", "confidence": 0.95, "likely_failure_cause": "high-conf cause"}'
        client = _make_client(r1, r2)

        result = judge(**_ARGS, client=client, n_judges=2)

        assert result.likely_failure_cause == "high-conf cause"


# ---------------------------------------------------------------------------
# Unparseable output -> safe fallback
# ---------------------------------------------------------------------------

class TestUnparseableFallback:
    def test_garbage_text_does_not_crash_and_returns_low_confidence_match(self):
        client = _make_client("I cannot comply with this request in JSON form, sorry!")

        result = judge(**_ARGS, client=client)

        assert result.verdict == "match"
        assert result.confidence == 0.0
        assert result.notes != ()

    def test_empty_response_does_not_crash(self):
        client = _make_client(None)

        result = judge(**_ARGS, client=client)

        assert result.verdict == "match"
        assert result.confidence == 0.0

    def test_transport_exception_does_not_propagate(self):
        class _ExplodingCompletions:
            def create(self, *args, **kwargs):
                raise RuntimeError("simulated network failure")

        class _ExplodingChat:
            def __init__(self):
                self.completions = _ExplodingCompletions()

        class _ExplodingClient:
            def __init__(self):
                self.chat = _ExplodingChat()

        result = judge(**_ARGS, client=_ExplodingClient())

        assert result.verdict == "match"
        assert result.confidence == 0.0

    def test_invalid_verdict_string_downgrades_to_match(self):
        client = _make_client('{"verdict": "totally_broken", "confidence": 0.5}')

        result = judge(**_ARGS, client=client)

        assert result.verdict == "match"

    def test_non_dict_top_level_json_falls_back_safely(self):
        client = _make_client('["not", "an", "object"]')

        result = judge(**_ARGS, client=client)

        assert result.verdict == "match"
        assert result.confidence == 0.0
