# src/envstate/repair_scope.py
"""The §9 RepairScope packet (2b §6.1): curated, structured context for the v3 typed-patch
agent. Pure: no Docker/network/LLM. Re-derives the Phase-1 RequirementSlice; never raw history."""
from __future__ import annotations

from dataclasses import dataclass


PATCH_SCHEMA_HINT = """\
Respond with EXACTLY ONE fenced JSON object and nothing after it:
```json
{
  "rationale": {"why": "<one sentence>"},
  "patch": {
    "add_requirements": [{"id": "syslib:<name>", "type": "SystemLib", "name": "<name>",
       "layer": "system", "check_command": "<read-only check>", "evidence_ref": "<ev.id>"}],
    "add_providers": [{"id": "apt:<pkg>", "kind": "apt",
       "command": "apt-get install -y <pkg>", "provides": ["syslib:<name>"], "override": false}],
    "add_edges": [{"source": "<id>", "target": "<id>", "relation": "requires", "hard": true}],
    "script_patches": [{"block_id": "<layer>.<short>", "wave": "system",
       "commands": ["<install>"], "target_node_ids": ["<node id>"],
       "checks": ["<read-only check>"], "provides": ["<id>"], "evidence_ref": "<ev.id>"}]
  }
}
```
Rules: canonical ids (syslib:/pkg:/tool:/service:/config:). check_command MUST be read-only.
Cite an evidence_ref present in the evidence below. Set "override": true to replace a known-bad provider."""


@dataclass(frozen=True)
class RepairScope:
    target_node_id: str | None
    failed_command: str | None
    failed_output: str
    slice_lines: tuple[str, ...]
    known_invalid: tuple[str, ...]
    constraints: tuple[tuple[str, str], ...]
    known_evidence_ids: frozenset[str]


def build_repair_scope(graph, *, target_node_id, failed_block, bundle,
                       known_invalid=(), constraints=None):
    cons = tuple(sorted((str(k), str(v)) for k, v in dict(constraints or {}).items()))
    # Graph context (the requirement slice) is intentionally OMITTED from the repair
    # prompt: the C0/C1 graph-repair ablation showed it does not improve the agent's
    # localization (and can mislead on the graph's own over-predictions). The dependency
    # graph stays the patch OUTPUT target + the gate/replay rail — it is dropped only as
    # agent-facing reasoning CONTEXT. `graph` is kept in the signature (the repair_loop
    # scope_builder contract) and the `slice_lines` field + render branch are retained so
    # this can be re-enabled — or replaced by a territory/state graph — by populating
    # slice_lines here.
    slice_lines = ()
    failed_cmd = failed_block.commands[-1] if (failed_block and failed_block.commands) else None
    failed_out = ""
    # bundle may be None on the binding-install path when there is no install-command failure
    # (e.g. install rc 0 but a reciped check still MISSING). On an install failure the binding
    # path supplies a single-item EvidenceBundle built from the InstallResult (see
    # orchestrator._build_install_evidence). Treat a missing bundle as empty evidence rather
    # than crashing on bundle.items.
    _evidence = bundle.items if bundle is not None else ()
    for ev in _evidence:
        if ev.rc != 0 and (failed_block is None or ev.block_id == failed_block.block_id):
            failed_cmd = failed_cmd or ev.command
            failed_out = ev.output_excerpt or ""
    return RepairScope(
        target_node_id=target_node_id, failed_command=failed_cmd, failed_output=failed_out,
        slice_lines=slice_lines, known_invalid=tuple(known_invalid), constraints=cons,
        known_evidence_ids=frozenset(ev.evidence_id for ev in _evidence))


def render_repair_scope(scope: RepairScope) -> str:
    parts = []
    if scope.target_node_id:
        parts.append(f"Failing obligation: {scope.target_node_id}")
    if scope.slice_lines:
        parts.append("Graph context:\n" + "\n".join(scope.slice_lines))
    if scope.failed_command:
        parts.append(f"Failed command: {scope.failed_command}")
    if scope.failed_output:
        parts.append("Failure output:\n" + scope.failed_output)
    if scope.known_invalid:
        parts.append("DO NOT propose these (already failed): " + ", ".join(scope.known_invalid))
    if scope.constraints:
        parts.append("Constraints: " + ", ".join(f"{k}={v}" for k, v in scope.constraints))
    parts.append("Cite evidence by id (available: "
                 + ", ".join(sorted(scope.known_evidence_ids)) + ").")
    parts.append(PATCH_SCHEMA_HINT)
    return "\n\n".join(parts)
