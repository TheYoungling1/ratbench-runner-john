"""Envstate adapter for the graph scheduler.

Turns the pure scheduling layer's next actionable obligation into a
PlannerDecision the orchestrator already knows how to execute. The graph decides
*what & when*; this module only translates that into the existing Task/Planner
message types. It writes no graph state.
"""
from __future__ import annotations

from typing import Callable

from python_deps.depgraph.schema import DepGraph
from python_deps.depgraph.schedule import (
    ObligationPacket, frame_obligation, scheduler_frontier,
)
from python_deps.depgraph.req_slice import render_requirement_slice
from src.envstate.constants import VERIFY_TEST_CMD
from src.envstate.world_model import PlannerDecision, Task


def packet_to_task(packet: ObligationPacket) -> Task:
    facts: list[str] = []
    if packet.requirement_slice is not None:
        facts.extend(render_requirement_slice(packet.requirement_slice))
    else:
        # back-compat: a packet without a slice keeps the old flat facts
        if packet.evidence:
            facts.append(f"evidence: {packet.evidence}")
        if packet.depends_on:
            facts.append("depends_on: " + ", ".join(packet.depends_on))
        if packet.certified_context:
            facts.append("already satisfied: " + ", ".join(packet.certified_context))
    # Clean CR6 setup recipe — the service provisioning recipe.
    if packet.setup:
        su = packet.setup
        for step in (su.get("install") or []):
            facts.append(f"install: {step}")
        if su.get("start"):
            facts.append("start the service in-image (run, then the host re-checks "
                         f"`{packet.check_command}`): {su['start']}")
        if su.get("createdb"):
            facts.append(f"then create the bound database: {su['createdb']}")
        for step in (su.get("post") or []):
            facts.append(f"post-setup: {step}")
        for step in (su.get("bind") or []):
            facts.append(f"repoint config: {step}")
    return Task(
        goal=packet.goal,
        done_when=packet.check_command,
        layer=packet.layer,
        facts=tuple(facts),
        target_node_ids=(packet.node_id,),
    )


def _discover_task() -> Task:
    return Task(
        goal=(
            "All known requirements are satisfied but the test suite still fails. "
            "Run the suite, read the failure, and install or provide whatever the "
            "running code actually needs (a missing dynamic import, a system "
            "library, a runtime env var, or a service) until the tests pass."
        ),
        done_when=VERIFY_TEST_CMD,
        layer="tests",
        facts=(),
    )


def unsatisfied_provisionable_services(
    graph: DepGraph | None, *, allow_services: bool
) -> tuple:
    """Provisionable (``data["setup"]``) SERVICE nodes not yet host-certified and
    not demoted out of the gate (``certify_fail_count < 3``).

    When ``allow_services`` and this is non-empty, a "passing" suite is a HOLLOW
    pass — the in-image service the tests need is not up (the
    1-unit-test-rides-to-0.2 trap, design §10) — so neither the scheduler's
    ``done`` door nor the loop's fast-termination may declare success. Returns
    ``()`` when services are off or ``graph`` is None, so a caller can treat any
    non-empty result as an unconditional "block done" signal. Shared by
    ``next_decision`` (below) and ``orchestrator.run_v3``'s fast-termination so
    the two apply the identical guard.
    """
    from python_deps.depgraph.schema import NodeType, State
    if not allow_services or graph is None:
        return ()
    return tuple(
        n for n in graph.nodes
        if n.type is NodeType.SERVICE
        and n.data.get("setup") is not None                         # only a provisionable (setup) service gates 'done'
        and n.state is not State.SATISFIED
        and n.data.get("certify_fail_count", 0) < 3                 # demote: 3 strikes -> drop out of the gate
    )


def next_decision(
    graph: DepGraph | None,
    run_tests: Callable[[], bool],
    handed: dict[str, int] | None = None,
    attempt_cap: int = 3,
    *,
    allow_services: bool | None = None,
    residual_ids: frozenset[str] = frozenset(),
) -> tuple[PlannerDecision, str | None]:
    """Decide the next action from the certified graph (no LLM).

    Returns (decision, chosen_obligation_id). chosen_id is the node handed to the
    agent (so the caller can bump its oscillation counter), or None for the
    done / discover-task branches.

    allow_services: when None (default), resolved from env var
    DOCKERAGENT_ENABLE_SERVICE_PROVISION; pass True/False explicitly in tests.
    """
    import os
    handed = handed or {}
    if allow_services is None:
        allow_services = os.environ.get("DOCKERAGENT_ENABLE_SERVICE_PROVISION") == "1"
    frontier = scheduler_frontier(graph, allow_services=allow_services) if graph is not None else ()
    # residual_ids: nodes whose repair diagnosed RESIDUAL (unrepairable by any
    # env change) — excluded so the scheduler stops re-handing them; the loop's
    # residual-stall giveup owns convergence (design: residual-node-drop.md).
    eligible = [n for n in frontier
                if handed.get(n.id, 0) < attempt_cap and n.id not in residual_ids]
    if eligible:
        node = eligible[0]
        decision = PlannerDecision(
            action="task", task=packet_to_task(frame_obligation(graph, node))
        )
        return decision, node.id
    if run_tests():
        if unsatisfied_provisionable_services(graph, allow_services=allow_services):
            # Anti-hollow: tests "passing" while a required in-image service is not
            # host-certified up is the 1-unit-test-rides-to-0.2 trap (design §10).
            return PlannerDecision(action="task", task=_discover_task()), None
        return PlannerDecision(
            action="done", reason="graph-scheduler: frontier clean, tests pass"
        ), None
    return PlannerDecision(action="task", task=_discover_task()), None
