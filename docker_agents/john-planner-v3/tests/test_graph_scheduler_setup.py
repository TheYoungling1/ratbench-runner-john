"""CR8 (Inc 4b): the graph-scheduler consumers handle the setup-shape Service AND
realize the anti-deadlock demote.

Two behaviours:
  * ``packet_to_task`` surfaces the clean ``setup`` recipe (install/start/bind/...).
  * ``next_decision``'s anti-hollow ``promoted_unsatisfied`` gate keys on ``setup``
    AND EXCLUDES a service that has failed certify 3× (``certify_fail_count >= 3``)
    — so a never-provisionable service demotes out and "done" becomes reachable
    (the must-verify invariant).
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_ROOT), str(_ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph, Node, NodeType, Layer, State, DiscoveredBy,
)
from python_deps.depgraph.schedule import ObligationPacket  # noqa: E402
from src.envstate.graph_scheduler import next_decision, packet_to_task  # noqa: E402
from src.envstate.constants import VERIFY_TEST_CMD  # noqa: E402


def _packet_with_setup(setup):
    return ObligationPacket(
        node_id="service:redis", node_type="Service", tier=5, layer="services",
        goal="bring up redis", evidence="", check_command="redis-cli ping",
        setup=setup,
    )


def test_packet_to_task_surfaces_setup():
    setup = {
        "install": ["apt-get update"],
        "start": "redis-server --daemonize yes",
        "bind": ["export CACHE_URL=redis://127.0.0.1:6379"],
        "createdb": None,
        "post": [],
    }
    task = packet_to_task(_packet_with_setup(setup))
    joined = "\n".join(task.facts)
    assert "redis-server --daemonize yes" in joined      # start line
    assert "apt-get update" in joined                    # install step
    assert "export CACHE_URL=redis://127.0.0.1:6379" in joined  # repoint/bind step


# ── anti-hollow gate: dual-shape re-key + the demote ─────────────────────────

def _setup_service(state=State.MISSING, fail_count=None):
    data = {
        "setup": {
            "install": ["apt-get install -y redis-server"],
            "start": "redis-server --daemonize yes",
            "probe": "redis-cli ping",
            "createdb": None,
            "post": [],
        },
        "service_kind": "redis",
    }
    if fail_count is not None:
        data["certify_fail_count"] = fail_count
    return Node(
        id="service:redis", type=NodeType.SERVICE, name="redis",
        layer=Layer.SERVICES, discovered_by=DiscoveredBy.STATIC_SCAN,
        state=state, check_command="redis-cli ping", data=data,
    )


# handed at the attempt_cap pushes the (otherwise actionable) service out of the
# eligible frontier so next_decision reaches the anti-hollow / done branch —
# exactly the loop state after the agent has exhausted its attempts.
_HANDED = {"service:redis": 3}


def test_setup_service_blocks_done_when_not_demoted():
    """A setup service still MISSING (certify_fail_count absent/0) keeps 'done'
    from firing — tests 'passing' over a down service is the anti-hollow trap."""
    g = DepGraph().with_node(_setup_service(state=State.MISSING))  # no certify_fail_count
    decision, chosen = next_decision(
        g, run_tests=lambda: True, handed=_HANDED, attempt_cap=3, allow_services=True,
    )
    assert decision.action == "task"                  # anti-hollow discover-task, NOT done
    assert decision.task.done_when == VERIFY_TEST_CMD
    assert chosen is None


def test_demoted_setup_service_lets_done_through():
    """THE INVARIANT: a setup service that failed certify 3× is excluded from
    promoted_unsatisfied → next_decision returns 'done' (no deadlock)."""
    g = DepGraph().with_node(_setup_service(state=State.MISSING, fail_count=3))
    decision, chosen = next_decision(
        g, run_tests=lambda: True, handed=_HANDED, attempt_cap=3, allow_services=True,
    )
    assert decision.action == "done"
    assert chosen is None
