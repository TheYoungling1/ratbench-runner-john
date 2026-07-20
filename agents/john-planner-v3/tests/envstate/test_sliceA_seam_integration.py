"""Slice A deterministic chain end-to-end with a FakeExecutor (no Docker, no LLM):
emit phase   : graph(MISSING) -> block_emit (compose_script -> run_blocks -> certify + ledger dual-write)
artifact spine: certified graph -> compile_replay_blocks -> render_setup_sh."""
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from python_deps.depgraph.block import compile_replay_blocks
from python_deps.depgraph.schema import DepGraph, Node, NodeType, Layer, State, DiscoveredBy
from python_deps.depgraph.script import render_setup_sh
from src.envstate.block_emit import block_emit
from src.envstate.ledger import ActionLedger


def _graph():
    return DepGraph().with_node(Node(id="syslib:libpq.so", type=NodeType.SYSTEM_LIB,
        name="libpq.so", layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER,
        state=State.MISSING, check_command="ldconfig -p | grep -q libpq",
        chosen_fix="apt:libpq-dev"))


def test_block_emit_then_replay_artifact_spine():
    led = ActionLedger()
    graph, bundle, failed = block_emit(_graph(), lambda c: (True, "ok"),
                                       lambda c: (0, "") if "ldconfig" in c else (1, ""),
                                       led, cycle=1)
    assert failed is None and graph.get("syslib:libpq.so").state is State.SATISFIED
    assert any("libpq-dev" in e.cmd for e in led.events())          # dual-write happened

    # the artifact the finalizer emits for the CERTIFIED graph (replay, state-independent):
    spine = render_setup_sh(compile_replay_blocks(graph))
    assert "apt-get install -y --no-install-recommends libpq-dev" in spine
    assert "#@check ldconfig -p | grep -q libpq" in spine
    assert "SATISFIED" not in spine                                  # script carries no state
