"""Tests for runtime_ingest.ingest_runtime_failures (pure, no Docker)."""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.depgraph.ids import (
    TEST_NODE_ID, config_id, package_id, service_id, syslib_id, tool_id,
)
from python_deps.depgraph.runtime_classify import Discovery
from python_deps.depgraph.runtime_ingest import ingest_runtime_failures
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, EdgeType, Layer, Node, NodeType, State,
)


def _test_node() -> Node:
    return Node(
        id=TEST_NODE_ID,
        type=NodeType.TEST,
        name="repo_tests_pass",
        layer=Layer.TESTS,
        discovered_by=DiscoveredBy.GOAL,
    )


def _base_graph() -> DepGraph:
    return DepGraph().with_node(_test_node())


# ── append-new ───────────────────────────────────────────────────────────────

def test_append_new_package_node():
    graph = _base_graph()
    obs = [("python app.py", "ModuleNotFoundError: No module named 'yaml'")]
    new_graph, discoveries = ingest_runtime_failures(graph, obs)

    node = new_graph.get(package_id("PyYAML", None))
    assert node is not None
    assert node.type is NodeType.PACKAGE
    assert node.discovered_by is DiscoveredBy.RUNTIME
    assert node.check_command == 'python3 -c "import yaml"'
    assert node.data.get("runtime_confidence") == "runtime-deterministic"
    assert len(discoveries) == 1


def test_append_new_syslib_node():
    graph = _base_graph()
    obs = [("python app.py", "ImportError: libGL.so.1: cannot open shared object file")]
    new_graph, discoveries = ingest_runtime_failures(graph, obs)

    node = new_graph.get(syslib_id("libGL.so.1"))
    assert node is not None
    assert node.type is NodeType.SYSTEM_LIB
    assert node.discovered_by is DiscoveredBy.RUNTIME
    assert node.check_command == "ldconfig -p | grep -q libGL.so.1"


def test_append_new_tool_node():
    graph = _base_graph()
    obs = [("make all", "make: command not found")]
    new_graph, discoveries = ingest_runtime_failures(graph, obs)

    node = new_graph.get(tool_id("make"))
    assert node is not None
    assert node.type is NodeType.TOOL
    assert node.check_command == "command -v make"


def test_append_new_config_node():
    graph = _base_graph()
    obs = [("python app.py", "KeyError: 'DATABASE_URL'")]
    new_graph, discoveries = ingest_runtime_failures(graph, obs)

    node = new_graph.get(config_id("DATABASE_URL"))
    assert node is not None
    assert node.type is NodeType.CONFIG
    assert node.check_command == "printenv DATABASE_URL"


# ── service advisory (no check_command flip) ─────────────────────────────────

def test_append_service_node_advisory():
    graph = _base_graph()
    obs = [("python manage.py migrate", "psycopg2.OperationalError: could not connect to server")]
    new_graph, discoveries = ingest_runtime_failures(graph, obs)

    node = new_graph.get(service_id("postgres"))
    assert node is not None
    assert node.type is NodeType.SERVICE
    assert node.discovered_by is DiscoveredBy.RUNTIME
    # Services are advisory — check_command stays None (certify skip-guards them)
    assert node.check_command is None
    assert node.state is State.UNKNOWN


# ── edge attribution (Test --requires--> node, origin="runtime") ─────────────

def test_runtime_edge_hangs_off_test_node():
    graph = _base_graph()
    obs = [("python app.py", "ModuleNotFoundError: No module named 'yaml'")]
    new_graph, _ = ingest_runtime_failures(graph, obs)

    edges = [e for e in new_graph.edges
             if e.src == TEST_NODE_ID and e.relation is EdgeType.REQUIRES
             and e.origin == "runtime"]
    assert len(edges) == 1
    assert edges[0].dst == package_id("PyYAML", None)


# ── annotate-existing (idempotent across two passes) ─────────────────────────

def test_annotate_existing_node_is_idempotent():
    graph = _base_graph()
    obs = [("python app.py", "ModuleNotFoundError: No module named 'yaml'")]

    graph1, discoveries1 = ingest_runtime_failures(graph, obs)
    graph2, discoveries2 = ingest_runtime_failures(graph1, obs)

    # Same number of nodes both times — no duplicate appended
    assert len(graph2.nodes) == len(graph1.nodes)
    # Edge deduped — still exactly one runtime edge to the package
    runtime_edges = [e for e in graph2.edges
                     if e.src == TEST_NODE_ID and e.origin == "runtime"]
    assert len(runtime_edges) == 1
    # Both passes returned a discovery
    assert len(discoveries1) == 1
    assert len(discoveries2) == 1


