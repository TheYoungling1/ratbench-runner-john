"""Task 1 — graph model: schema.py + ids.py."""

from __future__ import annotations

import pytest

from python_deps.depgraph import ids
from python_deps.depgraph.schema import (
    Attempt,
    DepGraph,
    DiscoveredBy,
    Edge,
    EdgeType,
    Layer,
    Node,
    NodeType,
    State,
)


def make_node(
    node_id: str,
    node_type: NodeType,
    name: str,
    layer: Layer,
    discovered_by: DiscoveredBy = DiscoveredBy.STATIC_SCAN,
) -> Node:
    return Node(
        id=node_id,
        type=node_type,
        name=name,
        layer=layer,
        discovered_by=discovered_by,
    )


def test_node_defaults():
    node = make_node("import:cv2", NodeType.IMPORT, "cv2", Layer.NAMING)
    assert node.state is State.UNKNOWN
    assert node.version is None
    assert node.fix_candidates == ()
    assert node.attempts == ()
    assert node.certified_cycle is None


def test_with_state_returns_new_instance_and_leaves_original():
    node = make_node("syslib:libGL.so.1", NodeType.SYSTEM_LIB, "libGL.so.1", Layer.SYSTEM)
    updated = node.with_state(State.SATISFIED, evidence="ldconfig ok", cycle=4)

    assert updated is not node
    assert updated.state is State.SATISFIED
    assert updated.evidence == "ldconfig ok"
    assert updated.certified_cycle == 4
    # original untouched (immutability)
    assert node.state is State.UNKNOWN
    assert node.evidence is None
    assert node.certified_cycle is None


def test_with_state_without_optionals_preserves_fields():
    node = make_node("import:cv2", NodeType.IMPORT, "cv2", Layer.NAMING).with_state(
        State.MISSING, evidence="boom"
    )
    flipped = node.with_state(State.SATISFIED)
    assert flipped.state is State.SATISFIED
    # evidence not cleared when not passed
    assert flipped.evidence == "boom"
    assert flipped.certified_cycle is None


def test_with_attempt_appends_and_is_immutable():
    node = make_node("pkg:numpy", NodeType.PACKAGE, "numpy", Layer.PIP)
    attempt = Attempt(command="pip install numpy", outcome="succeeded", cycle=2)
    updated = node.with_attempt(attempt)

    assert updated.attempts == (attempt,)
    assert node.attempts == ()
    assert updated is not node


def test_with_node_adds_then_replaces_by_id():
    graph = DepGraph()
    n1 = make_node("import:cv2", NodeType.IMPORT, "cv2", Layer.NAMING)
    graph = graph.with_node(n1)
    assert len(graph.nodes) == 1
    assert graph.get("import:cv2") is n1

    n1b = n1.with_state(State.SATISFIED)
    graph2 = graph.with_node(n1b)
    assert len(graph2.nodes) == 1  # replaced, not duplicated
    assert graph2.get("import:cv2").state is State.SATISFIED
    # original graph unchanged
    assert graph.get("import:cv2").state is State.UNKNOWN


def test_with_edge_dedups_by_src_dst_relation():
    test_node = make_node(
        ids.TEST_NODE_ID, NodeType.TEST, "repo_tests_pass", Layer.TESTS, DiscoveredBy.GOAL
    )
    imp = make_node("import:cv2", NodeType.IMPORT, "cv2", Layer.NAMING)
    graph = DepGraph().with_node(test_node).with_node(imp)

    edge = Edge(src=ids.TEST_NODE_ID, dst="import:cv2")
    graph = graph.with_edge(edge)
    graph = graph.with_edge(Edge(src=ids.TEST_NODE_ID, dst="import:cv2"))  # dup
    assert len(graph.edges) == 1


