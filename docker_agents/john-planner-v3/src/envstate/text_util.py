"""Pure text helpers shared across envstate (no LLM/Docker imports).

Extracted so the strict-shell block runner (``script_runner``) can truncate
command output without importing ``build_agent`` (which pulls in the LLM-agent
stack: ``llm_response`` etc.).

The head/tail sizes and truncation shape mirror ``build_agent._truncate_output``
exactly so excerpts look identical across the agent loop and the block runner.
Phase 1 keeps the duplication intentionally; Phase 2 may re-point
``build_agent._truncate_output`` to delegate here.
"""
from __future__ import annotations

# Mirror build_agent._truncate_output: head keeps tracebacks/setup, tail keeps
# the pytest '=== N passed ===' summary line.
_HEAD = 1500
_TAIL = 800


def truncate_output(output: str, head: int = _HEAD, tail: int = _TAIL) -> str:
    """Head+tail truncation preserving the start and the tail (traceback/pytest summary)."""
    s = output or ""
    if len(s) <= head + tail:
        return s
    return (
        s[:head].rstrip()
        + "\n...[output truncated]...\n"
        + s[-tail:].lstrip()
    )
