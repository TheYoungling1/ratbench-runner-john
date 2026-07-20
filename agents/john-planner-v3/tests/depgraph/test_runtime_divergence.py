import sys
from pathlib import Path
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph, Node, NodeType, Layer, State, DiscoveredBy,
)
from python_deps.depgraph.runtime_classify import Discovery  # noqa: E402
from python_deps.depgraph.runtime_ingest import diverged_node_ids  # noqa: E402


def _pkg(state):
    return Node(id="pkg:requests", type=NodeType.PACKAGE, name="requests",
                layer=Layer.PIP, discovered_by=DiscoveredBy.STATIC_SCAN,
                state=state, check_command="python3 -c 'import requests'")


def _disc():
    return Discovery(node_type=NodeType.PACKAGE, name="requests", layer=Layer.PIP,
                     evidence="ModuleNotFoundError: No module named 'requests'",
                     check_command="python3 -c 'import requests'")


def test_residual_mapping_to_satisfied_node_is_diverged():
    g = DepGraph().with_node(_pkg(State.SATISFIED))
    assert diverged_node_ids(g, [_disc()]) == ("pkg:requests",)


def test_residual_mapping_to_missing_node_is_not_diverged():
    g = DepGraph().with_node(_pkg(State.MISSING))
    assert diverged_node_ids(g, [_disc()]) == ()


def test_residual_with_no_matching_node_is_not_diverged():
    assert diverged_node_ids(DepGraph(), [_disc()]) == ()