def test_requires_of_and_required_by():
    test_node = make_node(
        ids.TEST_NODE_ID, NodeType.TEST, "repo_tests_pass", Layer.TESTS, DiscoveredBy.GOAL
    )
    imp = make_node("import:cv2", NodeType.IMPORT, "cv2", Layer.NAMING)
    pkg = make_node("pkg:opencv-python", NodeType.PACKAGE, "opencv-python", Layer.PIP)
    graph = (
        DepGraph()
        .with_node(test_node)
        .with_node(imp)
        .with_node(pkg)
        .with_edge(Edge(src=ids.TEST_NODE_ID, dst="import:cv2"))
        .with_edge(Edge(src="import:cv2", dst="pkg:opencv-python"))
    )

    succ = graph.requires_of("import:cv2")
    assert [n.id for n in succ] == ["pkg:opencv-python"]

    preds = graph.required_by("import:cv2")
    assert [n.id for n in preds] == [ids.TEST_NODE_ID]


def test_edge_rules_reject_illegal_requires():
    # SystemLib -> Package is not a legal `requires` edge.
    syslib = make_node(
        "syslib:libGL.so.1", NodeType.SYSTEM_LIB, "libGL.so.1", Layer.SYSTEM
    )
    pkg = make_node("pkg:opencv-python", NodeType.PACKAGE, "opencv-python", Layer.PIP)
    graph = DepGraph().with_node(syslib).with_node(pkg)

    with pytest.raises(ValueError):
        graph.with_edge(Edge(src="syslib:libGL.so.1", dst="pkg:opencv-python"))


def test_edge_rules_reject_unknown_nodes():
    graph = DepGraph()
    with pytest.raises(ValueError):
        graph.with_edge(Edge(src="import:nope", dst="pkg:nope"))


def test_alternative_to_edge_rejects_unknown_nodes():
    # alternative_to is a reserved relation with no EDGE_RULES entry, but it
    # must still reject dangling edges rather than returning early.
    g = DepGraph()
    with pytest.raises(ValueError):
        g.with_edge(Edge(src="import:fake1", dst="import:fake2",
                         relation=EdgeType.ALTERNATIVE_TO))


def test_edge_rules_reject_illegal_destination():
    # Package is a legal `requires` source, but Test is NOT a legal destination
    # (Test ∉ allowed_dst). Exercises the dst-rejection branch specifically.
    pkg = make_node("pkg:opencv-python", NodeType.PACKAGE, "opencv-python", Layer.PIP)
    test_node = make_node(
        ids.TEST_NODE_ID, NodeType.TEST, "repo_tests_pass", Layer.TESTS, DiscoveredBy.GOAL
    )
    graph = DepGraph().with_node(pkg).with_node(test_node)

    with pytest.raises(ValueError) as excinfo:
        graph.with_edge(Edge(src="pkg:opencv-python", dst=ids.TEST_NODE_ID))
    assert "destination" in str(excinfo.value)


def test_with_edge_returns_new_graph():
    imp = make_node("import:cv2", NodeType.IMPORT, "cv2", Layer.NAMING)
    pkg = make_node("pkg:opencv-python", NodeType.PACKAGE, "opencv-python", Layer.PIP)
    graph = DepGraph().with_node(imp).with_node(pkg)

    out = graph.with_edge(Edge(src="import:cv2", dst="pkg:opencv-python"))
    assert out is not graph
    assert out.edges != graph.edges
    assert graph.edges == ()  # original untouched (immutability)


def test_without_node_removes_node_and_touching_edges():
    imp = make_node("import:x", NodeType.IMPORT, "x", Layer.NAMING)
    ghost = make_node("pkg:ghost", NodeType.PACKAGE, "ghost", Layer.PIP)
    real = make_node("pkg:real", NodeType.PACKAGE, "real", Layer.PIP)
    graph = (
        DepGraph()
        .with_node(imp)
        .with_node(ghost)
        .with_node(real)
        .with_edge(Edge(src="import:x", dst="pkg:ghost", relation=EdgeType.REQUIRES))
        .with_edge(Edge(src="import:x", dst="pkg:real", relation=EdgeType.REQUIRES))
    )

    out = graph.without_node("pkg:ghost")

    assert out.get("pkg:ghost") is None
    assert out.get("pkg:real") is not None
    assert all(e.src != "pkg:ghost" and e.dst != "pkg:ghost" for e in out.edges)
    assert any(e.dst == "pkg:real" for e in out.edges)  # untouched edge kept
    assert graph.get("pkg:ghost") is not None  # original untouched (immutability)


