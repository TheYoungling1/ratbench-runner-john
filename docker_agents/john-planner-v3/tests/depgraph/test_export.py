"""Task 8 — GraphML export (``export.py``).

``to_graphml`` must emit well-formed GraphML using the SAME key schema as
``docs/sample-dependency-graph.graphml`` so it renders in the existing HTML
viewer.  We parse both the export and the sample with ``xml.dom.minidom`` and
assert the key ``attr.name`` sets match.
"""

from __future__ import annotations

from pathlib import Path
from xml.dom import minidom

from python_deps.depgraph.export import to_graphml
from python_deps.depgraph.ids import TEST_NODE_ID, import_id, package_id, syslib_id
from python_deps.depgraph.schema import (
    DepGraph,
    DiscoveredBy,
    Edge,
    EdgeType,
    Layer,
    Node,
    NodeType,
    State,
)

_SAMPLE = (
    Path(__file__).resolve().parents[2] / "docs" / "sample-dependency-graph.graphml"
)


def _key_attr_names(xml_text: str):
    doc = minidom.parseString(xml_text)
    return {k.getAttribute("attr.name") for k in doc.getElementsByTagName("key")}


def _sample_graph() -> DepGraph:
    test = Node(
        id=TEST_NODE_ID,
        type=NodeType.TEST,
        name="repo_tests_pass",
        layer=Layer.TESTS,
        discovered_by=DiscoveredBy.GOAL,
        check_command="python -m pytest -q",
    )
    imp = Node(
        id=import_id("cv2"),
        type=NodeType.IMPORT,
        name="cv2",
        layer=Layer.NAMING,
        discovered_by=DiscoveredBy.STATIC_SCAN,
        state=State.SATISFIED,
        check_command='python -c "import cv2"',
    )
    pkg = Node(
        id=package_id("opencv-python", "4.9.0.80"),
        type=NodeType.PACKAGE,
        name="opencv-python",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
        version="4.9.0.80",
        state=State.SATISFIED,
        check_command="python -m pip show opencv-python",
        chosen_fix="pip:opencv-python",
    )
    syslib = Node(
        id=syslib_id("libGL.so.1"),
        type=NodeType.SYSTEM_LIB,
        name="libGL.so.1",
        layer=Layer.SYSTEM,
        discovered_by=DiscoveredBy.PROBE,
        state=State.MISSING,
        check_command="ldconfig -p | grep libGL.so.1",
        evidence="ImportError: libGL.so.1: cannot open shared object file",
        fix_candidates=("apt:libgl1",),
    )
    return (
        DepGraph()
        .with_node(test)
        .with_node(imp)
        .with_node(pkg)
        .with_node(syslib)
        .with_edge(Edge(src=TEST_NODE_ID, dst=imp.id, relation=EdgeType.REQUIRES))
        .with_edge(Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES))
        .with_edge(Edge(src=pkg.id, dst=syslib.id, relation=EdgeType.REQUIRES))
    )


def test_export_is_well_formed_xml():
    xml_text = to_graphml(_sample_graph())
    doc = minidom.parseString(xml_text)  # raises if malformed
    assert doc.documentElement.tagName == "graphml"


def test_export_key_schema_supersets_sample():
    # The enriched export is backward compatible: it preserves every original
    # sample key (so the existing viewer still works) and ADDS new keys.
    xml_text = to_graphml(_sample_graph())
    exported = _key_attr_names(xml_text)
    sample = _key_attr_names(_SAMPLE.read_text())

    original = {
        "label",
        "type",
        "layer",
        "state",
        "discovered_by",
        "check",
        "fix",
        "evidence",
        "relation",
    }
    assert original <= exported  # every original key preserved
    assert sample <= exported  # viewer-compatible superset
    # the uv-enrichment additions
    assert {"build_from_source", "marker", "constraint"} <= exported


def test_export_round_trips_nodes_and_edges():
    graph = _sample_graph()
    doc = minidom.parseString(to_graphml(graph))

    node_ids = {n.getAttribute("id") for n in doc.getElementsByTagName("node")}
    assert node_ids == {n.id for n in graph.nodes}

    edges = doc.getElementsByTagName("edge")
    assert len(edges) == len(graph.edges)
    for e in edges:
        rel = e.getElementsByTagName("data")[0].firstChild.nodeValue
        assert rel == "requires"


