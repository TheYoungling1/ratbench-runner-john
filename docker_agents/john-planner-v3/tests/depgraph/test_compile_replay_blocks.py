import sys
from pathlib import Path
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.depgraph.block import compile_blocks, compile_replay_blocks
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Edge, EdgeType, Layer, Node, NodeType, State,
)


def _satisfied_syslib():
    return DepGraph().with_node(Node(id="syslib:libpq.so", type=NodeType.SYSTEM_LIB,
        name="libpq.so", layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER,
        state=State.SATISFIED, check_command="ldconfig -p | grep -q libpq",
        chosen_fix="apt:libpq-dev"))


def test_replay_includes_satisfied_node_emit_excludes_it():
    g = _satisfied_syslib()
    # the emit-phase compiler emits nothing (node is already certified) ...
    assert compile_blocks(g) == ()
    # ... but the replay compiler reproduces the install spine regardless of state.
    blocks = compile_replay_blocks(g)
    assert [b.block_id for b in blocks] == ["system.libpq.so"]
    assert blocks[0].commands == ("apt-get update && apt-get install -y --no-install-recommends libpq-dev",)
    assert blocks[0].check_commands == ("ldconfig -p | grep -q libpq",)


def test_replay_orders_system_before_pip_dep():
    g = DepGraph()
    g = g.with_node(Node(id="syslib:libpq.so", type=NodeType.SYSTEM_LIB, name="libpq.so",
        layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER, state=State.SATISFIED,
        check_command="ldconfig -p | grep -q libpq", chosen_fix="apt:libpq-dev"))
    g = g.with_node(Node(id="pkg:psycopg2==2.9.9", type=NodeType.PACKAGE, name="psycopg2",
        layer=Layer.PIP, discovered_by=DiscoveredBy.RESOLVER, state=State.SATISFIED,
        version="2.9.9", check_command="python3 -m pip show psycopg2"))
    g = g.with_edge(Edge(src="pkg:psycopg2==2.9.9", dst="syslib:libpq.so",
        relation=EdgeType.REQUIRES))
    ids = [b.block_id for b in compile_replay_blocks(g)]
    assert ids.index("system.libpq.so") < ids.index("pip.psycopg2==2.9.9")


def test_replay_skips_nodes_without_install_command():
    # a TEST-goal node is not _is_reciped -> no block
    g = DepGraph().with_node(Node(id="test:repo_tests_pass", type=NodeType.TEST, name="tests",
        layer=Layer.TESTS, discovered_by=DiscoveredBy.GOAL, state=State.SATISFIED))
    assert compile_replay_blocks(g) == ()