def test_edge_rules_allow_legal_requires():
    imp = make_node("import:cv2", NodeType.IMPORT, "cv2", Layer.NAMING)
    pkg = make_node("pkg:opencv-python", NodeType.PACKAGE, "opencv-python", Layer.PIP)
    graph = DepGraph().with_node(imp).with_node(pkg)
    graph = graph.with_edge(Edge(src="import:cv2", dst="pkg:opencv-python"))
    assert len(graph.edges) == 1


def test_to_dict_round_trips_enums_to_value():
    node = Node(
        id="syslib:libGL.so.1",
        type=NodeType.SYSTEM_LIB,
        name="libGL.so.1",
        layer=Layer.SYSTEM,
        discovered_by=DiscoveredBy.PROBE,
        state=State.SATISFIED,
        fix_candidates=("apt:libgl1",),
        attempts=(Attempt(command="apt-get install -y libgl1", outcome="succeeded", cycle=4),),
    )
    edge = Edge(src="pkg:opencv-python", dst="syslib:libGL.so.1", origin="probe")
    graph = DepGraph(nodes=(node,), edges=(edge,))

    data = graph.to_dict()
    nd = data["nodes"][0]
    assert nd["type"] == "SystemLib"
    assert nd["layer"] == "system"
    assert nd["discovered_by"] == "probe"
    assert nd["state"] == "satisfied"
    assert nd["fix_candidates"] == ["apt:libgl1"]
    assert nd["attempts"][0]["outcome"] == "succeeded"

    ed = data["edges"][0]
    assert ed["relation"] == "requires"
    assert ed["origin"] == "probe"
    # all enum values are plain strings (json-safe)
    assert isinstance(nd["type"], str)


def test_default_edge_relation_is_requires():
    assert Edge(src="a", dst="b").relation is EdgeType.REQUIRES


def test_runtime_node_construction_and_to_dict():
    # The 6th node type: Runtime. Construct it and round-trip its enums to values.
    node = Node(
        id="runtime:DATABASE_URL",
        type=NodeType.RUNTIME,
        name="DATABASE_URL",
        layer=Layer.RUNTIME,
        discovered_by=DiscoveredBy.RUNTIME,
        state=State.MISSING,
        check_command='python -c "import os; assert os.environ.get(\'DATABASE_URL\')"',
        evidence="KeyError: DATABASE_URL",
        fix_candidates=("env:DATABASE_URL=sqlite:///test.db",),
    )
    nd = node.to_dict()
    assert nd["type"] == "Runtime"
    assert nd["layer"] == "runtime"
    assert nd["discovered_by"] == "runtime"
    assert nd["state"] == "missing"


# --- ids.py ---


def test_id_constructors():
    assert ids.import_id("cv2") == "import:cv2"
    assert ids.package_id("opencv-python", "4.9.0.80") == "pkg:opencv-python==4.9.0.80"
    assert ids.package_id("opencv-python", None) == "pkg:opencv-python"
    assert ids.syslib_id("libGL.so.1") == "syslib:libGL.so.1"
    assert ids.tool_id("pg_config") == "tool:pg_config"
    assert ids.TEST_NODE_ID == "test:repo_tests_pass"


def test_slug_sanitizes_whitespace_and_keeps_safe_chars():
    assert ids.slug("  hello world ") == "hello-world"
    assert ids.slug("opencv-python==4.9.0.80") == "opencv-python==4.9.0.80"
    assert ids.slug("a/b:c") == "a-b-c"