def test_export_node_carries_label_type_state():
    graph = _sample_graph()
    doc = minidom.parseString(to_graphml(graph))

    by_id = {n.getAttribute("id"): n for n in doc.getElementsByTagName("node")}
    syslib = by_id[syslib_id("libGL.so.1")]
    data = {
        d.getAttribute("key"): (d.firstChild.nodeValue if d.firstChild else "")
        for d in syslib.getElementsByTagName("data")
    }
    # d0=label d1=type d3=state d6=fix d7=evidence (key-id schema mirrors sample)
    assert data["d0"] == "libGL.so.1"
    assert data["d1"] == "SystemLib"
    assert data["d3"] == "missing"
    assert "apt:libgl1" in data["d6"]
    assert "libGL.so.1" in data["d7"]


def test_export_package_label_pins_version():
    # Design §5.4 / the sample render Package labels as ``name==version``.
    graph = _sample_graph()
    doc = minidom.parseString(to_graphml(graph))

    by_id = {n.getAttribute("id"): n for n in doc.getElementsByTagName("node")}
    pkg = by_id[package_id("opencv-python", "4.9.0.80")]
    label = next(
        d.firstChild.nodeValue
        for d in pkg.getElementsByTagName("data")
        if d.getAttribute("key") == "d0"
    )
    assert label == "opencv-python==4.9.0.80"


def test_export_runtime_node_round_trips():
    # The 6th node type must survive the viewer-export path.
    runtime = Node(
        id="runtime:DATABASE_URL",
        type=NodeType.RUNTIME,
        name="DATABASE_URL",
        layer=Layer.RUNTIME,
        discovered_by=DiscoveredBy.RUNTIME,
        state=State.MISSING,
        check_command='python -c "import os; assert os.environ[\'DATABASE_URL\']"',
    )
    graph = DepGraph().with_node(runtime)
    doc = minidom.parseString(to_graphml(graph))

    node = doc.getElementsByTagName("node")[0]
    data = {
        d.getAttribute("key"): (d.firstChild.nodeValue if d.firstChild else "")
        for d in node.getElementsByTagName("data")
    }
    assert data["d1"] == "Runtime"
    assert data["d2"] == "runtime"
    assert data["d4"] == "runtime"


def test_export_escapes_xml_special_chars():
    # Evidence with <, &, ", > must keep the document well-formed and round-trip.
    evil = 'ImportError: a<b & c="d" > e'
    syslib = Node(
        id=syslib_id("libGL.so.1"),
        type=NodeType.SYSTEM_LIB,
        name="libGL.so.1",
        layer=Layer.SYSTEM,
        discovered_by=DiscoveredBy.PROBE,
        state=State.MISSING,
        check_command="ldconfig -p | grep libGL.so.1",
        evidence=evil,
    )
    graph = DepGraph().with_node(syslib)

    doc = minidom.parseString(to_graphml(graph))  # raises if escaping is broken
    node = doc.getElementsByTagName("node")[0]
    reparsed = next(
        d.firstChild.nodeValue
        for d in node.getElementsByTagName("data")
        if d.getAttribute("key") == "d7"
    )
    assert reparsed == evil


def test_export_omits_empty_fields():
    # A node with no check/fix/evidence must not emit d5/d6/d7 elements.
    imp = Node(
        id=import_id("os"),
        type=NodeType.IMPORT,
        name="os",
        layer=Layer.NAMING,
        discovered_by=DiscoveredBy.STATIC_SCAN,
        check_command=None,
    )
    graph = DepGraph().with_node(imp)
    doc = minidom.parseString(to_graphml(graph))

    node = doc.getElementsByTagName("node")[0]
    keys = {d.getAttribute("key") for d in node.getElementsByTagName("data")}
    assert "d5" not in keys  # check_command
    assert "d6" not in keys  # fix
    assert "d7" not in keys  # evidence
    assert "d8" not in keys  # build_from_source (None -> omitted)
    # required identity fields are still present
    assert {"d0", "d1", "d2", "d3", "d4"} <= keys


def _data_map(node_elem):
    return {
        d.getAttribute("key"): (d.firstChild.nodeValue if d.firstChild else "")
        for d in node_elem.getElementsByTagName("data")
    }


