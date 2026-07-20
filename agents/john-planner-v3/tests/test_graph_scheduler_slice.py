"""Task 5: packet_to_task renders RequirementSlice into Task.facts.

Tests that when a packet carries a requirement_slice, Task.facts contains the
rendered slice lines (e.g. `target:`, `providers:` lines) rather than the old
flat evidence/depends_on/certified_context facts.
"""
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from python_deps.depgraph.schema import DepGraph, Node, NodeType, Layer, State, DiscoveredBy
from python_deps.depgraph.schedule import frame_obligation
from src.envstate.graph_scheduler import packet_to_task


def _frontier_packet():
    g = DepGraph().with_node(Node(id="syslib:libxml2", type=NodeType.SYSTEM_LIB, name="libxml2",
        layer=Layer.SYSTEM, discovered_by=DiscoveredBy.PROBE, state=State.MISSING,
        check_command="pkg-config --exists libxml-2.0", chosen_fix="apt:libxml2-dev",
        fix_candidates=("apt:libxml2-dev",)))
    return frame_obligation(g, g.get("syslib:libxml2"))


def test_task_facts_are_the_rendered_slice():
    task = packet_to_task(_frontier_packet())
    blob = "\n".join(task.facts)
    assert any(f.startswith("target: syslib:libxml2") for f in task.facts)   # slice rendered
    assert "providers: candidates=[apt:libxml2-dev]" in blob
    assert task.done_when == "pkg-config --exists libxml-2.0"
    assert task.target_node_ids == ("syslib:libxml2",)