# --- uv-enrichment schema additions ---


def test_node_native_risk_fields_default_none():
    node = make_node("pkg:numpy", NodeType.PACKAGE, "numpy", Layer.PIP)
    assert node.build_from_source is None
    assert node.artifact is None
    assert node.hash is None
    assert node.resolved_python is None
    assert node.resolved_platform is None


def test_node_native_risk_fields_round_trip():
    node = Node(
        id="pkg:opencv-python==4.9.0.80",
        type=NodeType.PACKAGE,
        name="opencv-python",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
        version="4.9.0.80",
        build_from_source=True,
        artifact="opencv-python-4.9.0.80.tar.gz",
        hash="sha256:deadbeef",
        resolved_python="3.11",
        resolved_platform="aarch64-manylinux_2_28",
    )
    nd = node.to_dict()
    assert nd["build_from_source"] is True
    assert nd["artifact"] == "opencv-python-4.9.0.80.tar.gz"
    assert nd["hash"] == "sha256:deadbeef"
    assert nd["resolved_python"] == "3.11"
    assert nd["resolved_platform"] == "aarch64-manylinux_2_28"


def test_node_native_risk_fields_immutable_via_replace():
    from dataclasses import replace

    node = make_node("pkg:numpy", NodeType.PACKAGE, "numpy", Layer.PIP)
    updated = replace(node, build_from_source=False)
    assert updated.build_from_source is False
    assert node.build_from_source is None  # original untouched


def test_edge_marker_and_data_default():
    edge = Edge(src="a", dst="b")
    assert edge.marker is None
    assert edge.data == {}


def test_edge_marker_and_data_round_trip():
    edge = Edge(
        src="pkg:pandas",
        dst="pkg:numpy",
        relation=EdgeType.REQUIRES,
        origin="resolver",
        marker="python_version >= '3.9'",
    )
    ed = edge.to_dict()
    assert ed["marker"] == "python_version >= '3.9'"
    assert ed["data"] == {}


def test_edge_data_is_per_instance_not_shared():
    e1 = Edge(src="a", dst="b")
    e2 = Edge(src="c", dst="d")
    assert e1.data is not e2.data


def test_conflicts_with_allowed_package_to_package():
    a = make_node("pkg:flask", NodeType.PACKAGE, "flask", Layer.PIP)
    b = make_node("pkg:werkzeug", NodeType.PACKAGE, "werkzeug", Layer.PIP)
    graph = DepGraph().with_node(a).with_node(b)
    edge = Edge(
        src="pkg:flask",
        dst="pkg:werkzeug",
        relation=EdgeType.CONFLICTS_WITH,
        origin="resolver",
        data={"constraint_src": "werkzeug<2.0", "constraint_dst": "werkzeug>=2.1"},
    )
    out = graph.with_edge(edge)
    assert len(out.edges) == 1
    stored = out.edges[0]
    assert stored.relation is EdgeType.CONFLICTS_WITH
    assert stored.data["constraint_dst"] == "werkzeug>=2.1"
    assert stored.to_dict()["relation"] == "conflicts_with"


def test_edge_data_is_read_only():
    # frozen=True must mean immutable: in-place mutation of data is rejected so a
    # caller cannot silently corrupt an edge stored in a frozen DepGraph.
    edge = Edge(src="a", dst="b", data={"k": "v"})
    with pytest.raises(TypeError):
        edge.data["k"] = "mutated"
    assert edge.data["k"] == "v"


def test_edge_data_snapshots_caller_dict():
    # A plain dict passed in is copied, so later edits to the caller's dict do not
    # leak into the stored edge.
    src = {"k": "v"}
    edge = Edge(src="a", dst="b", data=src)
    src["k"] = "changed"
    assert edge.data["k"] == "v"


