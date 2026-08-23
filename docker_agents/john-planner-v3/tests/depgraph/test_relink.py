"""Unit tests for certified import->package relink (no Docker/network)."""

from __future__ import annotations

from python_deps.depgraph.relink import (
    PACKAGES_DIST_CMD,
    parse_packages_distributions,
)


def test_parse_valid_map():
    stdout = '{"cv2": ["opencv-python"], "yaml": ["PyYAML"], "google": ["google-auth", "protobuf"]}'
    out = parse_packages_distributions(stdout)
    assert out["cv2"] == ["opencv-python"]
    assert out["google"] == ["google-auth", "protobuf"]


def test_parse_malformed_returns_empty():
    assert parse_packages_distributions("not json") == {}
    assert parse_packages_distributions("") == {}
    assert parse_packages_distributions("[1, 2, 3]") == {}


def test_command_is_stdlib_only():
    assert "packages_distributions" in PACKAGES_DIST_CMD
    assert "importlib.metadata" in PACKAGES_DIST_CMD


from python_deps.depgraph.ids import import_id, package_id
from python_deps.depgraph.schema import (
    DepGraph,
    DiscoveredBy,
    Edge,
    EdgeType,
    Layer,
    Node,
    NodeType,
)
from python_deps.depgraph.relink import import_to_package_edges


def _imp(name):
    return Node(
        id=import_id(name), type=NodeType.IMPORT, name=name,
        layer=Layer.NAMING, discovered_by=DiscoveredBy.STATIC_SCAN,
    )


def _pkg(name, version="1.0"):
    return Node(
        id=package_id(name, version), type=NodeType.PACKAGE, name=name,
        layer=Layer.PIP, discovered_by=DiscoveredBy.RESOLVER, version=version,
    )


def test_edge_builder_links_unmapped_import():
    # Heuristic identity guess would say dateutil->dateutil and find no package;
    # packages_distributions says dateutil is provided by python-dateutil.
    graph = DepGraph().with_node(_imp("dateutil")).with_node(_pkg("python-dateutil", "2.9.0"))
    edges = import_to_package_edges(graph, {"dateutil": ["python-dateutil"]})
    assert len(edges) == 1
    e = edges[0]
    assert e.src == import_id("dateutil")
    assert e.dst == package_id("python-dateutil", "2.9.0")
    assert e.relation is EdgeType.REQUIRES
    assert e.origin == "certified"


def test_edge_builder_case_insensitive_module_key():
    # packages_distributions key is the real module name "PIL"; Import node too.
    graph = DepGraph().with_node(_imp("PIL")).with_node(_pkg("pillow", "10.3.0"))
    edges = import_to_package_edges(graph, {"PIL": ["pillow"]})
    assert len(edges) == 1
    assert edges[0].dst == package_id("pillow", "10.3.0")


def test_edge_builder_namespace_links_all_present_dists():
    graph = (
        DepGraph()
        .with_node(_imp("google"))
        .with_node(_pkg("google-auth", "2.0"))
        .with_node(_pkg("protobuf", "4.0"))
    )
    edges = import_to_package_edges(graph, {"google": ["google-auth", "protobuf", "google-api-core"]})
    dsts = {e.dst for e in edges}
    assert package_id("google-auth", "2.0") in dsts
    assert package_id("protobuf", "4.0") in dsts
    # google-api-core has no Package node in the closure -> no edge.
    assert len(edges) == 2


def test_edge_builder_skips_existing_edge():
    graph = (
        DepGraph()
        .with_node(_imp("yaml"))
        .with_node(_pkg("PyYAML", "6.0"))
    )
    graph = graph.with_edge(
        Edge(src=import_id("yaml"), dst=package_id("PyYAML", "6.0"),
             relation=EdgeType.REQUIRES, origin="reconcile")
    )
    edges = import_to_package_edges(graph, {"yaml": ["PyYAML"]})
    assert edges == []


from python_deps.depgraph.relink import certified_import_links


