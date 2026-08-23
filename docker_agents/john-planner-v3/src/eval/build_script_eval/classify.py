"""Eval-layer SUPPLEMENT to coverage.py's failure classification. Never edits
coverage.py (hard reuse-by-import boundary) — this module only adds TOOL
patterns coverage misses (compiler/git/make) and a noise-robust "real" first
failure line, then a pure merge helper to combine with coverage's output.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_KNOWN_BUILD_TOOLS = ("gcc", "cc", "g++", "clang", "make", "cmake", "git", "pkg-config", "pkgconf")

_CMD_FAILED_RE = re.compile(r"error: command '([^']+)' failed", re.IGNORECASE)

_NO_SUCH_FILE_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _KNOWN_BUILD_TOOLS) + r")['\"]?\s*:\s*no such file or directory",
    re.IGNORECASE,
)

_GIT_ERROR_RE = re.compile(
    r"GIT_PYTHON_REFRESH|Bad git executable|git executable[^\n]*(?:not found|must be)",
    re.IGNORECASE,
)

_MAKE_ERROR_RE = re.compile(
    r"make: \*\*\*[^\n]*Error|make:[^\n]*command not found",
    re.IGNORECASE,
)

_NOISE_PREFIXES = ("[notice]", "[warning]")
_PIP_UPGRADE_NOISE = "pip install --upgrade pip"


def classify_tool_failures(output: str) -> tuple[dict, ...]:
    """TOOL gaps coverage.classify_execution_failures misses: compiler build
    failures (`error: command 'gcc' failed`), missing build tools (`gcc: No
    such file or directory`), broken git executables (GitPython refresh /
    "Bad git executable"), and `make` failures. Shaped like coverage's gap
    dicts; deduped by id (tier is always TOOL here)."""
    gaps: list[dict] = []
    seen: set[str] = set()

    def _add(tool_id: str, evidence: str) -> None:
        if tool_id not in seen:
            seen.add(tool_id)
            gaps.append({"tier": "TOOL", "id": tool_id, "evidence": evidence.strip()})

    for m in _CMD_FAILED_RE.finditer(output):
        _add(m.group(1), m.group(0))
    for m in _NO_SUCH_FILE_RE.finditer(output):
        _add(m.group(1).lower(), m.group(0))
    git_m = _GIT_ERROR_RE.search(output)
    if git_m:
        _add("git", git_m.group(0))
    make_m = _MAKE_ERROR_RE.search(output)
    if make_m:
        _add("make", make_m.group(0))
    return tuple(gaps)


def merge_gaps(base: tuple[dict, ...], extra: tuple[dict, ...]) -> tuple[dict, ...]:
    """Concatenate `base` then `extra`, deduped by (tier, id), base order first."""
    seen: set[tuple[str, str]] = set()
    merged: list[dict] = []
    for gap in (*base, *extra):
        key = (gap.get("tier"), gap.get("id"))
        if key not in seen:
            seen.add(key)
            merged.append(gap)
    return tuple(merged)


def _is_noise(line: str) -> bool:
    lowered = line.strip().lower()
    if lowered.startswith(_NOISE_PREFIXES):
        return True
    return _PIP_UPGRADE_NOISE in lowered


def real_first_failure(output: str, *, tail_lines: int = 40) -> dict:
    """Like coverage.first_failure_evidence but robust to pip upgrade-notice
    noise: prefers the last line actually containing `error:`, then the last
    bash-trace line, then the last non-noise line -- never a [notice]/
    [warning] line or a "pip install --upgrade pip" line."""
    lines = [line for line in output.splitlines() if line.strip()]
    tail = lines[-tail_lines:]
    candidates = [line for line in tail if not _is_noise(line)]

    command = next((line.strip() for line in reversed(candidates) if "error:" in line.lower()), None)
    if command is None:
        command = next(
            (line.strip()[2:].strip() for line in reversed(candidates) if line.strip().startswith("+ ")),
            None,
        )
    if command is None and candidates:
        command = candidates[-1].strip()
    return {"command": command, "stderr_tail": "\n".join(tail)}
