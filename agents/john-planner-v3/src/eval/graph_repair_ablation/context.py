"""The two arm treatments, as strings appended to the repair scope.

flat_list_context (C0.5) = the dependency INFORMATION with none of the structure.
graph_context     (C1)   = the same needs, but typed + tiered + provenance + the
                           chosen_fix hint + the requires-neighborhood (the STRUCTURE)."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from python_deps.depgraph.schema import DepGraph, NodeType  # noqa: E402


def flat_list_context(graph: DepGraph) -> str:
    names = []
    for n in graph.nodes:
        if n.type is NodeType.PACKAGE:
            names.append(f"{n.name}=={n.version}" if n.version else n.name)
    names.sort()
    return "Declared dependencies:\n" + "\n".join(f"- {x}" for x in names)


def graph_context(graph: DepGraph, symptom_ids: tuple[str, ...] = ()) -> str:
    lines = ["Dependency graph (typed, tiered):"]
    for n in sorted(graph.nodes, key=lambda x: (x.tier, x.type.value, x.name)):
        if n.type in (NodeType.PROJECT, NodeType.TEST, NodeType.IMPORT):
            continue
        fix = f"  fix={n.chosen_fix}" if n.chosen_fix else ""
        prov = n.discovered_by.value
        lines.append(f"- [{n.type.value} tier={n.tier}] {n.name} "
                     f"state={n.state.value} via={prov}{fix}")
    # requires-neighborhood so the agent can walk symptom -> cause
    lines.append("\nrequires edges (src requires dst):")
    for e in graph.edges:
        lines.append(f"- {e.src} requires {e.dst}")
    return "\n".join(lines)