def test_certified_import_links_adds_edge(fake_executor, make_result_fixture):
    graph = DepGraph().with_node(_imp("dateutil")).with_node(_pkg("python-dateutil", "2.9.0"))
    fake_executor.responses = {
        "packages_distributions": make_result_fixture(
            stdout='{"dateutil": ["python-dateutil"]}'
        )
    }

    out = certified_import_links(graph, fake_executor)

    deps = out.requires_of(import_id("dateutil"))
    assert any(d.id == package_id("python-dateutil", "2.9.0") for d in deps)


def test_certified_import_links_graceful_on_command_failure(fake_executor):
    # Empty FakeExecutor -> command returns rc 127 (not ok) -> graph unchanged.
    graph = DepGraph().with_node(_imp("dateutil")).with_node(_pkg("python-dateutil", "2.9.0"))
    out = certified_import_links(graph, fake_executor)
    assert out.edges == ()


from python_deps.depgraph.relink import flag_unresolved_imports


def test_unlinked_import_is_flagged_unresolved():
    # `box` was linked by relink (import->package edge present); `mystery` has
    # no distribution at all and must be flagged as an honest under-declaration
    # signal, not a fabricated root.
    linked = _imp("box")
    unlinked = _imp("mystery")
    pkg = _pkg("python-box", "7.3.2")
    edge = Edge(src=linked.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="certified")
    graph = DepGraph(nodes=(linked, unlinked, pkg), edges=(edge,))

    out = flag_unresolved_imports(graph)

    assert out.get(unlinked.id).data.get("unresolved") is True
    assert "mystery" in out.get(unlinked.id).evidence
    assert out.get(linked.id).data.get("unresolved") is not True


def test_stale_unresolved_flag_cleared_when_now_provided():
    # `box` was flagged unresolved on a PRIOR pass (stale data carried over),
    # but NOW has a certified REQUIRES->Package edge -- the relink must clear
    # both the stale flag and the evidence it set, not just leave it stuck.
    imp = Node(
        id=import_id("box"), type=NodeType.IMPORT, name="box",
        layer=Layer.NAMING, discovered_by=DiscoveredBy.STATIC_SCAN,
        data={"unresolved": True},
        evidence="unresolved: no distribution provides import box",
    )
    pkg = _pkg("python-box", "7.3.2")
    edge = Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="certified")
    graph = DepGraph(nodes=(imp, pkg), edges=(edge,))

    out = flag_unresolved_imports(graph)

    node = out.get(imp.id)
    assert node.data.get("unresolved") is not True
    assert "unresolved" not in dict(node.data)
    assert node.evidence is None


def test_stale_unresolved_flag_clear_preserves_other_data_keys():
    # Clearing the stale flag must not disturb unrelated data keys on the node.
    imp = Node(
        id=import_id("box"), type=NodeType.IMPORT, name="box",
        layer=Layer.NAMING, discovered_by=DiscoveredBy.STATIC_SCAN,
        data={"unresolved": True, "some_other_key": "keep-me"},
        evidence="unresolved: no distribution provides import box",
    )
    pkg = _pkg("python-box", "7.3.2")
    edge = Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="certified")
    graph = DepGraph(nodes=(imp, pkg), edges=(edge,))

    out = flag_unresolved_imports(graph)

    node = out.get(imp.id)
    assert node.data.get("some_other_key") == "keep-me"
    assert "unresolved" not in dict(node.data)


def test_linked_never_flagged_import_not_rewritten():
    # A provided import that was never flagged unresolved must be left byte-
    # for-byte untouched (no needless node rewrite on the common/first-run path).
    linked = _imp("box")
    pkg = _pkg("python-box", "7.3.2")
    edge = Edge(src=linked.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="certified")
    graph = DepGraph(nodes=(linked, pkg), edges=(edge,))

    out = flag_unresolved_imports(graph)

    assert out.get(linked.id) is linked


