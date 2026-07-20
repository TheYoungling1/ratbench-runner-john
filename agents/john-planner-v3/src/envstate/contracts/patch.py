"""Semantic GraphPatch (spec §7) + host-internal update fields + tolerant parser."""
from __future__ import annotations
import dataclasses
from typing import Any
from .nodes import Edge, Node, edge_from_dict, node_from_dict


@dataclasses.dataclass(frozen=True)
class GraphPatch:
    add_contracts: tuple[Node, ...] = ()
    add_blockers: tuple[Node, ...] = ()
    add_edges: tuple[Edge, ...] = ()
    update_blocker_classification: tuple[dict, ...] = ()
    update_contract_description: tuple[dict, ...] = ()
    diagnostic_notes: tuple[str, ...] = ()
    # host-internal (never parsed from LLM):
    add_attempts: tuple[Node, ...] = ()
    update_blockers: tuple[Node, ...] = ()
    update_contracts: tuple[Node, ...] = ()
    update_attempts: tuple[Node, ...] = ()
    invalidate_nodes: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (self.add_contracts or self.add_blockers or self.add_edges
                    or self.update_blocker_classification or self.update_contract_description
                    or self.diagnostic_notes or self.add_attempts or self.update_blockers
                    or self.update_contracts or self.update_attempts or self.invalidate_nodes)


def _nodes(v: Any) -> tuple[Node, ...]:
    return tuple(node_from_dict(x) for x in v if isinstance(x, dict)) if isinstance(v, list) else ()


def _dicts(v: Any) -> tuple[dict, ...]:
    return tuple(x for x in v if isinstance(x, dict)) if isinstance(v, list) else ()


def parse_graph_patch(d: Any) -> GraphPatch:
    if not isinstance(d, dict):
        return GraphPatch()
    return GraphPatch(
        add_contracts=_nodes(d.get("add_contracts")),
        add_blockers=_nodes(d.get("add_blockers")),
        add_edges=tuple(edge_from_dict(x) for x in d.get("add_edges", []) if isinstance(x, dict))
                  if isinstance(d.get("add_edges"), list) else (),
        update_blocker_classification=_dicts(d.get("update_blocker_classification")),
        update_contract_description=_dicts(d.get("update_contract_description")),
        diagnostic_notes=tuple(str(x) for x in d.get("diagnostic_notes", []))
                         if isinstance(d.get("diagnostic_notes"), list) else (),
    )
