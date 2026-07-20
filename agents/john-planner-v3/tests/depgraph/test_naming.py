"""Tests for stage 2: import -> distribution name mapping (``naming.py``).

Realizes section 4.2 / 10.2 of the design: import names are mapped to PyPI
distribution names via the curated table, with declared manifest names taking
precedence (the precedence ladder of 10.3).  Pure, no Executor needed.

REFERENCE-ONLY -- DO NOT RE-WIRE INTO CONSTRUCTION.
``naming.package_roots`` is OFF the two-phase declared-only construction path:
``build_dep_graph`` no longer calls it. It is RETAINED solely as the A/B
eval's *generator* reference (see P3.1 / ``tests/eval/graph_fidelity/``), which
measures what an imports-as-generator selector *would* have added versus the
declared-only verifier that construction actually uses. These tests exercise
that retained helper directly; they assert the mapping semantics of the
reference generator, NOT construction behavior. A future reader must not take a
green test here as licence to re-connect ``package_roots`` to
``build_dep_graph`` -- imports audit demand, they never fabricate roots.
"""

from __future__ import annotations

from python_deps.depgraph.ids import TEST_NODE_ID, import_id
from python_deps.depgraph.naming import package_roots
from python_deps.depgraph.schema import (
    DepGraph,
    DiscoveredBy,
    Layer,
    Node,
    NodeType,
)


def _import_node(name: str) -> Node:
    return Node(
        id=import_id(name),
        type=NodeType.IMPORT,
        name=name,
        layer=Layer.NAMING,
        discovered_by=DiscoveredBy.STATIC_SCAN,
    )


def _graph(*names: str) -> DepGraph:
    graph = DepGraph()
    for name in names:
        graph = graph.with_node(_import_node(name))
    return graph


def test_curated_native_and_aliased_mappings():
    graph = _graph("cv2", "PIL", "sklearn", "bs4", "fitz", "yaml")
    roots = dict(package_roots(graph))
    assert roots[import_id("cv2")] == "opencv-python"
    assert roots[import_id("PIL")] == "Pillow"
    assert roots[import_id("sklearn")] == "scikit-learn"
    assert roots[import_id("bs4")] == "beautifulsoup4"
    assert roots[import_id("fitz")] == "PyMuPDF"
    assert roots[import_id("yaml")] == "PyYAML"


def test_new_native_aliases_added_additively():
    graph = _graph("psycopg2", "MySQLdb", "OpenSSL", "lxml")
    roots = dict(package_roots(graph))
    assert roots[import_id("psycopg2")] == "psycopg2"
    assert roots[import_id("MySQLdb")] == "mysqlclient"
    assert roots[import_id("OpenSSL")] == "pyOpenSSL"
    assert roots[import_id("lxml")] == "lxml"


def test_unmapped_import_yields_no_root():
    # An undeclared import with no curated-table entry is unresolved -> no
    # root is fabricated for it (the old identity fallback is gone).
    graph = _graph("requests")
    assert package_roots(graph) == []


def test_returns_one_pair_per_import_in_node_order():
    # Only resolved imports (declared or curated) appear, in node order;
    # "requests" is undeclared and uncurated here, so it is unresolved and
    # omitted entirely.
    graph = _graph("cv2", "requests")
    assert package_roots(graph) == [
        (import_id("cv2"), "opencv-python"),
    ]


def test_declared_name_precedence_over_curated():
    graph = _graph("yaml")
    # No manifest: the curated table wins.
    assert package_roots(graph) == [(import_id("yaml"), "PyYAML")]
    # A declared distribution whose normalized name matches the import wins
    # over the curated table, preserving the manifest's original form.
    assert package_roots(graph, declared_names={"YAML"}) == [
        (import_id("yaml"), "YAML")
    ]


def test_non_import_nodes_are_ignored():
    graph = _graph("cv2").with_node(
        Node(
            id=TEST_NODE_ID,
            type=NodeType.TEST,
            name="repo_tests_pass",
            layer=Layer.TESTS,
            discovered_by=DiscoveredBy.GOAL,
        )
    )
    assert package_roots(graph) == [(import_id("cv2"), "opencv-python")]


def test_package_roots_omits_unresolved_import(monkeypatch):
    import python_deps.depgraph.naming as naming
    from python_deps.import_mapping import unresolved_result, MappingResult

    def fake_map(import_name, declared_package_names=None):
        if import_name == "mystery":
            return unresolved_result(import_name)
        return MappingResult(import_name, import_name, "direct_name", "low")

    monkeypatch.setattr(naming, "map_import_to_package", fake_map)
    graph = _graph("requests", "mystery")
    roots = naming.package_roots(graph)
    # requests still resolves; mystery is unresolved -> no root fabricated.
    assert (import_id("mystery"), None) not in roots
    assert all(dist is not None for _imp, dist in roots)
    assert (import_id("requests"), "requests") in roots