def test_flag_unresolved_imports_is_idempotent():
    # Running flag_unresolved_imports twice must yield the same node data as
    # running it once, regardless of the prior flag state on the input graph.
    linked = _imp("box")
    unlinked = _imp("mystery")
    stale = Node(
        id=import_id("stale_but_provided"), type=NodeType.IMPORT, name="stale_but_provided",
        layer=Layer.NAMING, discovered_by=DiscoveredBy.STATIC_SCAN,
        data={"unresolved": True},
        evidence="unresolved: no distribution provides import stale_but_provided",
    )
    pkg = _pkg("python-box", "7.3.2")
    graph = (
        DepGraph(nodes=(linked, unlinked, stale, pkg))
        .with_edge(Edge(src=linked.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="certified"))
        .with_edge(Edge(src=stale.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="certified"))
    )

    once = flag_unresolved_imports(graph)
    twice = flag_unresolved_imports(once)

    for node_id in (linked.id, unlinked.id, stale.id):
        n1 = once.get(node_id)
        n2 = twice.get(node_id)
        assert dict(n1.data) == dict(n2.data)
        assert n1.evidence == n2.evidence


def _opt_imp(name):
    # A try/except-ImportError guarded import (P0.2 tags data["optional"]=True).
    return Node(
        id=import_id(name), type=NodeType.IMPORT, name=name,
        layer=Layer.NAMING, discovered_by=DiscoveredBy.STATIC_SCAN,
        data={"optional": True},
    )


def test_optional_unprovided_import_not_flagged():
    # A guarded/optional import that no distribution provides is NOT an under-
    # declaration (the try/except is deliberate) -- leave it unflagged.
    opt = _opt_imp("ujson")
    out = flag_unresolved_imports(DepGraph(nodes=(opt,)))
    assert out.get(opt.id).data.get("unresolved") is not True


def test_hard_unprovided_import_still_flagged():
    # A non-optional import with no provider is a genuine under-declaration
    # signal (unchanged existing behavior).
    hard = _imp("mystery")  # data={} -> not optional
    out = flag_unresolved_imports(DepGraph(nodes=(hard,)))
    node = out.get(hard.id)
    assert node.data.get("unresolved") is True
    assert "mystery" in node.evidence


def test_transitively_satisfied_import_not_flagged():
    # `urllib3` is covered by a REQUIRES edge to a present `requests` Package
    # (mimicking transitive coverage) -> provided -> not flagged.
    imp = _imp("urllib3")
    pkg = _pkg("requests", "2.32.0")
    edge = Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="certified")
    graph = DepGraph(nodes=(imp, pkg), edges=(edge,))
    out = flag_unresolved_imports(graph)
    assert out.get(imp.id).data.get("unresolved") is not True


def test_name_variant_import_not_flagged():
    # `cv2` certified-linked to the `opencv-python` Package (name variant) is
    # provided -> not flagged.
    imp = _imp("cv2")
    pkg = _pkg("opencv-python", "4.10.0")
    edge = Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="certified")
    graph = DepGraph(nodes=(imp, pkg), edges=(edge,))
    out = flag_unresolved_imports(graph)
    assert out.get(imp.id).data.get("unresolved") is not True


def test_stale_flag_cleared_when_now_optional():
    # An import flagged unresolved on a PRIOR pass but NOW known optional (P0.2)
    # must have the stale flag + evidence cleared; unrelated data keys preserved.
    imp = Node(
        id=import_id("ujson"), type=NodeType.IMPORT, name="ujson",
        layer=Layer.NAMING, discovered_by=DiscoveredBy.STATIC_SCAN,
        data={"unresolved": True, "optional": True, "some_other_key": "keep-me"},
        evidence="unresolved: no distribution provides import ujson",
    )
    out = flag_unresolved_imports(DepGraph(nodes=(imp,)))
    node = out.get(imp.id)
    assert node.data.get("unresolved") is not True
    assert "unresolved" not in dict(node.data)
    assert node.data.get("optional") is True  # exemption key untouched
    assert node.data.get("some_other_key") == "keep-me"
    assert node.evidence is None
