"""Provider action-class taxonomy (design §10): a provider command must match the
shell action class declared by its `kind`. Pure: no Docker/network/LLM."""
from __future__ import annotations

import re

# kind -> regex the provider command must match (searched against the stripped command).
ACTION_CLASSES: dict[str, str] = {
    "apt":   r"^(?:apt-get|apt)(?:\s+update\s*&&\s*(?:apt-get|apt))?\s+install\b",
    "pip":   r"^(python3?\s+-m\s+)?pip\d?\s+install\b",
    "npm":   r"^npm\s+(install|ci)\b",
    "shell": r".",
}


def matches_action_class(kind: str, command: str) -> bool:
    pattern = ACTION_CLASSES.get(kind)
    if pattern is None:
        return False
    return re.search(pattern, (command or "").strip()) is not None
