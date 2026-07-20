"""Tests for the PURE scheduling layer's SERVICE gating.

A SERVICE is actionable only when it carries a clean ``data["setup"]`` recipe and
services are armed; an advisory service (no setup) is never scheduled. The
setup-shape actionability/framing is covered by ``test_schedule_setup.py``.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_inferred_service_never_actionable():
    """A SERVICE without a setup recipe is never scheduled (advisory-only)."""
    from python_deps.depgraph.schema import (
        DepGraph, Node, NodeType, Layer, DiscoveredBy, State,
    )
    from python_deps.depgraph.schedule import scheduler_frontier
    svc = Node(id="service:redis", type=NodeType.SERVICE, name="redis",
               layer=Layer.SERVICES, discovered_by=DiscoveredBy.STATIC_SCAN,
               state=State.MISSING, check_command="redis-cli ping",
               data={})   # no setup recipe
    g = DepGraph().with_node(svc)
    assert scheduler_frontier(g, allow_services=True) == ()
