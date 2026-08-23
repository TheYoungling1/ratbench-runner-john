"""Typed PatchProposal model + tolerant parser (design §9). Pure: no Docker/network/LLM.

The model is the v3 LLM contract (invariant #6): the only accepted state change is a
PatchProposal. NodeSpec deliberately has NO `state` field — the model cannot certify."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NodeSpec:
    id: str
    type: str            # NodeType value, e.g. "SystemLib"
    name: str
    layer: str           # Layer value, e.g. "system"
    check_command: str | None = None
    evidence_ref: str | None = None
    promotion: str | None = None   # "hint" | "candidate" | None (gate validates)
    # Service/Config tier (design 2026-07-03). The LLM proposes DATA, never shell:
    service_kind: str | None = None         # KNOWN_SERVICE_KINDS member, or exotic on a setup node (gate validates)
    service_params: dict | None = None      # {db, var, scheme, port, extensions...} for the renderer
    # CLEAN setup-shape Service (Inc 5, the sole Service shape). Carries the pinned recipe
    # {install:[...], start, probe, createdb?, post:[...]}; the gate derives check_command from
    # render_probe_poll(setup["probe"]).
    setup: dict | None = None


@dataclass(frozen=True)
class ProviderSpec:
    id: str              # e.g. "apt:libplacebo-dev"
    kind: str            # action class, e.g. "apt" | "pip" | "npm" | "shell"
    command: str
    provides: tuple[str, ...] = ()
    override: bool = False   # True => replace an existing chosen_fix (repair correction)


@dataclass(frozen=True)
class EdgeSpec:
    source: str
    target: str
    relation: str = "requires"
    hard: bool = True


@dataclass(frozen=True)
class ScriptPatch:
    block_id: str
    wave: str
    commands: tuple[str, ...]
    target_node_ids: tuple[str, ...]
    op: str = "add_block"
    checks: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    evidence_ref: str | None = None


@dataclass(frozen=True)
class PatchProposal:
    rationale: dict = field(default_factory=dict)   # advisory only
    add_requirements: tuple[NodeSpec, ...] = ()
    add_providers: tuple[ProviderSpec, ...] = ()
    add_edges: tuple[EdgeSpec, ...] = ()
    script_patches: tuple[ScriptPatch, ...] = ()
    request_checks: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (self.add_requirements or self.add_providers or self.add_edges
                    or self.script_patches or self.request_checks)


def _as_tuple(x) -> tuple:
    return tuple(x) if isinstance(x, (list, tuple)) else ()


class PatchParseError(ValueError):
    """Missing required fields -> structured rejection (the v3 propose path retries/rejects)."""
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def parse_patch_proposal(d: dict) -> PatchProposal:
    d = d or {}
    patch = d.get("patch", d)
    rationale = d.get("rationale", {})
    if not isinstance(rationale, dict):
        rationale = {}
    errs: list[str] = []

    def _req(item, key, ctx):
        if key not in item:
            errs.append(f"{ctx}: missing required key {key!r}")
            return None
        return item[key]

    reqs = tuple(NodeSpec(
        id=_req(r, "id", "add_requirements"), type=_req(r, "type", "add_requirements"),
        name=r.get("name", ""), layer=_req(r, "layer", "add_requirements"),
        check_command=r.get("check_command"), evidence_ref=r.get("evidence_ref"),
        promotion=r.get("promotion") if r.get("promotion") is not None else r.get("state"),
        service_kind=r.get("service_kind"),
        service_params=r.get("service_params"),
        setup=r.get("setup"),
    ) for r in _as_tuple(patch.get("add_requirements")))
    provs = tuple(ProviderSpec(
        id=_req(p, "id", "add_providers"), kind=_req(p, "kind", "add_providers"),
        command=_req(p, "command", "add_providers"), provides=_as_tuple(p.get("provides")),
        override=bool(p.get("override", False)),
    ) for p in _as_tuple(patch.get("add_providers")))
    edges = tuple(EdgeSpec(
        source=_req(e, "source", "add_edges"), target=_req(e, "target", "add_edges"),
        relation=e.get("relation", "requires"), hard=bool(e.get("hard", True)),
    ) for e in _as_tuple(patch.get("add_edges")))
    sps = tuple(ScriptPatch(
        block_id=_req(s, "block_id", "script_patches"), wave=_req(s, "wave", "script_patches"),
        commands=_as_tuple(s.get("commands")) or ((s["command"],) if s.get("command") else ()),
        target_node_ids=_as_tuple(s.get("target_node_ids")),
        op=s.get("op", "add_block"), checks=_as_tuple(s.get("checks")),
        provides=_as_tuple(s.get("provides")), evidence_ref=s.get("evidence_ref"),
    ) for s in _as_tuple(patch.get("script_patches")))
    if errs:
        raise PatchParseError(errs)
    return PatchProposal(
        rationale=rationale, add_requirements=reqs, add_providers=provs, add_edges=edges,
        script_patches=sps, request_checks=_as_tuple(patch.get("request_checks")),
    )