def test_node_exclude_newer_round_trips():
    node = Node(
        id="pkg:numpy==1.26.4",
        type=NodeType.PACKAGE,
        name="numpy",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
        version="1.26.4",
        exclude_newer="2024-01-01",
    )
    assert node.exclude_newer == "2024-01-01"
    assert node.to_dict()["exclude_newer"] == "2024-01-01"


def test_node_exclude_newer_defaults_none():
    node = make_node("pkg:numpy", NodeType.PACKAGE, "numpy", Layer.PIP)
    assert node.exclude_newer is None
    assert node.to_dict()["exclude_newer"] is None


def test_conflicts_with_rejects_non_package_endpoint():
    imp = make_node("import:cv2", NodeType.IMPORT, "cv2", Layer.NAMING)
    pkg = make_node("pkg:opencv-python", NodeType.PACKAGE, "opencv-python", Layer.PIP)
    graph = DepGraph().with_node(imp).with_node(pkg)
    with pytest.raises(ValueError):
        graph.with_edge(
            Edge(src="import:cv2", dst="pkg:opencv-python", relation=EdgeType.CONFLICTS_WITH)
        )


# --- Task 1: new env-tier NodeTypes, Layer.CONFIG, config_id ---

from python_deps.depgraph.schema import NodeType, Layer


def test_new_environment_node_types_exist():
    assert NodeType.PLATFORM.value == "Platform"
    assert NodeType.SERVICE.value == "Service"
    assert NodeType.CONFIG.value == "Config"


def test_config_layer_exists():
    assert Layer.CONFIG.value == "config"


def test_config_id_format():
    assert ids.config_id("DJANGO_SETTINGS_MODULE") == "config:DJANGO_SETTINGS_MODULE"


# --- Task 2: Node.tier auto-derived from type ---

def test_tier_auto_derived_from_type():
    pkg = make_node("pkg:x", NodeType.PACKAGE, "x", Layer.PIP)
    cfg = make_node("config:X", NodeType.CONFIG, "X", Layer.CONFIG)
    syslib = make_node("syslib:libGL.so.1", NodeType.SYSTEM_LIB, "libGL.so.1", Layer.SYSTEM)
    assert pkg.tier == 4
    assert cfg.tier == 6
    assert syslib.tier == 2


def test_goal_nodes_have_tier_zero():
    test_node = make_node("test:repo_tests_pass", NodeType.TEST, "repo_tests_pass", Layer.TESTS)
    assert test_node.tier == 0


def test_explicit_tier_is_respected():
    from python_deps.depgraph.schema import Node
    n = Node(id="config:X", type=NodeType.CONFIG, name="X", layer=Layer.CONFIG,
             discovered_by=DiscoveredBy.STATIC_SCAN, tier=3)
    assert n.tier == 3


def test_to_dict_includes_tier():
    cfg = make_node("config:X", NodeType.CONFIG, "X", Layer.CONFIG)
    assert cfg.to_dict()["tier"] == 6


# --- Task 3: requires edges into new env-tier node types ---

def test_requires_edge_into_config_is_allowed():
    from python_deps.depgraph.schema import DepGraph, Edge, EdgeType
    proj = make_node("project:app", NodeType.PROJECT, "app", Layer.PIP)
    cfg = make_node("config:SECRET_KEY", NodeType.CONFIG, "SECRET_KEY", Layer.CONFIG)
    g = DepGraph().with_node(proj).with_node(cfg)
    g = g.with_edge(Edge(src="project:app", dst="config:SECRET_KEY",
                         relation=EdgeType.REQUIRES, origin="project"))
    assert any(e.dst == "config:SECRET_KEY" for e in g.edges)


