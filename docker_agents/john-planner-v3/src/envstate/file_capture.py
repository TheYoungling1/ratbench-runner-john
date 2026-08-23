"""Fix 1 — capture achieved file state instead of replaying edit commands.

The synthesizer historically rebuilt edited files by re-running the agent's edit
*commands* (``sed -i``, ``printf > f`` …) in trajectory order. That replays
superseded edits, corrupts config files, and loses any edit whose recording was
truncated (a single-line heredoc opener). This module captures, for every file the
agent demonstrably edited, the file's FINAL bytes from the achieved container state
and lets the synthesizer emit ONE deterministic write per file.

The class is pure given a ``read_file(path) -> str | None`` callable, so it is fully
mockable without a Docker daemon.
"""
from __future__ import annotations

import logging
import re
import shlex
from typing import Callable, Dict, List, Optional, Tuple

from src.envstate.ledger import ActionLedger
from src.envstate.synthesis import (
    _RE_FILE_WRITE,
    _RE_PYTHON_C_WRITE,
    _RE_SED_I,
    _RE_WRITE_PREFIX,
    _PYTEST_RE,
    _READONLY_PREFIXES,
    _is_package_install_command,
    _is_source_file_edit,
    _is_unterminated_heredoc,
    _strip_python_package_installs_from_compound,
)

logger = logging.getLogger(__name__)

# Skip files larger than this (bytes). Keeps the image layer small and avoids
# baking giant fixtures. Edited config/source files are far below this.
DEFAULT_MAX_FILE_BYTES = 256 * 1024

# Single-redirect target: `>`/`>>` to a real file (never `2>`, `&>`, `/dev/null`).
_RE_REDIRECT_TARGET = re.compile(r"(?<![0-9&])>>?\s*(?!&)(?!/dev/null\b)([^\s;&|<>]+)")
# open('<path>', ...) inside a python -c write.
_RE_PY_OPEN_PATH = re.compile(r"open\s*\(\s*['\"]([^'\"]+)['\"]")
# Replacement-character / null-byte heuristics for binary detection.
_REPLACEMENT_CHAR = "�"


def _command_edits_file(cmd: str) -> bool:
    """Whether ``cmd`` is a file-writing edit whose target we should capture.

    Mirrors ``synthesis._is_source_file_edit`` but DELIBERATELY does not exclude a
    truncated single-line heredoc opener: the heredoc body DID execute in-sandbox, so
    the file exists in the achieved state — we capture its real content even though the
    recorded command is unreplayable.
    """
    if not cmd:
        return False
    s = cmd.strip()
    if _PYTEST_RE.match(s):
        return False
    if s.startswith(_READONLY_PREFIXES):
        return False
    if _RE_WRITE_PREFIX.match(s) and _RE_FILE_WRITE.search(s):
        return True
    if _RE_SED_I.match(s):
        return True
    if _RE_PYTHON_C_WRITE.match(s):
        return True
    return False


def _paths_from_sed(cmd: str) -> List[str]:
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return []
    tokens = tokens[1:]  # drop "sed"
    has_explicit_script = any(t in ("-e", "-f", "--expression", "--file") for t in tokens)
    files: List[str] = []
    consumed_script = False
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-"):
            if tok in ("-e", "-f", "--expression", "--file"):
                skip_next = True
            continue
        if not has_explicit_script and not consumed_script:
            consumed_script = True  # the first bareword is the inline sed script
            continue
        files.append(tok)
    return files


def extract_paths_from_command(cmd: str) -> List[str]:
    """Ordered file targets written/edited by a single edit command (else [])."""
    if not _command_edits_file(cmd):
        return []
    s = cmd.strip()
    if _RE_SED_I.match(s):
        return _paths_from_sed(s)
    if _RE_PYTHON_C_WRITE.match(s):
        return [m.group(1) for m in _RE_PY_OPEN_PATH.finditer(s)]
    # printf/echo/cat/tee redirect: the LAST redirect target is the effective file.
    targets = [m.group(1) for m in _RE_REDIRECT_TARGET.finditer(s)]
    if targets:
        return [_unquote(targets[-1])]
    return []


