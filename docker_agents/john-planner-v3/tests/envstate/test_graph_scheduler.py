"""Tests for packet_to_task rendering and the anti-hollow done gate.

Verifies packet_to_task adds no service facts for a non-service obligation, and
that next_decision returns 'done' for a graph with no provisionable service. The
setup-shape rendering + done-gate is covered by tests/test_graph_scheduler_setup.py.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_done_unchanged_for_non_service_graph():
    from python_deps.depgraph.schema import DepGraph
    from src.envstate.graph_scheduler import next_decision
    decision, _ = next_decision(DepGraph(), run_tests=lambda: True, allow_services=True)
    assert decision.action == "done"


def test_packet_to_task_no_service_facts_for_non_service():
    """packet_to_task adds no 'start the service' fact for a non-service obligation."""
    from python_deps.depgraph.schema import (
        DepGraph, Node, NodeType, Layer, DiscoveredBy, State,
    )
    from python_deps.depgraph.schedule import frame_obligation
    from src.envstate.graph_scheduler import packet_to_task
    node = Node(id="pkg:requests", type=NodeType.PACKAGE, name="requests",
                layer=Layer.PIP, discovered_by=DiscoveredBy.STATIC_SCAN,
                state=State.MISSING, check_command="python -c 'import requests'")
    g = DepGraph().with_node(node)
    packet = frame_obligation(g, node)
    task = packet_to_task(packet)
    # No fact should mention "start the service"
    assert not any("start the service" in f for f in task.facts)


def test_packet_to_task_no_binding_facts_without_setup():
    """A non-service obligation (no setup recipe) renders no binding/repoint facts."""
    from python_deps.depgraph.schema import (
        DepGraph, Node, NodeType, Layer, DiscoveredBy, State,
    )
    from python_deps.depgraph.schedule import frame_obligation
    from src.envstate.graph_scheduler import packet_to_task
    node = Node(id="pkg:requests", type=NodeType.PACKAGE, name="requests",
                layer=Layer.PIP, discovered_by=DiscoveredBy.STATIC_SCAN,
                state=State.MISSING, check_command="python -c 'import requests'")
    g = DepGraph().with_node(node)
    task = packet_to_task(frame_obligation(g, node))
    assert not any("ALTER USER" in f for f in task.facts)
    assert not any("/etc/profile.d" in f for f in task.facts)
