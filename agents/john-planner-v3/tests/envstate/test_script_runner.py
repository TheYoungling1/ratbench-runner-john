from python_deps.depgraph.block import Block
from python_deps.depgraph.schema import DepGraph, Node, NodeType, Layer, State, DiscoveredBy
from src.envstate.script_runner import run_blocks


def _graph():
    g = DepGraph()
    g = g.with_node(Node(id="syslib:libpq", type=NodeType.SYSTEM_LIB, name="libpq.so",
                         layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING,
                         check_command="ldconfig -p | grep -q libpq", chosen_fix="apt:libpq-dev"))
    return g


_BLOCKS = (
    Block(block_id="system.libpq", wave="system",
          commands=("apt-get install -y --no-install-recommends libpq-dev",),
          target_node_ids=("syslib:libpq",), check_commands=("ldconfig -p | grep -q libpq",)),
    Block(block_id="system.second", wave="system", commands=("echo two",),
          target_node_ids=("syslib:libpq",)),
)


def _exec_ok(cmd):                       # mutating exec: (ok, output)
    return True, "installed"


def test_one_evidence_per_block_and_certify():
    # read-only exec: the check passes -> node becomes SATISFIED
    def ro(cmd):
        return (0, "libpq found") if "ldconfig" in cmd else (1, "")
    graph, bundle, failed = run_blocks(_BLOCKS, _exec_ok, ro, _graph(), cycle=1)
    assert failed is None
    assert len(bundle.items) == 2 and all(e.container_kind == "canonical" for e in bundle.items)
    assert graph.get("syslib:libpq").state is State.SATISFIED


def test_stops_on_first_failed_block():
    calls = []
    def exec_fail_first(cmd):
        calls.append(cmd)
        return (False, "E: package not found") if "libpq-dev" in cmd else (True, "")
    def ro(cmd):
        return (1, "")                  # never satisfied
    graph, bundle, failed = run_blocks(_BLOCKS, exec_fail_first, ro, _graph(), cycle=1)
    assert failed == "system.libpq"
    assert not any("echo two" in c for c in calls)        # block 2 never ran
    assert bundle.items[-1].rc != 0


def test_block_rc0_does_not_certify_without_check_pass():
    # block succeeds (rc 0) but the host check fails -> node stays MISSING (invariant #2/#3)
    def ro(cmd):
        return (1, "not found")
    graph, bundle, failed = run_blocks(_BLOCKS, _exec_ok, ro, _graph(), cycle=1)
    assert graph.get("syslib:libpq").state is not State.SATISFIED
