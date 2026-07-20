"""Deterministic block-emit phase for v3 (design §5.1): compile the graph's emittable
wave to blocks, run them, certify via host checks, and dual-write a minimal ActionLedger
(the state-capture feed) alongside the typed EvidenceBundle. NO LLM. The v3 analog of
emit_drain, but graph-compiled and free of build_agent.

QUARANTINE (Phase 9): this module is the incremental-execution **ablation
baseline**, NOT the canonical v3 method. The canonical executor is fresh
full-script replay — ``orchestrator._binding_emit`` inside ``run_v3``, which
renders the WHOLE certified graph to one install-only script, resets the
container to base, and replays it every cycle (Model B). ``run_v3`` never
imports or calls ``block_emit`` (grep-confirmed at Phase 9); it exists here
only for (a) its own direct unit tests and (b) a future incremental-vs-replay
ablation harness — a separate experiment, not part of the canonical claim.
Do not wire this back into ``run_v3``; if you need incremental block-at-a-time
execution today, that is ``run_v1``'s ``emit_drain``/``repair_failed_nodes``
path (``src/envstate/depgraph_live.py``), which has the same quarantine
status relative to ``run_v3``."""
from __future__ import annotations

from typing import Callable

from python_deps.depgraph.patch_gate import compose_script
from src.envstate.script_runner import run_blocks
from src.envstate.ledger import ActionEvent, ActionLedger


def block_emit(
    graph,
    sandbox_execute: Callable[[str], tuple[bool, str]],
    exec_readonly: Callable[[str], tuple[int, str]],
    ledger: ActionLedger,
    cycle: int,
    *,
    manual_blocks: tuple = (),
):
    """Run the graph-compiled blocks; mirror each command into ``ledger``; certify via
    run_blocks' host checks. Returns (certified_graph, EvidenceBundle, failed_block_id).

    The dual-write records ACTIONS only — node state is written exclusively by
    certify_refresh inside run_blocks (invariants #3/#4). Both successful and failed
    commands are mirrored; failures (rc != 0) feed _runtime_ingest_phase."""
    blocks = compose_script(graph, manual_blocks)

    def _mirroring_sandbox(cmd: str) -> tuple[bool, str]:
        ok, out = sandbox_execute(cmd)
        ledger.append(ActionEvent(
            step=len(ledger.events()),          # monotonic step (ActionEvent.step is required)
            cmd=cmd,
            rc=0 if ok else 1,
            stdout=out or "",
            mutation_class="file_or_env_change",
        ))
        return ok, out

    return run_blocks(blocks, _mirroring_sandbox, exec_readonly, graph, cycle)
