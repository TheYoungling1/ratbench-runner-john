"""Design §16 invariants assertable in Phase 1."""
from python_deps.depgraph.block import compile_blocks
from python_deps.depgraph.script import render_setup_sh, parse_setup_sh
from python_deps.depgraph.schema import DepGraph, Node, NodeType, Layer, State, DiscoveredBy
from src.envstate.script_runner import run_blocks


def _g():
    g = DepGraph()
    return g.with_node(Node(id="syslib:libpq", type=NodeType.SYSTEM_LIB, name="libpq.so",
                            layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING,
                            check_command="ldconfig -p | grep -q libpq",
                            chosen_fix="apt:libpq-dev"))


def test_invariant2_block_success_is_not_node_truth():
    # block rc=0, host check fails -> node not SATISFIED
    blocks = compile_blocks(_g())
    g, _bundle, failed = run_blocks(blocks, lambda c: (True, "ok"),
                                    lambda c: (1, "absent"), _g(), cycle=1)
    assert failed is None
    assert g.get("syslib:libpq").state is not State.SATISFIED


def test_invariant_script_is_compiled_artifact_not_state():
    # the script is a pure projection of the graph; round-trips with no state inside it
    blocks = compile_blocks(_g())
    text = render_setup_sh(blocks)
    assert "SATISFIED" not in text and "state" not in text.lower()
    assert parse_setup_sh(text) == blocks
