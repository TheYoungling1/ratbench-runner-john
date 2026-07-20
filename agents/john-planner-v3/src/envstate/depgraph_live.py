"""Live integration glue: drive the dependency graph against the running agent
container. Re-certify each node via host checks (CERTIFY) and run the emit drain
loop (EMIT). Mutations go through build_agent.run_recipe; certification through a
read-only executor — keeping the host-owns-truth invariant (certify.py).

This is the ONLY module allowed to bridge python_deps.depgraph (pure) and
src.envstate (the agent loop).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from python_deps.depgraph.certify import certify_all
from python_deps.depgraph.emit import EMIT_ATTEMPT_TAG, build_recipe, partition, topo_order
from python_deps.depgraph.executor import CommandResult
from python_deps.depgraph.schema import Attempt
from src.envstate.world_model import RecipePatch, RecipeStep

if TYPE_CHECKING:
    from python_deps.depgraph.schema import DepGraph


class _ReadonlyExecAdapter:
    """Adapt the orchestrator's ``exec_readonly`` callable to the Executor protocol.

    ``certify_all`` only needs ``run(cmd).ok`` and ``.stderr``; check_commands are
    read-only presence checks (``command -v`` / ``ldconfig -p | grep`` /
    ``python -c import``), so the read-only path is the correct executor.
    """

    def __init__(self, exec_readonly: Callable[[str], tuple[int, str]]) -> None:
        self._f = exec_readonly

    def run(self, command: str, *, timeout: int = 300) -> CommandResult:
        rc, out = self._f(command)
        return CommandResult(command=command, returncode=rc, stdout=out, stderr=out)


def certify_refresh(
    graph,
    exec_readonly,
    cycle: int,
    *,
    allow_service_certify: bool | None = None,
):
    """Re-flip every node's state via a host check in the live container.

    No-op (returns the input) when the graph is empty/None or no read-only
    executor is available — so the feature degrades gracefully.

    When ``allow_service_certify`` is ``None`` (the default), the arm flag is
    resolved from the environment variable ``DOCKERAGENT_ENABLE_SERVICE_PROVISION``
    so the three existing call sites need no change.  Pass an explicit ``True``/
    ``False`` in tests to override the env lookup.
    """
    if graph is None or not graph.nodes or exec_readonly is None:
        return graph
    import os
    from python_deps.depgraph.certify import _SERVICE_LAYER_ORDER, _LAYER_ORDER
    if allow_service_certify is None:
        allow_service_certify = os.environ.get("DOCKERAGENT_ENABLE_SERVICE_PROVISION") == "1"
    order = _SERVICE_LAYER_ORDER if allow_service_certify else _LAYER_ORDER
    return certify_all(graph, _ReadonlyExecAdapter(exec_readonly), cycle=cycle,
                       allow_service_certify=allow_service_certify, layer_order=order)


def test_gate_soname_refresh(graph, exec_readonly, events, test_cmd):
    """Route the testability gate's failed output through ``test_gate_probe``.

    See docstring in the plan. The testability gate is the dlopen-tail oracle
    (design §3): only a soname surfaced by the repo's own test run reaches here.
    Filters ``events`` to the ones whose command IS ``test_cmd`` (the pytest
    gate) and feeds their combined output to ``test_gate_probe`` with the live
    read-only executor (``_ReadonlyExecAdapter``) so a dlopen-tail soname is
    apt-resolved. No-op (returns input) when graph/exec is absent. Immutable.
    """
    if graph is None or exec_readonly is None:
        return graph
    from python_deps.depgraph.probe import test_gate_probe
    executor = _ReadonlyExecAdapter(exec_readonly)
    new = graph
    for cmd, out in events:
        if cmd != test_cmd:
            continue
        new = test_gate_probe(new, executor, out or "", command=test_cmd)
    return new


# Its name matches pytest's default ``test_*`` collection pattern; mark it
# not-a-test so importing it into tests/envstate/test_test_gate_soname_refresh.py
# does not make pytest try to call it as a test function (missing-fixture error).
# Mirrors the same guard on ``test_gate_probe`` (python_deps/depgraph/probe.py).
test_gate_soname_refresh.__test__ = False


def ensure_python_shim(sandbox_execute) -> None:
    """Symlink ``python`` -> ``python3`` in the live container.

    The depgraph's check_commands invoke a bare ``python`` (e.g. ``python -m pip
    show <pkg>``). On a python3-only base that exits 127, so a successfully-installed
    node never certifies and the drain re-emits the same closure every cycle (the
    e2e-smoke certify loop). This normalizes the container to the standard python:3.x
    layout. Runs through the MUTATING ``sandbox_execute`` so the symlink persists;
    best-effort, never raises.

    Issued as a SINGLE setup mutation (a lone idempotent ``ln -sf``), NOT the
    earlier ``command -v python || ln -sf`` compound: ``sandbox_execute`` is
    preflight-gated, and the preflight rejects a command that combines multiple
    steps (the guard + the symlink) — a rejection would silently defeat the shim,
    leaving bare ``python`` unresolved and the certify loop churning
    (reset_to_base -> shim rejected -> reset, never certifying). ``ln -sf`` is
    already idempotent: it re-points an existing ``python`` symlink to ``python3``
    and creates it when absent, so the guard was redundant as well as rejected.
    """
    if sandbox_execute is None:
        return
    try:
        sandbox_execute('ln -sf "$(command -v python3)" /usr/local/bin/python')
    except Exception:  # noqa: BLE001 — best-effort; must never break the loop
        pass


def emit_drain(
    graph,
    build_agent,
    sandbox_execute,
    ledger,
    exec_readonly,
    *,
    step_offset: int,
    cycle: int,
    max_drain: int = 4,
):
    """Drain the certifiable closure: emit -> run -> re-certify, repeat.

    Each pass emits the current emittable set (apt then pip), runs it through the
    real ``build_agent.run_recipe`` (D4: repair is a free safety layer), records
    an emit Attempt per target node, then re-certifies against the live container.
    Certifying a toolchain unlocks the build-from-source package that needs it, so
    the next pass picks it up (D5). Bounded by ``max_drain``.

    Returns ``(new_graph, reports, steps_consumed)``.
    """
    reports: list = []
    steps_consumed = 0
    new = graph
    if new is None or not new.nodes:
        return new, reports, steps_consumed

    for _ in range(max_drain):
        part = partition(new)
        if not part.emittable:
            break
        ordered = topo_order(new, part.emittable)
        emit_steps = build_recipe(new, ordered)
        if not emit_steps:
            break

        recipe = RecipePatch(steps=tuple(
            RecipeStep(
                id=f"emit-{cycle}-{i}",
                kind=s.kind,
                command=s.command,
                target_node_ids=s.target_node_ids,
            )
            for i, s in enumerate(emit_steps)
        ))
        report = build_agent.run_recipe(
            recipe, sandbox_execute, ledger, step_offset=step_offset + steps_consumed
        )
        reports.append(report)
        steps_consumed += len(report.commands)

        outcome = "succeeded" if report.status == "done" else "failed"
        for s in emit_steps:
            for nid in s.target_node_ids:
                node = new.get(nid)
                if node is not None:
                    # check=EMIT_ATTEMPT_TAG lets partition's backoff count failed
                    # emits and demote a doomed node to the frontier (no re-emit loop).
                    new = new.with_node(
                        node.with_attempt(Attempt(
                            command=s.command, outcome=outcome, cycle=cycle,
                            check=EMIT_ATTEMPT_TAG,
                        ))
                    )

        new = certify_refresh(new, exec_readonly, cycle)

    return new, reports, steps_consumed


def repair_failed_nodes(
    graph,
    build_agent,
    sandbox_execute,
    ledger,
    exec_readonly,
    *,
    step_offset: int,
    cycle: int,
    repaired_ids: set,
    max_repair: int = 3,
    budget: int = 5,
):
    """Host-first repair of reciped nodes the batch wave could not certify.

    For each failed reciped node not already in ``repaired_ids`` (capped at
    ``max_repair`` per call), frame a one-node ``Task`` and run a bounded
    host-first repair: ``BuildAgent.run`` with ``check=node.check_command`` and
    ``budget=budget`` — the LLM only proposes commands, the HOST check is the
    stop. Re-certify after each. One repair per node per run (cross-cycle memory
    lives in the caller's ``repaired_ids`` set).

    Returns ``(new_graph, steps_consumed, repaired_count)``.
    """
    from python_deps.depgraph.emit import failed_reciped_nodes
    from src.envstate.world_model import Task

    steps = 0
    repaired = 0
    new = graph
    for node in failed_reciped_nodes(new):
        if repaired >= max_repair:
            break
        if node.id in repaired_ids:
            continue
        repaired_ids.add(node.id)
        task = Task(
            goal=f"Make the host check `{node.check_command}` succeed for "
                 f"{node.type.name} '{node.name}'. A batched install left it unsatisfied; "
                 f"read the error and provide whatever it needs (e.g. a system library).",
            done_when=node.check_command,
            layer=getattr(node.layer, "name", "pip").lower(),
            facts=(f"node: {node.id}",),
            target_node_ids=(node.id,),
        )
        report = build_agent.run(
            task, sandbox_execute, ledger, step_offset=step_offset + steps,
            check=node.check_command, budget=budget,
        )
        steps += len(report.commands)
        repaired += 1
        new = certify_refresh(new, exec_readonly, cycle)   # HOST flips state, not the LLM
    return new, steps, repaired
