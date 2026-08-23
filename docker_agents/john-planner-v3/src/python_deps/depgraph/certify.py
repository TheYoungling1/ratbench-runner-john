"""Stage 5 — host certification.

This realizes the certification invariant of design section 3.1: a node's
``state`` is flipped **only** by running its ``check_command`` on the host and
observing the exit code.  Install/import *actions* never imply ``satisfied`` —
only a passing check does.  State is therefore:

* **revocable** — re-certifying after a mutation can flip ``SATISFIED`` back to
  ``MISSING`` (a later install can break an earlier import; design 10.9);
* **host-issued** — nothing here is inferred from an action outcome.

``certify`` certifies one node; ``certify_all`` walks the graph in execution
layer order (design section 6): interpreter -> system -> toolchain -> pip ->
naming -> tests.  Every "mutation" returns a NEW ``DepGraph`` (repo immutability
rule).
"""

from __future__ import annotations

from dataclasses import replace

from python_deps.depgraph.executor import Executor
from python_deps.depgraph.schema import DepGraph, Layer, NodeType, State

# Execution layer priority (design section 6). Runtime joins the walk first: the
# interpreter minor is the platform floor every later layer assumes.
#
# Public: also consumed by build_script.py so the rendered artifact's section
# order never contradicts the order certify actually walks the graph in.
EXECUTION_LAYER_ORDER: tuple[Layer, ...] = (
    Layer.RUNTIME,
    Layer.INTERPRETER,
    Layer.SYSTEM,
    Layer.TOOLCHAIN,
    Layer.PIP,
    Layer.NAMING,
    Layer.CONFIG,
    Layer.TESTS,
)
_LAYER_ORDER = EXECUTION_LAYER_ORDER  # backwards-compat alias

# Services join the walk LAST (after the server binary/SystemLib is installed);
# only used on the live in-image path (arm v3). Never used off-arm.
_SERVICE_LAYER_ORDER: tuple[Layer, ...] = _LAYER_ORDER + (Layer.SERVICES,)


def certify(
    graph: DepGraph,
    node_id: str,
    executor: Executor,
    cycle: int = 0,
    *,
    allow_service_certify: bool = False,
) -> DepGraph:
    """Run one node's ``check_command`` and write its host-certified ``state``.

    * rc 0          -> ``SATISFIED`` with ``certified_cycle = cycle``;
    * rc != 0       -> ``MISSING`` with the check's stderr as ``evidence``;
    * no check_command -> left ``UNKNOWN`` (the host ran nothing).

    ``certified_cycle`` always records the cycle in which the host check was last
    actually run — including on the revocation path (SATISFIED -> MISSING) — so a
    consumer can distinguish "never certified" (``None``) from "certified then
    revoked" (the cycle of the failing re-check).

    Unknown ``node_id`` returns the graph unchanged.  Returns a NEW graph.

    SERVICE nodes are certified (loopback probe) only when ``allow_service_certify``
    is True AND the node carries a clean ``data["setup"]`` provisioning recipe (the
    CR6 setup shape) (design §4.3).  Off-arm / advisory services (no setup) stay
    UNKNOWN — the scratch container cannot host the daemon.

    For SERVICE nodes, ``certify`` also owns the anti-deadlock demote counter
    ``data["certify_fail_count"]``: a MISSING re-check increments it, a SATISFIED
    check resets it to 0.  A never-provisionable service therefore accrues a fail
    count and can be demoted rather than deadlocking the "done" gate.
    """
    node = graph.get(node_id)
    if node is None or not node.check_command:
        return graph
    # Services are reachability-certified only on the live in-image path (arm v3) and
    # only when they carry a clean setup recipe (data["setup"]). Off-arm / advisory:
    # allow_service_certify=False or setup absent -> stay UNKNOWN (design §4.3). The
    # scratch container cannot host the daemon, so the scratch certify_all call leaves
    # allow_service_certify=False.
    if node.type is NodeType.SERVICE:
        if not (allow_service_certify and node.data.get("setup") is not None):
            return graph

    result = executor.run(node.check_command)
    if result.ok:
        updated = node.with_state(State.SATISFIED, cycle=cycle)
    else:
        # Preserve the node's DISCOVERY evidence (the real build/import failure the
        # probe captured) — only fall back to the check's stderr when there is no
        # prior evidence. A presence check like ``ldconfig -p | grep`` or
        # ``command -v`` prints nothing on failure, so writing its empty stderr
        # would otherwise clobber the diagnostic line that explains WHY the need
        # exists. (design 3.1: certify owns ``state``, not the evidence of need.)
        evidence = node.evidence or result.stderr or None
        updated = node.with_state(State.MISSING, evidence=evidence, cycle=cycle)
    # Demote counter (must-verify invariant): SERVICE nodes only. A service that
    # comes up clears its fail count; one that stays down accrues one so it can
    # be demoted instead of deadlocking "done". Immutable data update via
    # ``replace`` (never mutate ``node.data`` in place); non-service nodes are
    # left byte-unchanged.
    if node.type is NodeType.SERVICE:
        if result.ok:
            fail_count = 0
        else:
            fail_count = node.data.get("certify_fail_count", 0) + 1
        updated = replace(
            updated, data={**dict(node.data), "certify_fail_count": fail_count}
        )
    return graph.with_node(updated)


def certify_all(
    graph: DepGraph,
    executor: Executor,
    cycle: int = 0,
    *,
    allow_service_certify: bool = False,
    layer_order: tuple[Layer, ...] = _LAYER_ORDER,
) -> DepGraph:
    """Certify every node in execution layer order (design section 6).

    Re-reads the evolving graph after each certification so revocation/ordering
    side effects compose.  Returns a NEW graph.

    Pass ``allow_service_certify=True`` and ``layer_order=_SERVICE_LAYER_ORDER``
    (via ``certify_refresh``) to also certify confirmed SERVICE nodes on the live
    in-image path (arm v3).
    """
    new = graph
    for layer in layer_order:
        node_ids = [n.id for n in new.nodes if n.layer is layer]
        for node_id in node_ids:
            new = certify(new, node_id, executor, cycle=cycle,
                          allow_service_certify=allow_service_certify)
    return new