def test_export_node_carries_build_from_source():
    pkg = Node(
        id=package_id("numpy", "1.26.4"),
        type=NodeType.PACKAGE,
        name="numpy",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
        version="1.26.4",
        build_from_source=True,
    )
    graph = DepGraph().with_node(pkg)
    doc = minidom.parseString(to_graphml(graph))
    data = _data_map(doc.getElementsByTagName("node")[0])
    assert data["d8"] == "true"


def test_export_edge_carries_marker():
    parent = Node(
        id=package_id("pkg-a", "1.0"),
        type=NodeType.PACKAGE,
        name="pkg-a",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
        version="1.0",
    )
    child = Node(
        id=package_id("pkg-b", "2.0"),
        type=NodeType.PACKAGE,
        name="pkg-b",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
        version="2.0",
    )
    graph = (
        DepGraph()
        .with_node(parent)
        .with_node(child)
        .with_edge(
            Edge(
                src=parent.id,
                dst=child.id,
                relation=EdgeType.REQUIRES,
                marker="python_version < '3.11'",
            )
        )
    )
    doc = minidom.parseString(to_graphml(graph))
    edge = doc.getElementsByTagName("edge")[0]
    data = _data_map(edge)
    assert data["e0"] == "requires"
    assert data["e1"] == "python_version < '3.11'"


def test_export_build_from_source_false_emits_false():
    # build_from_source=False must emit d8="false" (distinct from omitted/None).
    pkg = Node(
        id=package_id("numpy", "1.26.4"),
        type=NodeType.PACKAGE,
        name="numpy",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
        version="1.26.4",
        build_from_source=False,
    )
    graph = DepGraph().with_node(pkg)
    doc = minidom.parseString(to_graphml(graph))
    assert _data_map(doc.getElementsByTagName("node")[0])["d8"] == "false"


def test_export_fix_joins_multiple_candidates():
    node = Node(
        id=syslib_id("libGL.so.1"),
        type=NodeType.SYSTEM_LIB,
        name="libGL.so.1",
        layer=Layer.SYSTEM,
        discovered_by=DiscoveredBy.PROBE,
        state=State.MISSING,
        fix_candidates=("apt:libgl1", "apt:libglx-mesa0"),
    )
    graph = DepGraph().with_node(node)
    doc = minidom.parseString(to_graphml(graph))
    assert _data_map(doc.getElementsByTagName("node")[0])["d6"] == (
        "apt:libgl1; apt:libglx-mesa0"
    )


def test_export_constraint_single_bound():
    edge = Edge(
        src="pkg:a",
        dst="pkg:b",
        relation=EdgeType.CONFLICTS_WITH,
        data={"package": "pkg", "src_bound": "<2.0"},
    )
    graph = DepGraph(edges=(edge,))
    doc = minidom.parseString(to_graphml(graph))
    assert _data_map(doc.getElementsByTagName("edge")[0])["e2"] == "pkg: <2.0"


def test_export_constraint_no_package_key():
    edge = Edge(
        src="pkg:a",
        dst="pkg:b",
        relation=EdgeType.CONFLICTS_WITH,
        data={"src_bound": "<2.0", "dst_bound": ">=2.0"},
    )
    graph = DepGraph(edges=(edge,))
    doc = minidom.parseString(to_graphml(graph))
    assert _data_map(doc.getElementsByTagName("edge")[0])["e2"] == "<2.0 vs >=2.0"


def test_export_conflicts_with_edge_carries_constraint():
    a = Node(
        id=package_id("pkg-a", None),
        type=NodeType.PACKAGE,
        name="pkg-a",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
    )
    b = Node(
        id=package_id("pkg-b", None),
        type=NodeType.PACKAGE,
        name="pkg-b",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
    )
    graph = (
        DepGraph()
        .with_node(a)
        .with_node(b)
        .with_edge(
            Edge(
                src=a.id,
                dst=b.id,
                relation=EdgeType.CONFLICTS_WITH,
                origin="resolver",
                data={"package": "urllib3", "src_bound": "<2.0", "dst_bound": ">=2.0"},
            )
        )
    )
    doc = minidom.parseString(to_graphml(graph))
    edge = doc.getElementsByTagName("edge")[0]
    data = _data_map(edge)
    assert data["e0"] == "conflicts_with"
    assert "urllib3" in data["e2"]
    assert "<2.0" in data["e2"] and ">=2.0" in data["e2"]
