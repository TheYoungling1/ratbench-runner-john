import sys
from pathlib import Path
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.depgraph.schema import DepGraph, Node, NodeType, Layer, State, DiscoveredBy
from python_deps.depgraph.schedule import frame_obligation


def _g():
    g = DepGraph().with_node(Node(id="syslib:libxml2", type=NodeType.SYSTEM_LIB, name="libxml2",
        layer=Layer.SYSTEM, discovered_by=DiscoveredBy.PROBE, state=State.MISSING,
        check_command="pkg-config --exists libxml-2.0", chosen_fix="apt:libxml2-dev",
        fix_candidates=("apt:libxml2-dev",)))
    return g


def test_frame_obligation_attaches_requirement_slice():
    g = _g()
    pkt = frame_obligation(g, g.get("syslib:libxml2"))
    assert pkt.requirement_slice is not None
    assert pkt.requirement_slice.node_id == "syslib:libxml2"
    assert pkt.requirement_slice.providers.chosen == "apt:libxml2-dev"
