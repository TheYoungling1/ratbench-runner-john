"""Stage 2 install-failure localization + reciped-only certify + debug-bundle assembly.

Pure / read-only except certify_reciped_only, which delegates state writes to the host
certify pass. No Docker/LLM imports at module level.
"""
from __future__ import annotations

from dataclasses import dataclass

from python_deps.depgraph.emit import _is_reciped
from python_deps.depgraph.schema import State
from src.envstate.depgraph_live import certify_refresh

_EVIDENCE_CAP = 500
_WINDOW = 3  # annotated lines kept above/below the failing line


@dataclass(frozen=True)
class LocalizedFailure:
    node_id: str | None
    block_lines: tuple[str, ...]


def _node_id_of(line: str) -> str | None:
    s = line.strip()
    for prefix in ("#@node ", "#@block "):
        if s.startswith(prefix):
            return s[len(prefix):].split()[0]
    return None


def localize_install_failure(script: str, failing_command: str | None) -> LocalizedFailure:
    """Map the failing command to the most recent #@node/#@block id at/above it,
    returning that id plus a bounded window of surrounding lines."""
    if not failing_command:
        return LocalizedFailure(node_id=None, block_lines=())
    lines = script.splitlines()
    fail_idx = next((i for i, l in enumerate(lines) if failing_command in l), None)
    if fail_idx is None:
        return LocalizedFailure(node_id=None, block_lines=())
    node_id = None
    for i in range(fail_idx, -1, -1):
        nid = _node_id_of(lines[i])
        if nid is not None:
            node_id = nid
            break
    lo = max(0, fail_idx - _WINDOW)
    hi = min(len(lines), fail_idx + _WINDOW + 1)
    return LocalizedFailure(node_id=node_id, block_lines=tuple(lines[lo:hi]))


def certify_reciped_only(graph, exec_readonly, cycle: int):
    """Run the host certify pass, then return (graph, unsatisfied_reciped_ids).

    The binding gate is evaluated ONLY over _is_reciped nodes — #@need stubs
    (CONFIG/SERVICE) are excluded (they are Stage-2.5)."""
    graph = certify_refresh(graph, exec_readonly, cycle)
    unsat = tuple(
        n.id for n in graph.nodes
        if _is_reciped(n) and n.state is not State.SATISFIED
    )
    return graph, unsat


def assemble_install_debug_bundle(localized: LocalizedFailure, stderr: str,
                                  repair_scope_text: str, window: tuple[str, ...]) -> str:
    """Three-part bundle: localized failure (node + block) + RepairScope slice + script window."""
    parts = [
        f"## Failing node: {localized.node_id or '(unmapped)'}",
        "### Failing block",
        "\n".join(localized.block_lines),
        "### stderr",
        (stderr or "")[-_EVIDENCE_CAP:],
        "### Graph slice (RepairScope)",
        repair_scope_text,
    ]
    if window:
        parts += ["### Script context", "\n".join(window)]
    return "\n".join(parts)
