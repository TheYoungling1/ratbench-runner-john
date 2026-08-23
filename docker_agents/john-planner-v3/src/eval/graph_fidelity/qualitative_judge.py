"""Qualitative LLM-judge: the semantic lens over graph fidelity.

Spec: docs/superpowers/loops/2026-07-02-graph-fidelity-eval-loop.md §1 ("Numbers alone
mislead"), §3 item 7, §7 ("Numbers are not the whole truth" / "No metric-gaming").

The honest `pass_rate` (scripts.compute_essr) is a coarse binary: a repo can pass by luck
(the base image pre-ships a need our graph never declared) or fail on one trivial gap while
the graph is 90% right — the number tells you neither. This module reads the held-out recipe
(what a working setup does) next to OUR graph + rendered setup.sh and reports where they
diverge, via a cheap model (default Haiku).

This is a GRADER, not the graph BUILDER — the minimal-LLM rule (§7) binds construction, not
measurement, so an LLM here is allowed. Its output is diagnostic ONLY: it must never change
the honest `pass_rate` computed by `scripts.compute_essr`.

Reuses the repo's existing OpenAI-compatible LLM plumbing (the same one `env_classifier.py`
and `base_image_selection.py` use): `complete_with_retry` for the retrying transport call and
`extract_json_object` for tolerant JSON parsing (fenced / noisy output). No new HTTP client.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.envstate.jsonutil import extract_json_object
from src.envstate.llm_response import complete_with_retry

# Cheapest current Claude model — see docs/superpowers (claude-api skill, cached 2026-06-24).
# Callers may pass a different `model=` (e.g. Sonnet) for harder repos; this is only the
# default, matching the loop doc's "cheap (Haiku, or Sonnet for hard repos)" guidance.
DEFAULT_MODEL = "claude-haiku-4-5"

_VERDICTS = ("match", "minor_gaps", "major_gaps")
_SEVERITY = {"match": 0, "minor_gaps": 1, "major_gaps": 2}

# A judge's per-finding-list contribution is only trusted into the consensus union when its
# own reported confidence clears this bar — keeps one low-confidence hallucinated judge from
# polluting the merged findings of an otherwise-agreeing panel.
_HIGH_CONFIDENCE_THRESHOLD = 0.5

_SYSTEM_PROMPT = (
    "You are a strict grader comparing a WORKING reference environment-setup recipe for a "
    "repo against an AUTOMATED graph + build script for the SAME repo. Name ONLY where they "
    "diverge in ways that would affect install/test correctness — do not restate what "
    "matches, and do not treat semantically-equivalent choices as divergences (e.g. apt "
    "libgl1 vs libgl1-mesa-glx naming the same soname; a pin that resolves to the same "
    "version; harmless reordering).\n\n"
    "Respond with ONLY one JSON object (no prose, no code fence) with this exact shape:\n"
    "{\n"
    '  "verdict": "match" | "minor_gaps" | "major_gaps",\n'
    '  "missing_needs": [{"tier": str, "id": str, "why": str, "would_cause_failure": bool}],\n'
    '  "spurious": [{"tier": str, "id": str, "why": str}],\n'
    '  "mis_tiered": [{"id": str, "our_tier": str, "correct_tier": str}],\n'
    '  "content_errors": [{"id": str, "ours": str, "correct": str}],\n'
    '  "likely_failure_cause": str,\n'
    '  "pass_by_luck": bool,\n'
    '  "confidence": number\n'
    "}\n\n"
    "Set pass_by_luck=true when the run would only succeed because the base image "
    "pre-ships a need our graph never declared (it would fail on a leaner base image). "
    "confidence is your own calibrated confidence in this verdict, 0.0-1.0."
)

_MAX_RECIPE_CHARS = 8_000
_MAX_SCRIPT_CHARS = 6_000


@dataclass(frozen=True)
class JudgeResult:
    """One judge verdict (or the deterministic consensus of several)."""

    verdict: str
    missing_needs: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    spurious: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    mis_tiered: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    content_errors: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    likely_failure_cause: str = ""
    pass_by_luck: bool = False
    confidence: float = 0.0
    n_judges: int = 1
    notes: Tuple[str, ...] = field(default_factory=tuple)


def _truncate(text: Optional[str], limit: int) -> str:
    if not text:
        return "(none)"
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({len(text) - limit} more characters truncated)"


def _build_messages(
    repo_id: str,
    recipe_text: str,
    baseline_outcome: Dict[str, Any],
    our_graph_summary: Dict[str, Any],
    setup_sh_text: str,
    run_error_class: Optional[str],
) -> List[Dict[str, str]]:
    """Compose the tight judge prompt. Pure and deterministic given identical inputs."""
    user_content = (
        f"### Repo\n{repo_id}\n\n"
        f"### Held-out reference recipe (what a working setup does)\n"
        f"{_truncate(recipe_text, _MAX_RECIPE_CHARS)}\n\n"
        f"### Baseline outcome (did honest peers pass this repo)\n"
        f"{baseline_outcome!r}\n\n"
        f"### Our graph node set (by tier)\n"
        f"{our_graph_summary!r}\n\n"
        f"### Our rendered setup.sh (first pass, no repair)\n"
        f"{_truncate(setup_sh_text, _MAX_SCRIPT_CHARS)}\n\n"
        f"### First-pass run error class\n"
        f"{run_error_class if run_error_class else '(none — install/test succeeded)'}\n"
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _coerce_finding_list(raw: Any) -> Tuple[Dict[str, Any], ...]:
    """Keep only dict entries from a claimed list; anything else is dropped, not fatal."""
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, dict))


def _parse_judge_response(text: Optional[str]) -> JudgeResult:
    """Robustly parse one judge's raw text into a JudgeResult.

    Tolerates code fences and leading/trailing prose (via extract_json_object). On
    unparseable output, returns a low-confidence "match" rather than raising — a judge
    is diagnostic; it must never crash the eval harness (§7).
    """
    obj = extract_json_object(text)
    if obj is None:
        return JudgeResult(
            verdict="match",
            confidence=0.0,
            notes=("unparseable judge output — treated as no-signal, not a finding",),
        )

    verdict = obj.get("verdict")
    if verdict not in _VERDICTS:
        verdict = "match"

    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    likely_failure_cause = obj.get("likely_failure_cause")
    if not isinstance(likely_failure_cause, str):
        likely_failure_cause = ""

    return JudgeResult(
        verdict=verdict,
        missing_needs=_coerce_finding_list(obj.get("missing_needs")),
        spurious=_coerce_finding_list(obj.get("spurious")),
        mis_tiered=_coerce_finding_list(obj.get("mis_tiered")),
        content_errors=_coerce_finding_list(obj.get("content_errors")),
        likely_failure_cause=likely_failure_cause,
        pass_by_luck=bool(obj.get("pass_by_luck", False)),
        confidence=confidence,
    )


def _dedupe(findings: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, Any], ...]:
    """Stable-order de-duplication by full-content identity (dicts aren't hashable)."""
    seen: List[Dict[str, Any]] = []
    for finding in findings:
        if finding not in seen:
            seen.append(finding)
    return tuple(seen)


def _majority_verdict(verdicts: Sequence[str]) -> str:
    """Majority vote; ties broken toward the more severe verdict (adversarial-verify bias —
    §4c: a class the number can't see should surface, not get voted away)."""
    counts: Dict[str, int] = {}
    for v in verdicts:
        counts[v] = counts.get(v, 0) + 1
    best_count = max(counts.values())
    tied = [v for v, c in counts.items() if c == best_count]
    return max(tied, key=lambda v: _SEVERITY[v])


def _consensus(results: Sequence[JudgeResult]) -> JudgeResult:
    """Deterministic merge of 1+ judge results (loop doc §3 item 7 / §4c).

    - verdict / pass_by_luck: majority vote, ties broken toward the more severe/cautious
      reading (adversarial-verify).
    - findings: union of de-duplicated findings from judges whose OWN confidence clears
      _HIGH_CONFIDENCE_THRESHOLD (a single low-confidence outlier can't pollute the merge).
    - confidence: mean across all judges.
    - likely_failure_cause: the highest-confidence judge's non-empty statement.
    """
    if len(results) == 1:
        return results[0]

    verdict = _majority_verdict([r.verdict for r in results])

    pass_by_luck_votes = sum(1 for r in results if r.pass_by_luck)
    # Tie (even panel split) leans True — a missed pass-by-luck flag is the costlier error.
    pass_by_luck = pass_by_luck_votes * 2 >= len(results)

    trusted = [r for r in results if r.confidence >= _HIGH_CONFIDENCE_THRESHOLD]
    merge_source = trusted if trusted else results

    missing_needs = _dedupe([f for r in merge_source for f in r.missing_needs])
    spurious = _dedupe([f for r in merge_source for f in r.spurious])
    mis_tiered = _dedupe([f for r in merge_source for f in r.mis_tiered])
    content_errors = _dedupe([f for r in merge_source for f in r.content_errors])

    ranked_by_confidence = sorted(results, key=lambda r: r.confidence, reverse=True)
    likely_failure_cause = ""
    for r in ranked_by_confidence:
        if r.likely_failure_cause:
            likely_failure_cause = r.likely_failure_cause
            break

    confidence = statistics.mean(r.confidence for r in results)

    return JudgeResult(
        verdict=verdict,
        missing_needs=missing_needs,
        spurious=spurious,
        mis_tiered=mis_tiered,
        content_errors=content_errors,
        likely_failure_cause=likely_failure_cause,
        pass_by_luck=pass_by_luck,
        confidence=confidence,
        n_judges=len(results),
    )


def judge(
    repo_id: str,
    recipe_text: str,
    baseline_outcome: Dict[str, Any],
    our_graph_summary: Dict[str, Any],
    setup_sh_text: str,
    run_error_class: Optional[str],
    *,
    client: Any,
    model: str = DEFAULT_MODEL,
    n_judges: int = 1,
) -> JudgeResult:
    """Grade OUR graph + setup.sh against the held-out recipe for one repo.

    `client` is the repo's existing OpenAI-compatible LLM client (the one
    `image_selector.ImageSelector` / `env_classifier.make_construction_classifier` use) —
    exposing ``client.chat.completions.create(model=..., messages=..., **kwargs)``.

    Dispatches `n_judges` (>=1) independent calls with the SAME prompt and merges them via
    `_consensus` — a deterministic function of the parsed per-judge results, so repeated runs
    against the same canned/mocked responses are reproducible. Never raises: a transport
    failure or unparseable response degrades to a low-confidence "match", never a crash (this
    is a diagnostic grader, not the honest pass_rate authority — §7).
    """
    messages = _build_messages(
        repo_id, recipe_text, baseline_outcome, our_graph_summary, setup_sh_text,
        run_error_class,
    )
    n = max(1, n_judges)

    results = []
    for _ in range(n):
        try:
            text, _usage, _response = complete_with_retry(
                client, model, messages, max_attempts=1, temperature=0,
            )
        except Exception as exc:  # noqa: BLE001 — grader must never crash the eval harness
            results.append(
                JudgeResult(
                    verdict="match",
                    confidence=0.0,
                    notes=(f"judge call failed: {exc}",),
                )
            )
            continue
        results.append(_parse_judge_response(text))

    return _consensus(results)
