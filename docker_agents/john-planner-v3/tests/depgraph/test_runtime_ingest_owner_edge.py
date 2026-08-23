import sys
from pathlib import Path
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph, Node, NodeType, Layer, State, DiscoveredBy, EdgeType,
)
from python_deps.depgraph.runtime_classify import Discovery  # noqa: E402
from python_deps.depgraph.runtime_ingest import _annotate_or_append  # noqa: E402
from python_deps.depgraph.ids import TEST_NODE_ID, syslib_id  # noqa: E402


def _test_node():
    return Node(id=TEST_NODE_ID, type=NodeType.TEST, name="repo_tests_pass",
                layer=Layer.TESTS, discovered_by=DiscoveredBy.GOAL)


def _psycopg2():
    return Node(id="pkg:psycopg2", type=NodeType.PACKAGE, name="psycopg2",
                layer=Layer.PIP, discovered_by=DiscoveredBy.STATIC_SCAN,
                state=State.SATISFIED, check_command="python3 -c 'import psycopg2'",
                version="2.9")


def _syslib_discovery(owner=None):
    return Discovery(node_type=NodeType.SYSTEM_LIB, name="libpq.so.5", layer=Layer.SYSTEM,
                     evidence="libpq.so.5: cannot open shared object",
                     check_command="ldconfig -p | grep -q libpq.so.5", requires_of=owner)


def _edge(graph, dst):
    return next((e for e in graph.edges
                 if e.dst == dst and e.relation is EdgeType.REQUIRES), None)


def test_requires_of_owner_present_hangs_edge_on_culprit():
    g = DepGraph().with_node(_test_node()).with_node(_psycopg2())
    out = _annotate_or_append(g, _syslib_discovery(owner="pkg:psycopg2"))
    e = _edge(out, syslib_id("libpq.so.5"))
    assert e is not None and e.src == "pkg:psycopg2"   # culprit, not Test


def test_owner_param_overrides_and_hangs_on_culprit():
    g = DepGraph().with_node(_test_node()).with_node(_psycopg2())
    out = _annotate_or_append(g, _syslib_discovery(owner=None), owner_node_id="pkg:psycopg2")
    e = _edge(out, syslib_id("libpq.so.5"))
    assert e is not None and e.src == "pkg:psycopg2"


def test_owner_absent_from_graph_falls_back_to_test():
    g = DepGraph().with_node(_test_node())            # no psycopg2 node
    out = _annotate_or_append(g, _syslib_discovery(owner="pkg:psycopg2"))
    e = _edge(out, syslib_id("libpq.so.5"))
    assert e is not None and e.src == TEST_NODE_ID     # safe fallback


def test_no_owner_defaults_to_test():
    g = DepGraph().with_node(_test_node())
    out = _annotate_or_append(g, _syslib_discovery(owner=None))
    e = _edge(out, syslib_id("libpq.so.5"))
    assert e is not None and e.src == TEST_NODE_ID     # existing behavior preserved


def test_non_package_owner_falls_back_to_test_not_dropped():
    # The LLM may name a SYSTEM_LIB owner (e.g. "libssl needed by libpq"). A
    # SystemLib is NOT a legal requires-src, so an unguarded with_edge would raise
    # and ingest would silently drop the discovery. The guard must fall back to Test
    # AND still create the node + edge (nothing dropped).
    libpq = Node(id="syslib:libpq.so.5", type=NodeType.SYSTEM_LIB, name="libpq.so.5",
                 layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RUNTIME,
                 state=State.MISSING, check_command="ldconfig -p | grep -q libpq.so.5")
    g = DepGraph().with_node(_test_node()).with_node(libpq)
    d = Discovery(node_type=NodeType.SYSTEM_LIB, name="libssl.so.3", layer=Layer.SYSTEM,
                  evidence="libssl.so.3: cannot open shared object",
                  check_command="ldconfig -p | grep -q libssl.so.3",
                  requires_of="syslib:libpq.so.5")            # illegal requires-src
    out = _annotate_or_append(g, d)
    assert out.get(syslib_id("libssl.so.3")) is not None      # node NOT dropped
    e = _edge(out, syslib_id("libssl.so.3"))
    assert e is not None and e.src == TEST_NODE_ID            # safe fallback, no raise