def test_annotate_existing_sets_runtime_confidence():
    """A package already in the graph (static) gets runtime_confidence annotated."""
    existing = Node(
        id=package_id("PyYAML", None),
        type=NodeType.PACKAGE,
        name="PyYAML",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.STATIC_SCAN,
    )
    graph = _base_graph().with_node(existing)
    obs = [("python app.py", "ModuleNotFoundError: No module named 'yaml'")]
    new_graph, discoveries = ingest_runtime_failures(graph, obs)

    node = new_graph.get(package_id("PyYAML", None))
    assert node.data.get("runtime_confidence") == "runtime-deterministic"
    # discovered_by must not be silently downgraded
    # (runtime evidence is stronger; annotated node picks up RUNTIME provenance)
    assert node.discovered_by is DiscoveredBy.RUNTIME
    assert len(discoveries) == 1


# ── ignore-set produces no mutation ──────────────────────────────────────────

def test_no_matching_distribution_ignored():
    graph = _base_graph()
    obs = [("pip install flask", "No matching distribution found for flask==99.0")]
    new_graph, discoveries = ingest_runtime_failures(graph, obs)

    assert new_graph is graph or len(new_graph.nodes) == len(graph.nodes)
    assert discoveries == []


def test_assertion_error_ignored():
    graph = _base_graph()
    obs = [("python -m pytest", "AssertionError: assert 1 == 2")]
    new_graph, discoveries = ingest_runtime_failures(graph, obs)
    assert discoveries == []


# ── original graph never mutated (immutability) ──────────────────────────────

def test_original_graph_unchanged():
    graph = _base_graph()
    original_node_count = len(graph.nodes)
    obs = [("python app.py", "ModuleNotFoundError: No module named 'requests'")]
    ingest_runtime_failures(graph, obs)
    assert len(graph.nodes) == original_node_count


# ── T3a: per-observation classifier exception never bubbles ──────────────────

def test_classifier_exception_never_raises():
    """T3a: if a classifier raises, ingest must return (graph_unchanged, []) without raising."""
    graph = _base_graph()

    def _boom(cmd, out):
        raise RuntimeError("boom")

    new_graph, found = ingest_runtime_failures(
        graph,
        [("python app.py", "ModuleNotFoundError: No module named 'requests'")],
        classifiers=(_boom,),
    )
    assert found == []
    # Graph is unchanged (no nodes added beyond the original Test node)
    assert len(new_graph.nodes) == len(graph.nodes)


# ── C3: versioned static node annotated, no unversioned duplicate ─────────────

def test_versioned_static_package_annotated_no_duplicate():
    """C3: a runtime ModuleNotFoundError for 'yaml' must annotate the versioned
    static node pkg:PyYAML==6.0, not append a duplicate unversioned pkg:PyYAML."""
    versioned = Node(
        id=package_id("PyYAML", "6.0"),
        type=NodeType.PACKAGE,
        name="PyYAML",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.STATIC_SCAN,
    )
    graph = _base_graph().with_node(versioned)
    obs = [("python app.py", "ModuleNotFoundError: No module named 'yaml'")]
    new_graph, discoveries = ingest_runtime_failures(graph, obs)

    # (a) versioned node is annotated with runtime provenance and confidence
    annotated = new_graph.get(package_id("PyYAML", "6.0"))
    assert annotated is not None
    assert annotated.discovered_by is DiscoveredBy.RUNTIME
    assert annotated.data.get("runtime_confidence") == "runtime-deterministic"

    # (b) no unversioned duplicate was appended
    assert new_graph.get(package_id("PyYAML", None)) is None, (
        "must not append unversioned pkg:PyYAML when versioned pkg:PyYAML==6.0 exists"
    )

    # (c) node count unchanged (Test + one versioned pkg)
    assert len(new_graph.nodes) == len(graph.nodes)
    assert len(discoveries) == 1


# ── unresolved import (name=None) tolerance ────────────────────────────────

def test_find_existing_node_tolerates_none_name():
    import python_deps.depgraph.runtime_ingest as ri

    graph = DepGraph(nodes=(), edges=())
    disc = Discovery(
        node_type=NodeType.PACKAGE, name=None, layer=Layer.PIP,
        evidence="unknown import", check_command="python -c 'import mystery'",
    )
    # Must return None cleanly, not raise (previously raised inside a blanket except).
    assert ri._find_existing_node(graph, disc) is None


def test_annotate_or_append_skips_none_named_discovery():
    """A discovery with no resolvable package name (name=None) must mutate NOTHING —
    never fabricate a `pkg:None` node (plan invariant: unmapped import -> NO root)."""
    from python_deps.depgraph.runtime_ingest import _annotate_or_append
    graph = _base_graph()
    disc = Discovery(
        node_type=NodeType.PACKAGE, name=None, layer=Layer.PIP,
        evidence="unresolved import", check_command="python3 -c 'import mystery'",
    )
    out = _annotate_or_append(graph, disc)
    assert out.get("pkg:None") is None
    assert len(out.nodes) == len(graph.nodes)   # nothing appended
    assert len(out.edges) == len(graph.edges)   # no edge either
