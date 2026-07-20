"""Optional raw-LLM-response logging for the EnvState supervisor path.

All writes are opt-in and exception-proof: if the log path is not configured
(or anything goes wrong), the function silently returns without affecting the
caller.

Usage
-----
Set ``ENVSTATE_LLM_LOG`` to a file path before starting the run, or pass
``log_path`` directly.  Both the supervisor, worker, and maintainer call this
after each LLM round-trip.
"""
from __future__ import annotations

import json
import os
from typing import Any


def log_llm_exchange(
    role: str,
    response: Any,
    parsed: Any = None,
    *,
    messages: Any = None,
    log_path: str | None = None,
) -> None:
    """Append one JSON line describing an LLM round-trip to *log_path*.

    Parameters
    ----------
    role:
        Logical role of the caller (``"supervisor"``, ``"worker"``,
        ``"maintainer"``).
    response:
        The raw OpenAI-compatible chat-completion object.
    parsed:
        Optional structured result derived from the response (task_spec dict,
        action dict, proposal summary, …).  Will be ``repr``-truncated to
        ~500 chars.
    messages:
        Optional INPUT prompt sent to the model — the list of
        ``{"role", "content"}`` dicts (system + user + any accumulated ReAct
        turns).  Recorded VERBATIM (untruncated) so the exact information the
        agent was given can be inspected offline.  ``None`` → the ``prompt``
        field is omitted (back-compat: existing callers that log only the
        response are unaffected).
    log_path:
        Explicit file path.  If ``None``, the value of the environment variable
        ``ENVSTATE_LLM_LOG`` is used.  If that is also ``None`` / empty, the
        function is a no-op.
    """
    try:
        _log_llm_exchange(role, response, parsed, messages=messages, log_path=log_path)
    except Exception:
        # Logging must never break a run.
        pass


def _log_llm_exchange(
    role: str,
    response: Any,
    parsed: Any,
    *,
    messages: Any = None,
    log_path: str | None,
) -> None:
    """Inner (non-exception-proof) implementation."""
    resolved = log_path if log_path is not None else os.environ.get("ENVSTATE_LLM_LOG")
    if not resolved:
        return

    # Extract fields from the response, tolerating any shape.
    raw_content: str | None = None
    raw_reasoning: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int | None] = {"prompt": None, "completion": None, "total": None}

    try:
        message = response.choices[0].message
        raw_content = getattr(message, "content", None)
        # reasoning: direct attr or model_extra
        raw_reasoning = getattr(message, "reasoning", None)
        if raw_reasoning is None:
            extra = getattr(message, "model_extra", None) or {}
            raw_reasoning = extra.get("reasoning")
    except (AttributeError, IndexError, TypeError, KeyError):
        pass

    try:
        finish_reason = response.choices[0].finish_reason
    except (AttributeError, IndexError, TypeError, KeyError):
        pass

    try:
        usage_obj = response.usage
        usage = {
            "prompt": getattr(usage_obj, "prompt_tokens", None),
            "completion": getattr(usage_obj, "completion_tokens", None),
            "total": getattr(usage_obj, "total_tokens", None),
        }
    except (AttributeError, TypeError):
        pass

    # Represent `parsed` as a short string (≤ ~500 chars).
    parsed_repr: str | None
    if parsed is None:
        parsed_repr = None
    else:
        r = repr(parsed)
        if len(r) > 500:
            r = r[:497] + "..."
        parsed_repr = r

    record = {
        "role": role,
        "raw_content": raw_content,
        "raw_reasoning": raw_reasoning,
        "finish_reason": finish_reason,
        "usage": usage,
        "parsed": parsed_repr,
    }
    # The exact input prompt the agent was given (system + user + ReAct turns),
    # recorded verbatim for offline inspection. Coerced to a plain list of
    # {role, content} so json.dumps never fails on an exotic message object.
    if messages is not None:
        try:
            record["prompt"] = [
                {"role": m.get("role"), "content": m.get("content")}
                if isinstance(m, dict) else {"role": None, "content": str(m)}
                for m in messages
            ]
        except TypeError:
            record["prompt"] = repr(messages)

    os.makedirs(os.path.dirname(os.path.abspath(resolved)), exist_ok=True)
    with open(resolved, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
