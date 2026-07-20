from python_deps.depgraph.block import Block, compile_blocks
from python_deps.depgraph.schema import DepGraph, Node, NodeType, Layer, State, DiscoveredBy


def test_block_fields_default():
    b = Block(block_id="sys.libpq", wave="system", commands=("apt-get install -y libpq-dev",),
              target_node_ids=("syslib:libpq",))
    assert b.can_batch is False
    assert b.mutates_env is True
    assert b.provider_ids == () and b.check_commands == ()


def _g():
    g = DepGraph()
    g = g.with_node(Node(id="syslib:libpq", type=NodeType.SYSTEM_LIB, name="libpq.so",
                         layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING,
                         check_command="ldconfig -p | grep -q libpq", chosen_fix="apt:libpq-dev"))
    g = g.with_node(Node(id="pkg:psycopg2", type=NodeType.PACKAGE, name="psycopg2",
                         layer=Layer.PIP, discovered_by=DiscoveredBy.RESOLVER,
                         state=State.MISSING, version="2.9.9",
                         check_command="python -m pip show psycopg2"))
    return g


def test_one_block_per_node_topo_order():
    blocks = compile_blocks(_g())
    assert len(blocks) == 2
    # system wave before python wave
    assert blocks[0].target_node_ids == ("syslib:libpq",)
    assert blocks[1].target_node_ids == ("pkg:psycopg2",)
    # command + annotations populated from the node
    assert "libpq-dev" in blocks[0].commands[0]
    assert blocks[0].provider_ids == ("apt:libpq-dev",)
    assert blocks[0].check_commands == ("ldconfig -p | grep -q libpq",)
    assert "psycopg2==2.9.9" in blocks[1].commands[0]
    assert blocks[1].check_commands == ("python -m pip show psycopg2",)
