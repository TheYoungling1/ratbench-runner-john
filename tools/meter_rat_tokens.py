#!/usr/bin/env python3
"""Add per-repo LLM token metering to the RAT agent (libkit/codeagent.py).

RAT's ``libkit/llm.py::chat()`` returns ``(content, usage)`` where ``usage`` carries
prompt/completion/total tokens, but ``codeagent.py`` captures only ``usage.completion_tokens``
(the ~0.4% output fraction) and discards prompt/total. So a RAT run records no usable token
figure (``tool_stats.json`` tokens are a separate, always-0 shell-tool field). This patch:

  1. inits ``prompt_tokens_total`` / ``total_tokens_total`` accumulators next to ``cost_tokens``,
  2. guards the accumulation — ``chat()`` returns ``(None, None)`` on retry exhaustion, which the
     current ``cost_tokens += usage.completion_tokens`` would crash on (AttributeError) — and adds
     prompt + total,
  3. writes ``output/<full_name>/usage.json`` each turn:
     ``{prompt_tokens, completion_tokens, total_tokens, llm_turns}``.

Idempotent (no-op if already patched). Re-run after re-provisioning the RAT tree (codeagent.py
is a real file, not symlinked). Scoped to libkit/codeagent.py — RAT-only; does not touch the
DockerAgent path (which uses libkit/command + libkit/llm, not codeagent).

Usage: meter_rat_tokens.py [RAT_ROOT]      # default $RAT_ROOT or /opt/runanything/src
"""
import os
import sys

RAT_ROOT = (sys.argv[1] if len(sys.argv) > 1
            else os.environ.get("RAT_ROOT", "/opt/runanything/src"))
F = os.path.join(RAT_ROOT, "libkit", "codeagent.py")

EDITS = [
    # 1. init accumulators alongside cost_tokens
    ("        cost_tokens = 0\n",
     "        cost_tokens = 0\n"
     "        prompt_tokens_total = 0\n"
     "        total_tokens_total = 0\n"),
    # 2. guard usage (None on retry exhaustion) + accumulate prompt/total
    ("            cost_tokens += usage.completion_tokens\n",
     "            if usage is not None:\n"
     "                cost_tokens += usage.completion_tokens\n"
     "                prompt_tokens_total += usage.prompt_tokens\n"
     "                total_tokens_total += usage.total_tokens\n"),
    # 3. write usage.json each turn, right after inner_commands.json is written
    ("                w1.write(json.dumps(self.env.commands, indent=4))\n",
     "                w1.write(json.dumps(self.env.commands, indent=4))\n"
     "            with open(\n"
     "                f\"{self.root_dir}/output/{self.full_name}/usage.json\", \"w\"\n"
     "            ) as wu:\n"
     "                wu.write(json.dumps({\"prompt_tokens\": prompt_tokens_total,\n"
     "                                     \"completion_tokens\": cost_tokens,\n"
     "                                     \"total_tokens\": total_tokens_total,\n"
     "                                     \"llm_turns\": turn}, indent=2))\n"),
]


def main():
    if not os.path.isfile(F):
        sys.exit(f"ERROR: codeagent.py not found at {F} (set RAT_ROOT)")
    src = open(F).read()
    if "total_tokens_total" in src:
        print(f"already patched (idempotent no-op): {F}")
        return 0
    for i, (old, new) in enumerate(EDITS, 1):
        n = src.count(old)
        if n != 1:
            sys.exit(f"ERROR edit {i}: anchor found {n}x (expected 1) — aborting, no changes "
                     f"written.\n  anchor: {old!r}")
        src = src.replace(old, new, 1)
    open(F, "w").write(src)
    print(f"patched: {F}")
    print("  + prompt_tokens_total / total_tokens_total accumulators")
    print("  + None-usage guard (fixes retry-exhaustion AttributeError)")
    print("  + per-turn usage.json {prompt,completion,total,llm_turns}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
