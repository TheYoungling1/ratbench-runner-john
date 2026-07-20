#!/usr/bin/env python3
"""Failure attribution for the RAT bench runner: did the synthesizer drop a working env?

Every DockerAgent repo produces two independent success signals that the runner stores
side by side under output/<owner>/<repo>/:

  * AGENT in-sandbox outcome  — saved adapter result `<instance>.json`:
      build_success, test_success, logs.verified_test_command(s), logs.verification_source
  * EVAL replay outcome       — _result_row.json + run_pytest_results.json:
      pytest_executed, parse_method ("build_failed" => synthesized Dockerfile did not build)

Classifying the GAP between them attributes each repo to one of four buckets:

  A_agent_build_failure  agent never built a working env in its own sandbox (build_success=False)
  B_weak_verification    agent "built" but only ran collect-only / never verified real tests, AND
                         the eval replay also failed — we cannot credit a synthesizer failure
                         because the agent never proved tests pass in-sandbox (the collect-only gate)
  C_synthesizer_failure  agent built AND verified REAL tests in-sandbox, but the eval replay did NOT
                         reproduce a runnable env — the synthesizer failed to capture the recipe
  D_reproduced_success   the eval replay built the env AND ran tests (the synthesized Dockerfile works)

`weak_verification` is a per-repo flag: True for bucket B, and also for D rows where the env
reproduced despite the agent only collect-only'ing (hollow-ish success).
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

AGENT_BUILD_FAILURE = "A_agent_build_failure"
WEAK_VERIFICATION = "B_weak_verification"
SYNTHESIZER_FAILURE = "C_synthesizer_failure"
REPRODUCED_SUCCESS = "D_reproduced_success"
# Only fires when there is no agent in-sandbox signal to attribute with (e.g. the rat/repo2run
# baselines never emit the DockerAgent <instance>.json). Keeps baselines from faking A/weak-D.
UNATTRIBUTABLE = "U_unattributable_no_agent_signal"
BUCKETS = [AGENT_BUILD_FAILURE, WEAK_VERIFICATION, SYNTHESIZER_FAILURE, REPRODUCED_SUCCESS]
ALL_BUCKETS = BUCKETS + [UNATTRIBUTABLE]

LEGEND = {
    AGENT_BUILD_FAILURE: "agent never built a working env in-sandbox",
    WEAK_VERIFICATION: "agent built but only collect-only / no real test verify; eval also failed",
    SYNTHESIZER_FAILURE: "agent built + verified real tests in-sandbox, but eval replay did not reproduce it",
    REPRODUCED_SUCCESS: "eval replay built the env and ran tests (synthesized Dockerfile works)",
    UNATTRIBUTABLE: "eval did not reproduce AND no agent in-sandbox signal (e.g. baselines) — cannot attribute",
}


def is_collect_only(cmd: Optional[str]) -> bool:
    """A verified test command that only collects tests, never runs them."""
    return bool(cmd) and "--collect-only" in cmd


def classify(build_success: bool, test_success: bool, verified_cmd: Optional[str],
             executed: bool, parse_method: Optional[str],
             agent_signal_missing: bool = False) -> Dict[str, Any]:
    """Attribute a single repo to a bucket from its agent + eval signals.

    eval_ebsr (eval replay built the env AND ran tests) mirrors compute_essr.py's EBSR:
    pytest_executed AND parse_method != "build_failed".

    With no agent in-sandbox signal (`agent_signal_missing`) we cannot tell agent from
    synthesizer fault: a reproduced repo is still D (but never flagged weak), and a
    non-reproduced repo is UNATTRIBUTABLE rather than a faked agent build failure.
    """
    eval_ebsr = bool(executed) and parse_method != "build_failed"
    agent_built = bool(build_success)
    agent_verified_real = bool(test_success) and not is_collect_only(verified_cmd)

    if eval_ebsr:
        # Reproduced. Flag "weak" only when we actually have signals showing weak verification.
        weak = (not agent_verified_real) and not agent_signal_missing
        return {"bucket": REPRODUCED_SUCCESS, "weak_verification": weak}
    if agent_signal_missing:
        return {"bucket": UNATTRIBUTABLE, "weak_verification": False}
    if agent_built and agent_verified_real:
        return {"bucket": SYNTHESIZER_FAILURE, "weak_verification": False}
    if agent_built:
        return {"bucket": WEAK_VERIFICATION, "weak_verification": True}
    return {"bucket": AGENT_BUILD_FAILURE, "weak_verification": False}


def read_agent_signals(repo_dir: str, full_name: str) -> Dict[str, Any]:
    """Read the saved adapter result `<instance>.json` for the agent's in-sandbox signals.

    Degrades gracefully: a missing/unreadable file yields build/test False + a
    `agent_signal_missing` flag so the caller can report it separately.
    """
    instance_id = full_name.replace("/", "__")
    path = os.path.join(repo_dir, f"{instance_id}.json")
    missing = {
        "build_success": False, "test_success": False, "verified_test_command": None,
        "verification_source": None, "dropped_commands": [], "agent_signal_missing": True,
    }
    if not os.path.exists(path):
        return missing
    try:
        d = json.load(open(path))
    except (json.JSONDecodeError, OSError):
        return missing
    logs = d.get("logs") or {}
    dropped = list(logs.get("filtered_test_commands") or []) + list(logs.get("dropped_broad_test_commands") or [])
    return {
        "build_success": bool(d.get("build_success")),
        "test_success": bool(d.get("test_success")),
        "verified_test_command": logs.get("verified_test_command"),
        "verification_source": logs.get("verification_source"),
        "dropped_commands": dropped,
        "agent_signal_missing": False,
    }


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-repo attribution into bucket counts + the actionable repo lists."""
    counts = {b: 0 for b in ALL_BUCKETS}
    synth, weak_reproduced = [], []
    missing = 0
    for r in rows:
        b = r.get("attribution")
        if b in counts:
            counts[b] += 1
        if b == SYNTHESIZER_FAILURE:
            synth.append(r.get("full_name"))
        if b == REPRODUCED_SUCCESS and r.get("weak_verification"):
            weak_reproduced.append(r.get("full_name"))
        if r.get("agent_signal_missing"):
            missing += 1
    return {
        "counts": counts,
        "total": len(rows),
        "synthesizer_failures": synth,
        "weak_verification_reproduced": weak_reproduced,
        "agent_signal_missing": missing,
        "legend": LEGEND,
    }
