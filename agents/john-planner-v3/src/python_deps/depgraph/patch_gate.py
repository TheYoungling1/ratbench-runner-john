"""Deterministic PatchGate (design §10): validate -> apply -> recompose.

The v3 replacement for the LLM Maintainer. validate_proposal returns an error list
(empty = accept); apply_proposal is a pure immutable reducer that NEVER writes
SATISFIED; compose_script re-derives the artifact from the graph plus governed
manual blocks. Pure: no Docker/network/LLM."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

from python_deps.depgraph.action_class import matches_action_class
from python_deps.depgraph.block import Block, compile_blocks
from python_deps.depgraph.certify import EXECUTION_LAYER_ORDER
from python_deps.depgraph.patch import (
    PatchProposal, NodeSpec, ProviderSpec, EdgeSpec, ScriptPatch,
)
from python_deps.depgraph.schema import (
    DepGraph, Node, Edge, NodeType, Layer, EdgeType, State, DiscoveredBy, EDGE_RULES,
)
from python_deps.depgraph.service_recipes import render_probe_poll
from python_deps.depgraph.service_tables import KNOWN_SERVICE_KINDS

# Node-type -> canonical id prefix (ids.py).  Types not listed accept any "<kind>:<rest>".
_KIND_PREFIX: dict[NodeType, str] = {
    NodeType.PACKAGE: "pkg:", NodeType.SYSTEM_LIB: "syslib:", NodeType.TOOL: "tool:",
    NodeType.CONFIG: "config:", NodeType.SERVICE: "service:", NodeType.RUNTIME: "runtime:",
    NodeType.IMPORT: "import:", NodeType.PROJECT: "project:",
}
_ALLOWED_PROMOTION = frozenset({"hint", "candidate"})
_BENIGN_REDIR = re.compile(r"\s*(?:\d?>>?\s*/dev/null|\d?>&\d)")
_MUTATING = re.compile(
    r"(\bapt-get\s+install\b|\bapt\s+install\b|\bpip\d?\s+install\b|\bnpm\s+(?:install|ci)\b"
    r"|\brm\b|\bmkdir\b|\bmv\b|\bcp\b|\btee\b|\bdd\b|\btruncate\b|\bln\s+-s\b"
    r"|\bcurl\b|\bwget\b|>>|>)")


def is_read_only(cmd: str) -> bool:
    """True when *cmd* performs no env mutation. Benign /dev/null and fd-dup (2>&1)
    redirects are stripped first. Load-bearing: admitted check_commands are host-executed."""
    scrubbed = _BENIGN_REDIR.sub("", cmd or "")
    return not _MUTATING.search(scrubbed)


def _node_type(value: str) -> NodeType | None:
    try:
        return NodeType(value)
    except ValueError:
        return None


def _requirement_errors(graph: DepGraph, r: NodeSpec,
                        known_evidence_ids: frozenset[str]) -> list[str]:
    """Per-requirement legality errors (empty list = admissible). validate_proposal
    voids the whole batch on any error; callers that want graceful per-node degradation
    can pre-filter with this predicate so one malformed field costs a single node rather
    than the entire proposal."""
    from python_deps.depgraph.check_quality import check_can_detect_absence
    if not isinstance(r.id, str) or not isinstance(r.type, str):
        return [f"malformed requirement id/type: {r.id!r} / {r.type!r}"]
    nt = _node_type(r.type)
    if nt is None:
        return [f"unknown node type {r.type!r} for {r.id}"]
    errs: list[str] = []
    try:
        Layer(r.layer)
    except ValueError:
        errs.append(f"unknown layer {r.layer!r} for {r.id}")
    prefix = _KIND_PREFIX.get(nt)
    if prefix is not None and not r.id.startswith(prefix):
        errs.append(f"non-canonical id {r.id!r}: {nt.value} requires prefix {prefix!r}")
    elif ":" not in r.id:
        errs.append(f"non-canonical id {r.id!r}: missing '<kind>:' prefix")
    if r.promotion is not None and (not isinstance(r.promotion, str)
                                    or r.promotion not in _ALLOWED_PROMOTION):
        errs.append(f"illegal promotion {r.promotion!r} for {r.id} "
                    f"(only {sorted(_ALLOWED_PROMOTION)} or none; SATISFIED is host-only)")
    if not (isinstance(r.evidence_ref, str) and r.evidence_ref in known_evidence_ids):
        errs.append(f"requirement {r.id} cites unknown/absent evidence {r.evidence_ref!r}")
    if r.check_command is not None and not isinstance(r.check_command, str):
        errs.append(f"check command for {r.id} must be a string: {r.check_command!r}")
    elif r.check_command and not is_read_only(r.check_command):
        errs.append(f"check command for {r.id} is not read-only: {r.check_command!r}")
    elif r.check_command and not check_can_detect_absence(r.check_command):
        errs.append(f"check command for {r.id} cannot detect absence "
                    f"(structurally trivial): {r.check_command!r}")
    cur = graph.get(r.id)
    if cur is not None and (cur.type.value != r.type or cur.layer.value != r.layer
                            or (cur.check_command or None) != (r.check_command or None)):
        errs.append(f"conflicting redefinition of existing node {r.id}")
    if r.service_kind is not None and not isinstance(r.service_kind, str):
        errs.append(f"service_kind for {r.id} must be a string: {r.service_kind!r}")
    elif (r.service_kind is not None and r.setup is None
          and r.service_kind not in KNOWN_SERVICE_KINDS):
        # Legacy nodes are restricted to KNOWN_SERVICE_KINDS; a clean setup-shape Service
        # may carry an EXOTIC kind (couchdb, qdrant, …) — the deterministic table doesn't
        # bound the open-vocabulary translate path.
        errs.append(f"unknown service_kind {r.service_kind!r} for {r.id}")
    if r.service_params is not None and not isinstance(r.service_params, dict):
        errs.append(f"service_params for {r.id} must be an object")
    # CLEAN setup-shape (Inc 5, the sole Service shape). A setup is valid ONLY on a Service
    # node; the EMPTY-PROBE GUARD then requires a runnable read-only probe, else its
    # render_probe_poll("") check_command is a broken shell and the node can never demote at
    # certify -> deadlock. Reject rather than admit.
    if r.setup is not None:
        if nt is not NodeType.SERVICE:
            errs.append(f"setup is only valid on a Service node, not {r.id}")
        elif not isinstance(r.setup, dict):
            errs.append(f"setup for {r.id} must be a dict: {r.setup!r}")
        else:
            probe = r.setup.get("probe")
            if not (isinstance(probe, str) and probe.strip()):
                errs.append(f"setup service {r.id} must carry a non-empty probe")
            elif not is_read_only(probe):
                errs.append(f"setup probe for {r.id} is not read-only: {probe!r}")
            if not isinstance(r.setup.get("install"), list):
                errs.append(f"setup for {r.id} must have an install list")
            if not (isinstance(r.setup.get("start"), str) and r.setup.get("start", "").strip()):
                errs.append(f"setup for {r.id} must have a start command")
    return errs


def validate_proposal(graph: DepGraph, proposal: PatchProposal, *,
                      known_evidence_ids: frozenset[str]) -> list[str]:
    errs: list[str] = []
    existing_ids = {n.id for n in graph.nodes}
    proposed_node_ids = {r.id for r in proposal.add_requirements}
    # Lazy import (not module-level) keeps python_deps.depgraph envstate-free; used by the
    # script-patch-check anti-weakening guard below (_requirement_errors does its own lazy import).
    from python_deps.depgraph.check_quality import check_can_detect_absence

    # within-proposal duplicate ids (nodes / providers / script blocks)
    for label, ids in (("add_requirements", [r.id for r in proposal.add_requirements]),
                       ("add_providers", [p.id for p in proposal.add_providers]),
                       ("script_patches", [s.block_id for s in proposal.script_patches])):
        if len(ids) != len(set(ids)):
            errs.append(f"duplicate id within {label}")

    for r in proposal.add_requirements:
        errs.extend(_requirement_errors(graph, r, known_evidence_ids))

    known_after = existing_ids | proposed_node_ids
    for p in proposal.add_providers:
        if not matches_action_class(p.kind, p.command):
            errs.append(f"provider {p.id} command does not match action class "
                        f"{p.kind!r}: {p.command!r}")
        for nid in p.provides:
            if nid not in known_after:
                errs.append(f"provider {p.id} provides unknown node {nid!r}")

    for s in proposal.script_patches:
        if not s.evidence_ref or s.evidence_ref not in known_evidence_ids:
            errs.append(f"script block {s.block_id} cites unknown/absent evidence {s.evidence_ref!r}")
        if not s.target_node_ids:
            errs.append(f"script block {s.block_id} has empty target_node_ids")
        for nid in s.target_node_ids:
            if nid not in known_after:
                errs.append(f"script block {s.block_id} targets unknown node {nid!r}")
        for chk in s.checks:
            if not is_read_only(chk):
                errs.append(f"script block {s.block_id} check is not read-only: {chk!r}")
            if not check_can_detect_absence(chk):
                errs.append(f"script block {s.block_id} check cannot detect absence "
                            f"(structurally trivial): {chk!r}")
        if not s.commands:
            errs.append(f"script block {s.block_id} has empty commands")
        if any(not c.strip() for c in s.commands):
            errs.append(f"script block {s.block_id} has a blank/whitespace-only command")
        try:
            Layer(s.wave)
        except ValueError:
            errs.append(f"script block {s.block_id} has illegal wave {s.wave!r} "
                        f"(must be a Layer value)")
        for nid in s.provides:
            if nid not in known_after:
                errs.append(f"script block {s.block_id} provides unknown node {nid!r}")

    # edges: replicate EDGE_RULES against the post-add_requirements view (with_edge would RAISE).
    type_of = {n.id: n.type.value for n in graph.nodes}
    type_of.update({r.id: r.type for r in proposal.add_requirements})
    for e in proposal.add_edges:
        try:
            EdgeType(e.relation)
        except ValueError:
            errs.append(f"unknown edge relation {e.relation!r}"); continue
        rule = EDGE_RULES.get(e.relation)
        if e.source not in type_of or e.target not in type_of:
            errs.append(f"edge {e.relation} references unknown node(s): {e.source!r} -> {e.target!r}")
            continue
        if rule is not None:
            allowed_src, allowed_dst = rule
            if type_of[e.source] not in allowed_src:
                errs.append(f"illegal {e.relation} source type {type_of[e.source]!r} ({e.source!r})")
            if type_of[e.target] not in allowed_dst:
                errs.append(f"illegal {e.relation} destination type {type_of[e.target]!r} ({e.target!r})")

    return errs


@dataclass(frozen=True)
class ApplyResult:
    graph: DepGraph
    blocks: tuple[Block, ...]


def _provider_fix(p: ProviderSpec) -> str:
    # apt providers store the "apt:NAME" form (emit._apt_name strips the prefix);
    # everything else stores the literal command (compile_blocks' fallback emits it,
    # and for PACKAGE nodes compile_blocks derives the pip command from name/version).
    return p.id if p.id.startswith("apt:") else p.command


def _script_patch_to_block(s: ScriptPatch) -> Block:
    return Block(
        block_id=s.block_id, wave=s.wave, commands=s.commands,
        target_node_ids=s.target_node_ids, provider_ids=s.provides,
        check_commands=s.checks,
        evidence_refs=(s.evidence_ref,) if s.evidence_ref else (),
    )


def apply_proposal(graph: DepGraph, proposal: PatchProposal) -> ApplyResult:
    g = graph
    # 1. requirement nodes — always MISSING (never SATISFIED), promotion tag if present.
    for r in proposal.add_requirements:
        if g.get(r.id) is not None:
            continue                                    # dedup no-op (validate ensured non-conflicting)
        nt = NodeType(r.type)
        data: dict = {}
        if r.promotion:
            data["promotion"] = r.promotion
        check_command = r.check_command
        if r.setup is not None and nt is NodeType.SERVICE:
            # CLEAN setup-shape Service: store the pinned setup; the read-only probe
            # (validate re-checked non-empty + read-only) becomes the node's bounded
            # check_command via the probe-poll loop.
            data["setup"] = r.setup
            if r.service_kind:
                data["service_kind"] = r.service_kind
            check_command = render_probe_poll(r.setup["probe"])
        g = g.with_node(Node(
            id=r.id, type=nt, name=r.name or r.id.split(":", 1)[-1],
            layer=Layer(r.layer), discovered_by=DiscoveredBy.CLASSIFIER, state=State.MISSING,
            check_command=check_command, evidence=r.evidence_ref, data=data,
        ))
    # 2. providers -> chosen_fix on each provided node (first writer wins).
    for p in proposal.add_providers:
        fix = _provider_fix(p)
        for nid in p.provides:
            node = g.get(nid)
            if node is not None and (node.chosen_fix is None or p.override):
                g = g.with_node(replace(node, chosen_fix=fix))
    # 3. edges — endpoints now exist (validate guaranteed legality; with_edge dedupes).
    for e in proposal.add_edges:
        g = g.with_edge(Edge(src=e.source, dst=e.target, relation=EdgeType(e.relation),
                             data={"hard": e.hard}))
    # 4. script_patches -> governed blocks; they NEVER mutate node state.
    blocks = tuple(_script_patch_to_block(s) for s in proposal.script_patches)
    return ApplyResult(graph=g, blocks=blocks)


# Wave rank for slotting manual blocks; shares the certified EXECUTION_LAYER_ORDER
# (design section 6, also used by certify.certify_all and build_script.render_build_script)
# so the live compose order matches render_build_script's artifact order (replay
# fidelity) and RUNTIME/CONFIG/TESTS waves sort correctly, rather than the raw Layer
# enum declaration order. Layers not in EXECUTION_LAYER_ORDER (e.g. SERVICES) sort
# last, in enum order. compile_blocks already emits compiled blocks in topo
# (wave-rank-nondecreasing) order, and Python's sort is STABLE, so sorting the
# merged list by wave rank leaves compiled blocks in place and slots each manual
# block after the compiled blocks of its wave.
_WAVE_ORDER: tuple[Layer, ...] = EXECUTION_LAYER_ORDER + tuple(
    L for L in Layer if L not in EXECUTION_LAYER_ORDER
)
_WAVE_RANK: dict[str, int] = {layer.value: i for i, layer in enumerate(_WAVE_ORDER)}


def compose_script(graph: DepGraph, manual_blocks: tuple[Block, ...] = ()) -> tuple[Block, ...]:
    compiled = compile_blocks(graph)
    seen = {b.block_id for b in compiled}
    fresh = []
    for b in manual_blocks:
        if b.block_id in seen:                 # graph-compiled block wins on id collision
            continue
        seen.add(b.block_id)
        fresh.append(b)
    if not fresh:
        return compiled
    merged = list(compiled) + fresh            # compiled first -> stable sort keeps them first per wave
    merged.sort(key=lambda b: _WAVE_RANK.get(b.wave, len(_WAVE_RANK)))
    return tuple(merged)


@dataclass(frozen=True)
class AdmitResult:
    accepted: bool
    errors: tuple[str, ...]
    graph: DepGraph
    blocks: tuple[Block, ...]
    manual_blocks: tuple[Block, ...]


def admit_proposal(graph: DepGraph, proposal: PatchProposal, *,
                   manual_blocks: tuple[Block, ...] = (),
                   known_evidence_ids: frozenset[str]) -> AdmitResult:
    """Pure: validate -> apply -> recompose. On reject, graph/manual_blocks unchanged and
    errors non-empty. NEVER writes SATISFIED, NEVER runs anything."""
    errs = validate_proposal(graph, proposal, known_evidence_ids=known_evidence_ids)
    if errs:
        return AdmitResult(False, tuple(errs), graph,
                           compose_script(graph, manual_blocks), manual_blocks)
    applied = apply_proposal(graph, proposal)
    new_manual = manual_blocks + applied.blocks
    return AdmitResult(True, (), applied.graph,
                       compose_script(applied.graph, new_manual), new_manual)
