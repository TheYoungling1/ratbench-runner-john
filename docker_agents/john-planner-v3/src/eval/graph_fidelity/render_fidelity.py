"""Phase-2 RENDER-FIDELITY checker (graph-fidelity eval loop, item 2 of
``docs/superpowers/loops/2026-07-02-graph-fidelity-eval-loop.md``): does
``render_build_script(graph)`` faithfully turn a graph into a script — every
reciped node emitted once, in valid topo order, valid bash, deterministic?

Pure: no Docker, no network, no LLM, no src.envstate. ``check_render`` never
renders internally — it grades an already-rendered ``script_text`` against the
``graph`` it claims to represent, so it stays a pure ``(graph, text) -> result``
function. Callers render once (or twice, via ``is_deterministic``).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from python_deps.depgraph.build_script import render_build_script  # noqa: E402
from python_deps.depgraph.emit import _is_installable_project, _is_reciped  # noqa: E402
from python_deps.depgraph.schema import DepGraph, State  # noqa: E402

# One #@node annotation line per reciped node (build_script._annotation); the
# node id is the first whitespace-delimited token after the marker.
_NODE_LINE_RE = re.compile(r"^#@node\s+(\S+)")


@dataclass(frozen=True)
class RenderFidelity:
    """One rendered script's fidelity to the graph it was rendered from."""

    all_reciped_emitted: bool
    missing_from_script: tuple[str, ...]
    single_emit: bool
    duplicated: tuple[str, ...]
    topo_order_ok: bool
    order_violations: tuple[tuple[str, str], ...]
    valid_bash: bool | None       # None only when `bash` is unavailable on host
    bash_error: str | None
    emitted_but_uncertified: tuple[str, ...]
    editable_last: bool | None    # None when the graph has no installable project


def _parse_emitted_order(script_text: str) -> tuple[tuple[str, int], ...]:
    """``(node_id, line_no)`` for every ``#@node`` annotation line, in the
    order they appear in the script."""
    out: list[tuple[str, int]] = []
    for line_no, line in enumerate(script_text.splitlines()):
        match = _NODE_LINE_RE.match(line)
        if match:
            out.append((match.group(1), line_no))
    return tuple(out)


def _check_all_emitted(
    reciped_ids: set[str], emitted_ids: set[str],
) -> tuple[bool, tuple[str, ...]]:
    missing = tuple(sorted(reciped_ids - emitted_ids))
    return (not missing, missing)


def _check_single_emit(
    emitted: tuple[tuple[str, int], ...],
) -> tuple[bool, tuple[str, ...]]:
    counts: dict[str, int] = {}
    for node_id, _ in emitted:
        counts[node_id] = counts.get(node_id, 0) + 1
    duplicated = tuple(sorted(nid for nid, n in counts.items() if n > 1))
    return (not duplicated, duplicated)


def _check_topo_order(
    graph: DepGraph,
    reciped_ids: set[str],
    emitted: tuple[tuple[str, int], ...],
) -> tuple[bool, tuple[tuple[str, str], ...]]:
    """Every reciped dep of an emitted node must have its EARLIEST emitted
    occurrence strictly before that node's occurrence.

    Uses the graph's real ``requires`` edges as the source of truth — the
    annotation's ``requires=`` text is a display artifact of the renderer, not
    authoritative, so a corrupted annotation must not hide a real ordering bug.
    """
    earliest_line: dict[str, int] = {}
    for node_id, line_no in emitted:
        if node_id not in earliest_line or line_no < earliest_line[node_id]:
            earliest_line[node_id] = line_no

    violations: set[tuple[str, str]] = set()
    for node_id, line_no in emitted:
        node = graph.get(node_id)
        if node is None:
            continue
        for dep in graph.requires_of(node_id):
            if dep.id not in reciped_ids:
                continue
            dep_line = earliest_line.get(dep.id)
            if dep_line is None:
                continue  # dep missing from script entirely -> reported separately
            if dep_line >= line_no:
                violations.add((node_id, dep.id))
    return (not violations, tuple(sorted(violations)))


def _check_valid_bash(script_text: str) -> tuple[bool | None, str | None]:
    """Run ``bash -n`` (syntax-only) against ``script_text``. Skips gracefully
    (``None, None``) when no ``bash`` binary is on the host."""
    bash_path = shutil.which("bash")
    if bash_path is None:
        return (None, None)
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
        handle.write(script_text)
        path = handle.name
    try:
        proc = subprocess.run([bash_path, "-n", path], capture_output=True, text=True)
    finally:
        Path(path).unlink(missing_ok=True)
    if proc.returncode == 0:
        return (True, None)
    return (False, proc.stderr.strip())


def _check_uncertified(graph: DepGraph, emitted_ids: set[str]) -> tuple[str, ...]:
    """Emitted reciped nodes not yet host-certified SATISFIED — the first-pass
    error-risk set (a script line ran, but nothing proved it actually worked)."""
    out = [
        n.id for n in graph.nodes
        if n.id in emitted_ids and _is_reciped(n) and n.state is not State.SATISFIED
    ]
    return tuple(sorted(out))


def _check_editable_last(
    graph: DepGraph,
    reciped_ids: set[str],
    emitted: tuple[tuple[str, int], ...],
) -> bool | None:
    """When the graph declares an installable PROJECT node, its editable install
    must be emitted AFTER every reciped dependency (the repo installs last —
    finding A). Returns None when there is no installable project (not
    applicable), False when the project is installable but absent from the script
    or precedes a dependency, True otherwise."""
    proj = next((n for n in graph.nodes if _is_installable_project(n)), None)
    if proj is None:
        return None
    earliest: dict[str, int] = {}
    for node_id, line_no in emitted:
        if node_id not in earliest or line_no < earliest[node_id]:
            earliest[node_id] = line_no
    proj_line = earliest.get(proj.id)
    if proj_line is None:
        return False  # installable, but never emitted — the finding-A regression
    dep_lines = [ln for nid, ln in earliest.items() if nid in reciped_ids]
    return all(proj_line > dep_line for dep_line in dep_lines)


def check_render(graph: DepGraph, script_text: str) -> RenderFidelity:
    """Grade one already-rendered ``script_text`` against the ``graph`` it
    claims to represent. Pure: does not call ``render_build_script`` itself."""
    reciped_ids = {n.id for n in graph.nodes if _is_reciped(n)}
    emitted = _parse_emitted_order(script_text)
    emitted_ids = {node_id for node_id, _ in emitted}

    all_emitted, missing = _check_all_emitted(reciped_ids, emitted_ids)
    single, duplicated = _check_single_emit(emitted)
    topo_ok, violations = _check_topo_order(graph, reciped_ids, emitted)
    valid_bash, bash_error = _check_valid_bash(script_text)
    uncertified = _check_uncertified(graph, emitted_ids)
    editable_last = _check_editable_last(graph, reciped_ids, emitted)

    return RenderFidelity(
        all_reciped_emitted=all_emitted,
        missing_from_script=missing,
        single_emit=single,
        duplicated=duplicated,
        topo_order_ok=topo_ok,
        order_violations=violations,
        valid_bash=valid_bash,
        bash_error=bash_error,
        emitted_but_uncertified=uncertified,
        editable_last=editable_last,
    )


def is_deterministic(graph: DepGraph) -> bool:
    """Render ``graph`` twice and byte-compare (``render_build_script`` is a
    pure function of the graph; this is a black-box confirmation of that)."""
    return render_build_script(graph) == render_build_script(graph)