def _unquote(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
        return tok[1:-1]
    return tok


def extract_edited_file_paths(ledger: ActionLedger) -> List[str]:
    """Ordered, de-duplicated paths of files edited by rc==0 commands."""
    seen = set()
    ordered: List[str] = []
    for event in ledger.events():
        if event.rc != 0:
            continue
        for path in extract_paths_from_command(event.cmd):
            if path and path not in seen:
                seen.add(path)
                ordered.append(path)
    return ordered


def is_probably_binary(content: str) -> bool:
    """Heuristic: NUL bytes, or a non-trivial run of UTF-8 replacement chars."""
    if not content:
        return False
    if "\x00" in content:
        return True
    replacements = content.count(_REPLACEMENT_CHAR)
    return replacements >= 8 or replacements > len(content) * 0.05


class FileStateCapturer:
    """Return ``{path: final_content}`` for every edited file present in final state."""

    def __init__(
        self,
        read_file: Callable[[str], Optional[str]],
        *,
        max_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self._read_file = read_file
        self._max_bytes = max_bytes

    def capture(self, ledger: ActionLedger) -> Dict[str, str]:
        captured: Dict[str, str] = {}
        for path in extract_edited_file_paths(ledger):
            content = self._read_file(path)
            if content is None:
                # edited then deleted / unreadable / oversize — only capture final
                # state. The caller (build_ordered_recipe_ops) replays the original
                # edit command for any path missing from the returned dict, so the
                # edit is never silently lost (read-failure log lives in the reader).
                continue
            # Byte length under the lossless surrogateescape codec (see
            # add_file_write_instruction); a few odd bytes never inflate the count.
            if len(content.encode("utf-8", "surrogateescape")) > self._max_bytes:
                logger.warning("[file-capture] miss (oversize), will replay edit: %s", path)
                continue
            if is_probably_binary(content):
                logger.warning("[file-capture] miss (binary), will replay edit: %s", path)
                continue
            captured[path] = content
        return captured


# Op tuples: ("run", command) or ("file", path, content).
Op = Tuple


def build_ordered_recipe_ops(
    ledger: ActionLedger,
    captured: Dict[str, str],
    *,
    distill: Optional[Callable[[str], List[str]]] = None,
) -> List[Op]:
    """Interleave captured file writes with the irreducible commands in trajectory order.

    Each captured file's single FINAL-content write is placed at the trajectory
    position of that file's LAST edit (Fix 1 ordering), so an irreducible command that
    ran AFTER the edit sees the final content, while commands before the first edit keep
    their position. Package installs are dropped (the pinned closure is authoritative,
    Fix 2). An edited file that is NOT in ``captured`` (read failure / oversize / binary)
    has its original edit command(s) REPLAYED instead of dropped — the HIGH-2 safety net
    that keeps behavior strictly >= the pre-capture replay path.

    Returns a list of ops: ``("run", command)`` and ``("file", path, content)``.
    """
    events = ledger.events()

    # Last trajectory index at which each CAPTURED file was edited.
    last_edit_index: Dict[str, int] = {}
    for i, event in enumerate(events):
        if event.rc != 0:
            continue
        for path in extract_paths_from_command(event.cmd):
            if path in captured:
                last_edit_index[path] = i

    writes_at: Dict[int, List[str]] = {}
    for path in captured:  # preserve first-appearance order among same-index writes
        idx = last_edit_index.get(path)
        if idx is not None:
            writes_at.setdefault(idx, []).append(path)

    ops: List[Op] = []
    # Defensive: a captured file with no detected edit index can't shadow a build
    # step — emit it up front so it is present before any consuming command.
    for path in captured:
        if path not in last_edit_index:
            ops.append(("file", path, captured[path]))

    def _emit_run(cmd: str) -> None:
        produced = distill(cmd) if distill is not None else [cmd]
        for unit in produced:
            unit = _strip_python_package_installs_from_compound(unit)
            if unit:
                ops.append(("run", unit))

    for i, event in enumerate(events):
        if event.rc != 0:
            continue
        cmd = event.cmd or ""
        edit_paths = extract_paths_from_command(cmd)
        is_edit = bool(edit_paths) or _is_source_file_edit(cmd)
        if is_edit:
            captured_all = bool(edit_paths) and all(p in captured for p in edit_paths)
            if captured_all:
                pass  # written separately at its last-edit position
            elif _is_unterminated_heredoc(cmd):
                # Truncated heredoc opener: unreplayable AND not captured -> nothing we
                # can safely emit (logged at capture). Pre-capture behavior also dropped
                # it, so this is not a regression.
                logger.warning("[file-capture] miss (unreplayable heredoc): %s", cmd[:120])
            else:
                _emit_run(cmd)  # HIGH-2 fallback: replay the original edit command
        elif _is_unterminated_heredoc(cmd):
            pass  # body-less heredoc-open with no file target — drop (no-op recipe guard)
        elif not event.mutation_class:
            pass  # read-only / test command
        elif _is_package_install_command(cmd):
            pass  # closure is the single source of truth (Fix 2)
        else:
            _emit_run(cmd)  # irreducible (apt / build step / other mutation)

        for path in writes_at.get(i, ()):
            ops.append(("file", path, captured[path]))

    return ops
