"""Pure, RAT-tree-free helpers for the claudecode-dockerfile PRODUCER.

Folded out of the runner's claudecode model + dockerfile helpers so producers/ never
imports the runner package or RAT model modules (the produce -> measure seam only
crosses via bench.schema). Stdlib-only, so producers stay importable without the RAT tree.
"""
import json

W = "/testbed"
AUTH_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
DOCKERFILE_GEN_PATH = "/testbed/Dockerfile.gen"

# The Claude Code CLI accepts model aliases (sonnet/opus/haiku) or full IDs.
_CLAUDE_PREFIXES = ("claude", "sonnet", "opus", "haiku")


def _as_text(v) -> str:
    """Decode subprocess output that may be str (text mode), bytes, or None."""
    if v is None:
        return ""
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else v


def _normalize_model(llm: str) -> str:
    """Claude Code needs a Claude model. Fall back to 'sonnet' for non-Claude slugs
    (e.g. the runner's default deepseek/...), so `bench claudecode` works even if the
    variety llm wasn't overridden."""
    low = (llm or "").lower()
    if low.startswith(_CLAUDE_PREFIXES):
        return llm
    return "sonnet"


_PROMPT_TEMPLATE = (
    "You are configuring a Python repository at /testbed so its EXISTING test suite can "
    "run, and then writing a Dockerfile that reproduces your setup from scratch.\n\n"
    "First, install ALL Python dependencies and any required system packages so that "
    "pytest can collect and run the tests. Install into the SYSTEM Python using "
    "`sudo pip install ...` and `sudo apt-get install -y ...` for system libraries — do "
    "NOT create a virtualenv (the grader runs the system python3). You may edit "
    "configuration files. DO NOT modify, add, or delete any test files. DO NOT run the "
    "test suite yourself.\n\n"
    "Then write a self-contained Dockerfile to {gen_path} that reproduces this environment "
    "FROM A CLEAN BASE. It MUST:\n"
    "  - start `FROM {base}`;\n"
    "  - `RUN git clone https://github.com/{full_name} /testbed` and `WORKDIR /testbed` "
    "(do NOT rely on any files from this container — the build starts empty);\n"
    "  - install the SAME system packages and Python dependencies you installed, as RUN "
    "steps. The build runs as ROOT, so DROP every `sudo` prefix (use "
    "`apt-get install -y ...`, `pip install ...`);\n"
    "  - re-encode any edits you made to repo files as explicit RUN steps (e.g. "
    "`RUN sed -i ...` or a heredoc), since the clone is pristine;\n"
    "  - NOT run pytest or the test suite in the Dockerfile.\n"
    "When the Dockerfile is written, stop."
)


def build_prompt(full_name: str, base: str) -> str:
    """The agentic setup+emit prompt with the repo and emitted-base injected."""
    return _PROMPT_TEMPLATE.format(gen_path=DOCKERFILE_GEN_PATH, base=base, full_name=full_name)


def summarize_stream(stream_text: str) -> dict:
    """Parse claude `--output-format stream-json` (one JSON object per line) into the economy
    numbers plus a readable action log.

    Tolerant of partial/truncated streams by design: a budget-capped or timed-out run emits no
    final `result` event, but the `tool_use` events it did emit are the only surviving record of
    what the agent attempted. Malformed lines are skipped; every result-derived field degrades to
    None rather than raising.

    Token accounting deliberately matches /opt/runs/ccdf_costs.py — tokens_in = input +
    cache_creation + cache_read — so ccdf numbers stay comparable with the earlier ccdf baselines.
    Cache reads dominate Claude Code usage (22k cache-read vs 2.5k fresh input on a trivial
    prompt), so this is NOT comparable to the raw prompt-token counts the deepseek agents report;
    the component split stays available in the persisted claude_stream.jsonl.
    """
    actions: list = []
    info: dict = {"turns": None, "cost_usd": None, "is_error": None, "stop_reason": None,
                  "tokens_in": None, "tokens_out": None, "total_tokens": None,
                  "llm_calls": 0, "tool_calls": 0, "model_usage": {}, "rate_limited": False}
    for raw in (stream_text or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "system" and obj.get("subtype") == "init":
            actions.append(f"[init] session={str(obj.get('session_id', '?'))[:8]} "
                           f"cwd={obj.get('cwd', '?')}")
        elif kind == "assistant":
            info["llm_calls"] += 1
            for block in ((obj.get("message") or {}).get("content") or []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    info["tool_calls"] += 1
                    payload = json.dumps(block.get("input") or {})[:200]
                    actions.append(f"[{info['tool_calls']}] {block.get('name', '?')}: {payload}")
                elif block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        actions.append(f"    say: {text[:240]}")
        elif kind == "user":
            for block in ((obj.get("message") or {}).get("content") or []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tag = "ERR" if block.get("is_error") else "ok"
                    actions.append(f"      -> [{tag}] {str(block.get('content'))[:200]}")
        elif kind == "rate_limit_event":
            status = str((obj.get("rate_limit_info") or obj).get("status", ""))
            if status and status != "allowed":      # only flag ACTUAL throttling/rejection
                info["rate_limited"] = True
            actions.append(f"[rate-limit] status={status}")
        elif kind == "result":
            info["turns"] = obj.get("num_turns")
            info["cost_usd"] = obj.get("total_cost_usd")
            info["is_error"] = obj.get("is_error")
            info["stop_reason"] = obj.get("stop_reason")
            info["model_usage"] = obj.get("modelUsage") or {}
            usage = obj.get("usage")
            if isinstance(usage, dict):
                tin = ((usage.get("input_tokens") or 0)
                       + (usage.get("cache_creation_input_tokens") or 0)
                       + (usage.get("cache_read_input_tokens") or 0))
                tout = usage.get("output_tokens") or 0
                info["tokens_in"], info["tokens_out"] = tin, tout
                info["total_tokens"] = tin + tout
    info["actions"] = "\n".join(actions)
    return info
