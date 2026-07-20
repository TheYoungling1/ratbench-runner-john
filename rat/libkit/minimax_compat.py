"""MiniMax reasoning-model output compatibility for RAT's bash-block parser.

MiniMax-M2.x emits <think>...</think> reasoning plus its native
<minimax:tool_call><invoke name="TOOL">args</invoke></minimax:tool_call> format.
RAT's extract_commands() expects ```bash fenced blocks and recognises special
tools by name (ToolDispatcher.is_tool_command, startswith). This normalises a
MiniMax reply into that format so the agent loop can parse the intended action.

Ordinary replies (already using ```bash fences, e.g. deepseek) pass through
unchanged. Only the FIRST <invoke> is converted, honouring RAT's one-action-per-round.
"""
import re

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_TOOLCALL = re.compile(r"<minimax:tool_call>.*?</minimax:tool_call>", re.DOTALL)
_INVOKE = re.compile(r'<invoke\s+name="([^"]+)"\s*>(.*?)</invoke>', re.DOTALL)


def normalize_minimax(content):
    if not content or ("<minimax:tool_call>" not in content and "<think>" not in content):
        return content
    text = _THINK.sub("", content)
    if "<minimax:tool_call>" in text:
        m = _INVOKE.search(text)
        if m:
            tool = m.group(1).strip()
            args = re.sub(r"\s+", " ", (m.group(2) or "").strip())
            cmd = (tool + " " + args).strip()
            text = _TOOLCALL.sub("\n```bash\n" + cmd + "\n```\n", text, count=1)
        text = text.replace("<minimax:tool_call>", "").replace("</minimax:tool_call>", "")
    return text
