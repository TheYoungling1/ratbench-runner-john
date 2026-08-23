"""Normalized fingerprint of a VERIFY_TEST_CMD (pytest) run's OUTCOME.

Pure, table-free, string-in/string-out — importable without any src.envstate
dependency. Two runs with the SAME set of failing tests (or the same non-pytest
crash) fingerprint identically; a run whose failing set changes — a test starts
passing, a new test fails, a different exception is raised — fingerprints
differently. Volatile tokens (durations, addresses, tmp paths, line numbers,
xdist worker tags) are stripped so RE-running the identical failing suite is
stable across repair cycles.

Powers run_v3's no-progress giveup (design: residual-giveup-fix.md): give up
when the test gate shows the IDENTICAL failure for NO_PROGRESS_CYCLES consecutive
cycles despite repair activity.
"""
from __future__ import annotations

import hashlib
import re

# pytest short-test-summary lines: "FAILED path::test - ExcClass: msg" / "ERROR path::test".
# Capture kind + node id + (optional) exception class; DROP the variable message.
_SUMMARY_LINE_RE = re.compile(
    r"^(FAILED|ERROR)\s+(\S+?)(?:\s+-\s+([A-Za-z_][\w.]*))?\s*$",
    re.MULTILINE,
)
# The final "=== N failed, M passed, K error[s] in T s ===" band (pytest -q always prints it).
_SUMMARY_BAND_RE = re.compile(
    r"^=+.*\b(?:failed|passed|error|errors|no tests ran)\b.*=+$",
    re.MULTILINE,
)
_COUNT_RE = re.compile(
    r"\b(\d+)\s+(failed|passed|errors?|skipped|xfailed|xpassed|deselected|warnings?)\b"
)

# Volatile tokens stripped from the FALLBACK tail so an identical crash is stable.
_VOLATILE_SUBS = (
    (re.compile(r"\x1b\[[0-9;]*m"), ""),                 # ANSI colour
    (re.compile(r"0x[0-9a-fA-F]+"), "0xADDR"),           # hex addresses
    (re.compile(r"\b\d+(?:\.\d+)?s\b"), "Ns"),           # durations "3.42s" / "12s"
    (re.compile(r"(?:/private)?/tmp/\S+"), "/tmp/PATH"),  # pytest tmp dirs
    (re.compile(r"\bline\s+\d+\b"), "line N"),           # traceback line numbers
    (re.compile(r"\[gw\d+\]"), "[gw]"),                  # xdist worker tags
    (re.compile(r"\bpid:?\s*\d+\b", re.IGNORECASE), "pid N"),
)


def _digest(payload: str) -> str:
    return hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()[:16]


def outcome_signature(passed: bool, out: str) -> str:
    """Stable fingerprint of one VERIFY_TEST_CMD run.

    ``passed`` is the VERIFIED gate result (``done_gate._verified_test_run_passed``),
    NOT the raw pytest rc — so a hollow pass (zero tests collected, all skipped)
    signs as a FAILURE and can still trip the no-progress detector. A verified
    pass always signs as the sentinel ``"pass"`` (never equal to any failure
    signature). Two failing runs sign identically iff their normalized failing
    node-id set, exception classes, and outcome counts match.
    """
    if passed:
        return "pass"
    text = out or ""
    fails = sorted(
        f"{kind} {nid} {exc or ''}".rstrip()
        for kind, nid, exc in _SUMMARY_LINE_RE.findall(text)
    )
    band = "\n".join(_SUMMARY_BAND_RE.findall(text))
    counts = sorted(f"{n} {word}" for n, word in _COUNT_RE.findall(band))
    if fails or counts:
        return "fail:" + _digest("\n".join(fails) + "\x00" + "\n".join(counts))
    # No pytest summary at all (collection crash, install error, segfault,
    # non-pytest command): fall back to a volatility-normalized tail so an
    # identical crash is still stable cycle-to-cycle.
    tail = "\n".join(ln for ln in text.splitlines() if ln.strip())[-2000:]
    for rx, repl in _VOLATILE_SUBS:
        tail = rx.sub(repl, tail)
    return "fail:" + _digest(tail)


def next_stall(prev_sig: str | None, sig: str, stall: int) -> int:
    """Advance the consecutive-identical-failing-signature counter.

    A verified pass (``sig == "pass"``) or a CHANGED failing signature resets the
    run's momentum; an unchanged failing signature extends the stall. Returns the
    new stall length (number of consecutive cycles sharing ``sig``).

        pass                       -> 0        (suite green / progress possible)
        first sight of a failure   -> 1
        same failure as last cycle -> stall + 1
    """
    if sig == "pass":
        return 0
    if prev_sig is not None and sig == prev_sig:
        return stall + 1
    return 1
