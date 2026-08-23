"""Wheel-oracle prior seeding (``seed.py``, construction-enrichment cluster 1a).

Replaces the old curated-table prediction: the ONLY signal is the resolver's
own ``build_from_source`` flag. A package that either needs a source build
(``build_from_source=True``) or has unknown build mode (``build_from_source=None``,
e.g. from degraded uv-pip-compile fallback) predicts a generic compiler toolchain
(``tool:build-essential``); only confirmed wheels (``build_from_source=False``)
skip the prediction. Does NOT predict specific ``-dev`` headers (that used to
come from ``PACKAGE_TO_SYSTEM_DEPS``, now deleted — see the design doc's
"What this loses, honestly").
"""

from __future__ import annotations

from python_deps.depgraph.ids import package_id, tool_id
from python_deps.depgraph.schema import (
    DepGraph,
    DiscoveredBy,
    EdgeType,
    Layer,
    Node,
    NodeType,
    State,
)
from python_deps.depgraph.seed import seed_wheel_oracle_prior


def _package(name: str, version: str, *, build_from_source=None) -> Node:
    return Node(
        id=package_id(name, version),
        type=NodeType.PACKAGE,
        name=name,
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
        version=version,
        check_command=f"python -m pip show {name}",
        build_from_source=build_from_source,
    )


def test_seed_predicts_build_essential_for_from_source_package():
    pkg = _package("psycopg2", "2.9.9", build_from_source=True)
    graph = DepGraph().with_node(pkg)

    out = seed_wheel_oracle_prior(graph)

    tool = out.get(tool_id("build-essential"))
    assert tool is not None
    assert tool.type is NodeType.TOOL
    assert tool.layer is Layer.TOOLCHAIN
    assert tool.discovered_by is DiscoveredBy.RESOLVER
    assert tool.state is State.UNKNOWN
    assert tool.fix_candidates == ("apt:build-essential",)
    assert tool.chosen_fix == "apt:build-essential"
    deps = {d.id for d in out.requires_of(pkg.id)}
    assert tool_id("build-essential") in deps


def test_seed_no_prediction_when_build_from_source_false():
    pkg = _package("requests", "2.31.0", build_from_source=False)
    graph = DepGraph().with_node(pkg)

    out = seed_wheel_oracle_prior(graph)

    assert [n for n in out.nodes if n.type is NodeType.TOOL] == []


def test_seed_predicts_build_essential_when_build_from_source_unknown():
    """Unknown build mode (e.g. resolved via the degraded uv-pip-compile fallback,
    which never computes the wheel/sdist signal) is treated cautiously, same as a
    known from-source build — matches emit.py's _toolchain_ready semantics
    (build_from_source is not False gates on the Tool dep)."""
    pkg = _package("requests", "2.31.0")  # default None (unknown)
    graph = DepGraph().with_node(pkg)

    out = seed_wheel_oracle_prior(graph)

    tool = out.get(tool_id("build-essential"))
    assert tool is not None
    deps = {d.id for d in out.requires_of(pkg.id)}
    assert tool_id("build-essential") in deps


def test_seed_dedupes_build_essential_across_multiple_from_source_packages():
    a = _package("psycopg2", "2.9.9", build_from_source=True)
    b = _package("lxml", "5.2.0", build_from_source=True)
    graph = DepGraph().with_node(a).with_node(b)

    out = seed_wheel_oracle_prior(graph)

    tools = [n for n in out.nodes if n.id == tool_id("build-essential")]
    assert len(tools) == 1
    a_deps = {d.id for d in out.requires_of(a.id)}
    b_deps = {d.id for d in out.requires_of(b.id)}
    assert tool_id("build-essential") in a_deps
    assert tool_id("build-essential") in b_deps


def test_seed_predicted_edge_is_resolver_origin():
    pkg = _package("psycopg2", "2.9.9", build_from_source=True)
    graph = DepGraph().with_node(pkg)

    out = seed_wheel_oracle_prior(graph)

    edges = [
        e for e in out.edges
        if e.dst == tool_id("build-essential") and e.relation is EdgeType.REQUIRES
    ]
    assert edges and all(e.origin == "resolver" for e in edges)


def test_seed_no_op_when_no_packages_need_a_build():
    graph = DepGraph()
    out = seed_wheel_oracle_prior(graph)
    assert out.nodes == ()


def test_seed_returns_new_graph_originals_unchanged():
    pkg = _package("psycopg2", "2.9.9", build_from_source=True)
    graph = DepGraph().with_node(pkg)

    out = seed_wheel_oracle_prior(graph)

    assert out is not graph
    assert graph.get(tool_id("build-essential")) is None


def test_seed_no_prediction_for_unresolved_diagnostic_package():
    """Diagnostic packages representing resolver failures (version=None) do not
    get a build-essential prediction, even if build_from_source is None.
    This aligns seed.py with emit.py's treatment of unresolved nodes as
    non-emittable."""
    diagnostic_pkg = Node(
        id=package_id("missing-package", "unresolved"),
        type=NodeType.PACKAGE,
        name="missing-package",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
        version=None,  # unresolved diagnostic
        check_command="python -m pip show missing-package",
        build_from_source=None,  # unknown (default)
    )
    graph = DepGraph().with_node(diagnostic_pkg)

    out = seed_wheel_oracle_prior(graph)

    # No build-essential node should be created for unresolved diagnostics
    assert [n for n in out.nodes if n.type is NodeType.TOOL] == []
