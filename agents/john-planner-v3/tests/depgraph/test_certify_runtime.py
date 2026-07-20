from python_deps.depgraph.certify import certify_all
from python_deps.depgraph.executor import CommandResult
from python_deps.depgraph.ids import runtime_id
from python_deps.depgraph.schema import (
    DepGraph, Node, NodeType, Layer, State, DiscoveredBy,
)


def _runtime_graph(minor):
    check = f'python3 -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,{minor.split(".")[1]}) else 1)"'
    n = Node(id=runtime_id(minor), type=NodeType.RUNTIME, name=f"python {minor}",
             layer=Layer.RUNTIME, discovered_by=DiscoveredBy.STATIC_SCAN,
             state=State.UNKNOWN, version=minor, check_command=check)
    return DepGraph(nodes=(n,), edges=())


class _Ex:
    def __init__(self, rc): self.rc = rc
    def run(self, command, *, timeout=300):
        return CommandResult(command=command, returncode=self.rc, stdout="", stderr="x")


def test_runtime_certifies_satisfied_on_rc0():
    g = certify_all(_runtime_graph("3.10"), _Ex(0), cycle=1)
    assert g.get(runtime_id("3.10")).state is State.SATISFIED


def test_runtime_certifies_missing_on_rc1():
    g = certify_all(_runtime_graph("3.10"), _Ex(1), cycle=1)
    assert g.get(runtime_id("3.10")).state is State.MISSING
