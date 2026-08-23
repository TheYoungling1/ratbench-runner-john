"""Strict-shell block runner (design §7): execute annotated blocks, log typed
Evidence, certify target nodes via the host-check path. The v3 analog of
depgraph_live.emit_drain, but runs raw block commands with NO LLM seeding.

Invariant #2/#3: a block exiting 0 never certifies a node — only certify_refresh
(a real host check) writes SATISFIED.
"""
from __future__ import annotations

from typing import Callable

from python_deps.depgraph.block import Block
from python_deps.depgraph.evidence_log import Evidence, EvidenceBundle
from src.envstate.depgraph_live import certify_refresh, ensure_python_shim
from src.envstate.text_util import truncate_output


def run_blocks(
    blocks: tuple[Block, ...],
    sandbox_execute: Callable[[str], tuple[bool, str]],
    exec_readonly: Callable[[str], tuple[int, str]],
    graph,
    cycle: int,
    *,
    container_kind: str = "canonical",
) -> tuple[object, EvidenceBundle, str | None]:
    ensure_python_shim(sandbox_execute)
    bundle = EvidenceBundle()
    failed_block_id: str | None = None
    ev_n = 0
    for block in blocks:
        ok = True
        out = ""
        for cmd in block.commands:
            ok, out = sandbox_execute(cmd)
            ev = Evidence(
                evidence_id=f"ev.{cycle}.{ev_n}", container_kind=container_kind,
                command=cmd, rc=0 if ok else 1,
                output_excerpt=truncate_output(out or ""), cycle=cycle,
                block_id=block.block_id,
                node_id=block.target_node_ids[0] if block.target_node_ids else None,
            )
            bundle = bundle.with_item(ev)
            ev_n += 1
            if not ok:
                failed_block_id = block.block_id
                break
        if not ok:
            break
        # block rc==0: certify the WHOLE graph via host checks (SATISFIED only on check pass)
        graph = certify_refresh(graph, exec_readonly, cycle)
    return graph, bundle, failed_block_id
