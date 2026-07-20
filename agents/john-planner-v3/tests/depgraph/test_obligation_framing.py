# tests/depgraph/test_obligation_framing.py
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph, Node, Edge, NodeType, Layer, State, EdgeType, DiscoveredBy,
)
from python_deps.depgraph.schedule import frame_obligation, ObligationPacket  # noqa: E402


def _node(nid, ntype, name, state, *, check="true", evidence="", layer=Layer.SYSTEM):
    return Node(
        id=nid, type=ntype, name=name, layer=layer,
        discovered_by=DiscoveredBy.RUNTIME, state=state,
        check_command=check, evidence=evidence,
    )


def test_packet_carries_node_identity_check_and_rich_goal():
    g = DepGraph().with_node(
        _node("syslib:libpq", NodeType.SYSTEM_LIB, "libpq", State.MISSING,
              check="ldconfig -p | grep libpq", evidence="ImportError: libpq.so.5")
    )
    pkt = frame_obligation(g, g.get("syslib:libpq"))
    assert isinstance(pkt, ObligationPacket)
    assert pkt.node_id == "syslib:libpq"
    assert pkt.check_command == "ldconfig -p | grep libpq"
    assert "libpq.so.5" in pkt.evidence
    assert pkt.node_type == NodeType.SYSTEM_LIB.value
    assert pkt.layer == Layer.SYSTEM.value
    # goal is a real instruction, not just the bare name
    assert "libpq" in pkt.goal
    assert pkt.check_command in pkt.goal


def test_packet_carries_dependency_and_certified_context():
    g = (
        DepGraph()
        .with_node(_node("syslib:libpq", NodeType.SYSTEM_LIB, "libpq", State.SATISFIED))
        .with_node(_node("pkg:psycopg2", NodeType.PACKAGE, "psycopg2", State.MISSING, layer=Layer.PIP))
        .with_edge(Edge(src="pkg:psycopg2", dst="syslib:libpq", relation=EdgeType.REQUIRES))
    )
    pkt = frame_obligation(g, g.get("pkg:psycopg2"))
    assert "syslib:libpq" in pkt.depends_on
    assert "syslib:libpq" in pkt.certified_context