def test_requires_edge_from_config_to_service_is_allowed():
    # A binding CONFIG node (config-binding feature) `requires` the SERVICE it
    # binds to (config:<VAR> -> service:postgres): the rewritten DB var is only
    # usable once the in-image service is up. `Config` is therefore a legal
    # `requires` source (widened alongside `Service` in the services slice).
    from python_deps.depgraph.schema import DepGraph, Edge, EdgeType
    cfg = make_node("config:DB_STRING", NodeType.CONFIG, "DB_STRING", Layer.CONFIG)
    svc = make_node("service:postgres", NodeType.SERVICE, "postgres", Layer.SERVICES)
    g = DepGraph().with_node(cfg).with_node(svc)
    g = g.with_edge(Edge(src="config:DB_STRING", dst="service:postgres",
                         relation=EdgeType.REQUIRES, origin="service"))
    assert any(e.src == "config:DB_STRING" and e.dst == "service:postgres"
               for e in g.edges)


# --- Task 1 (services slice): Layer.SERVICES, service_id, Node.data ---


def test_service_layer_and_id():
    from python_deps.depgraph.schema import Layer
    assert Layer.SERVICES.value == "services"
    assert ids.service_id("postgres") == "service:postgres"


def test_node_data_defaults_empty_and_frozen():
    n = make_node("service:postgres", NodeType.SERVICE, "postgres", Layer.SERVICES)
    assert dict(n.data) == {}
    import types as _t
    assert isinstance(n.data, _t.MappingProxyType)


def test_node_data_roundtrips_and_serializes():
    from python_deps.depgraph.schema import Node
    n = Node(id="service:postgres", type=NodeType.SERVICE, name="postgres",
             layer=Layer.SERVICES, discovered_by=DiscoveredBy.STATIC_SCAN,
             data={"service_confidence": "confirmed", "port": 5432})
    assert n.data["service_confidence"] == "confirmed"
    assert n.to_dict()["data"] == {"service_confidence": "confirmed", "port": 5432}


def test_service_may_require_systemlib():
    from python_deps.depgraph.schema import (
        DepGraph, Node, NodeType, Layer, DiscoveredBy, Edge, EdgeType,
    )
    svc = Node(id="service:postgres", type=NodeType.SERVICE, name="postgres",
               layer=Layer.SERVICES, discovered_by=DiscoveredBy.STATIC_SCAN)
    sysl = Node(id="syslib:postgresql", type=NodeType.SYSTEM_LIB, name="postgresql",
                layer=Layer.SYSTEM, discovered_by=DiscoveredBy.STATIC_SCAN)
    g = DepGraph().with_node(svc).with_node(sysl)
    # Service -> SystemLib (requires) must be legal now (in-image: the server
    # binary IS in our apt closure — design §3/§5).
    g2 = g.with_edge(Edge(src="service:postgres", dst="syslib:postgresql",
                          relation=EdgeType.REQUIRES, origin="service"))
    assert any(e.src == "service:postgres" and e.dst == "syslib:postgresql"
               for e in g2.edges)


def test_service_still_illegal_as_conflicts_source():
    # Only the `requires` source set is widened; conflicts_with is unchanged.
    from python_deps.depgraph.schema import (
        DepGraph, Node, NodeType, Layer, DiscoveredBy, Edge, EdgeType,
    )
    svc = Node(id="service:postgres", type=NodeType.SERVICE, name="postgres",
               layer=Layer.SERVICES, discovered_by=DiscoveredBy.STATIC_SCAN)
    pkg = Node(id="pkg:psycopg2", type=NodeType.PACKAGE, name="psycopg2",
               layer=Layer.PIP, discovered_by=DiscoveredBy.RESOLVER)
    g = DepGraph().with_node(svc).with_node(pkg)
    import pytest
    with pytest.raises(ValueError):
        g.with_edge(Edge(src="service:postgres", dst="pkg:psycopg2",
                         relation=EdgeType.CONFLICTS_WITH))


def test_discovered_by_has_classifier():
    from python_deps.depgraph.schema import DiscoveredBy
    assert DiscoveredBy.CLASSIFIER.value == "classifier"
