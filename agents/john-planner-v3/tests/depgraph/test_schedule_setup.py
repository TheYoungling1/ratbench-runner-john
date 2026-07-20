"""CR8 (Inc 4b): the PURE scheduling layer accepts the clean setup-shape Service.

A Service carrying ``data["setup"]`` (the CR6 clean provisioning recipe) is
actionable and framed; a Service without a setup recipe is never scheduled.
schedule.py must stay PURE (no src.envstate import).
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for p in (str(_ROOT), str(_ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph, Node, NodeType, Layer, State, DiscoveredBy,
)
from python_deps.depgraph.schedule import _is_actionable, frame_obligation  # noqa: E402


_SETUP = {
    "install": ["apt-get update", "apt-get install -y redis-server"],
    "start": "redis-server --daemonize yes",
    "probe": "redis-cli ping",
    "createdb": None,
    "post": [],
}


def _setup_service(state=State.MISSING):
    return Node(
        id="service:redis", type=NodeType.SERVICE, name="redis",
        layer=Layer.SERVICES, discovered_by=DiscoveredBy.STATIC_SCAN,
        state=state, check_command="redis-cli ping",
        data={"setup": _SETUP, "service_kind": "redis"},
    )


def test_setup_service_actionable():
    """A setup Service (data['setup'] present) is actionable with services armed —
    no service_confidence needed."""
    svc = _setup_service()
    g = DepGraph().with_node(svc)
    assert _is_actionable(g, svc, allow_services=True) is True


def test_setup_service_excluded_off_arm():
    """Sanity: the setup disjunct is gated on allow_services (off-arm → not actionable)."""
    svc = _setup_service()
    g = DepGraph().with_node(svc)
    assert _is_actionable(g, svc, allow_services=False) is False


def test_frame_obligation_carries_setup():
    """frame_obligation lifts data['setup'] onto the packet."""
    svc = _setup_service()
    g = DepGraph().with_node(svc)
    packet = frame_obligation(g, svc)
    assert packet.setup == _SETUP
