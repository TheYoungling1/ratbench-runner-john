# src/envstate/repair_loop.py
"""Bounded typed-patch repair loop (2b §6.3). Dependency-injected (propose + emit passed in)
so it is unit-testable with no Docker/LLM and touches no globals. The only state writers
remain certify_refresh (inside `emit`) and the graph reducer (apply_proposal inside admit)."""
from __future__ import annotations

from dataclasses import dataclass

from python_deps.depgraph.patch_gate import admit_proposal, compose_script
from src.envstate.repair_scope import build_repair_scope


@dataclass(frozen=True)
class RepairOutcome:
    graph: object
    still_failing_id: str | None
    manual_blocks: tuple
    known_invalid: frozenset
    turns_spent: int
    budget_exhausted: bool


def run_structured_repair(
    graph, failed_id, bundle, cycle, *,
    propose, emit,
    manual_blocks=(), known_invalid=frozenset(),
    max_repairs=5, repair_budget=10 ** 9,
    constraints=None, target_hint=None,
    scope_builder=build_repair_scope,
    cap_failed_id: bool = False,
):
    ki = set(known_invalid)
    mb = tuple(manual_blocks)
    turns = 0
    last_failed_cmd = None
    cons = dict(constraints or {})
    for _attempt in range(max_repairs):
        if turns >= repair_budget:
            return RepairOutcome(graph, failed_id, mb, frozenset(ki), turns, True)
        blocks = compose_script(graph, mb)
        failed_block = next((b for b in blocks if b.block_id == failed_id), None)
        target = (failed_block.target_node_ids[0]
                  if (failed_block and failed_block.target_node_ids) else target_hint)
        scope = scope_builder(graph, target_node_id=target, failed_block=failed_block,
                              bundle=bundle, known_invalid=tuple(sorted(ki)), constraints=cons)
        # Convergence guard: never re-attempt the identical failing command.
        if scope.failed_command and scope.failed_command == last_failed_cmd:
            ki.add(scope.failed_command)
            return RepairOutcome(graph, failed_id, mb, frozenset(ki), turns, False)
        proposal = propose(scope)
        turns += 1
        res = (admit_proposal(graph, proposal, manual_blocks=mb,
                              known_evidence_ids=scope.known_evidence_ids)
               if proposal is not None else None)
        if res is not None and not res.accepted:                 # §8: re-prompt ONCE with errors
            proposal = propose(scope, rejection_errors=res.errors)
            turns += 1
            res = (admit_proposal(graph, proposal, manual_blocks=mb,
                                  known_evidence_ids=scope.known_evidence_ids)
                   if proposal is not None else None)
        if res is None or not res.accepted:
            ki.add(scope.failed_command or failed_id)
            return RepairOutcome(graph, failed_id, mb, frozenset(ki), turns, False)
        graph, mb = res.graph, res.manual_blocks
        last_failed_cmd = scope.failed_command
        graph, bundle, new_failed_id = emit(graph, mb)
        if new_failed_id is None:
            return RepairOutcome(graph, None, mb, frozenset(ki), turns, False)
        if cap_failed_id and new_failed_id != failed_id:
            # Stage 2: original node fixed but a different node now fails; return the
            # original id and let the outer loop re-verify from a fresh base (no silent pivot).
            return RepairOutcome(graph, failed_id, mb, frozenset(ki), turns, False)
        failed_id = new_failed_id
        ki.add(scope.failed_command or "")
    return RepairOutcome(graph, failed_id, mb, frozenset(ki), turns, False)
