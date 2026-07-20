# tests/depgraph/test_project_native_deps.py
"""Tests for project_native_deps.py — the R1b project-native-build-obligations
construction stage.

Pure/no-Docker: ``Executor.run`` is stubbed via ``FakeExecutor`` (mirrors
``tests/depgraph/test_build_deps.py``); native-build-surface fixtures are
throwaway ``tmp_path`` trees, matching ``test_project_native_scan.py``.
"""

from __future__ import annotations

import textwrap

from python_deps.depgraph import build_deps
from python_deps.depgraph.ids import (
    apt_build_id, linker_id, package_id, project_id, tool_id,
)
from python_deps.depgraph.os_resolver import ProviderCandidate
from python_deps.depgraph.project_native_deps import project_native_obligations
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Layer, Node, NodeType, State,
)
from python_deps.depgraph.seed import seed_wheel_oracle_prior

from conftest import FakeExecutor, make_result  # type: ignore

_EX = FakeExecutor()  # every command misses -> every source degrades cleanly


def _write(tmp_path, rel, src):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(src))
    return p


def _project_node(name: str) -> Node:
    return Node(
        id=project_id(name),
        type=NodeType.PROJECT,
        name=name,
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.STATIC_SCAN,
        state=State.UNKNOWN,
        data={"installable": True},
    )


def _graph(*nodes):
    g = DepGraph()
    for n in nodes:
        g = g.with_node(n)
    return g


def test_scan_finds_own_extension_libraries_seeds_node_and_edge_on_project(
    tmp_path, monkeypatch,
):
    # §2.4 (primary): the repo's OWN setup.py declares Extension(libraries=["foo"]).
    _write(tmp_path, "setup.py", """
        from setuptools import setup, Extension
        setup(
            name="pygraphviz",
            ext_modules=[Extension("pkg._native", sources=["pkg/n.c"], libraries=["foo"])],
        )
    """)
    proj = _project_node("pygraphviz")
    graph = _graph(proj)

    def _fake_resolve(need, executor):
        if need.kind == "linker_lib" and need.name == "foo":
            return [ProviderCandidate(manager="apt", package="libfoo-dev", source="table")]
        return []

    monkeypatch.setattr(build_deps, "resolve", _fake_resolve)

    out = project_native_obligations(graph, str(tmp_path), _EX, _EX)

    node = out.get(linker_id("foo"))
    assert node is not None
    assert node.type is NodeType.TOOL
    assert node.discovered_by is DiscoveredBy.RESOLVER
    assert node.state is State.UNKNOWN
    assert node.chosen_fix == "apt:libfoo-dev"
    # the edge attaches to the PROJECT id, not floating.
    assert any(
        e.src == proj.id and e.dst == linker_id("foo") and e.origin == "resolver"
        for e in out.edges
    )


def test_debian_build_depends_keyed_by_own_project_name(tmp_path, monkeypatch):
    # §2.3 (lxml fix): stub `apt-cache showsrc <project-name>` directly -- the
    # project's OWN pypi-style name ("lxml"), not a dependency's. lxml is a
    # Cython project, so a `.pyx` fixture makes has_native_build_signal True
    # (required now that §2.3 is gated on the native signal).
    _write(tmp_path, "src/lxml/etree.pyx", "# cython\ncdef int _x():\n    return 0\n")
    showsrc_stdout = textwrap.dedent("""
        Package: lxml
        Binary: python3-lxml, python-lxml-doc
        Build-Depends: debhelper (>= 10), dh-python, python3-all-dev,
         libxml2-dev, libxslt1-dev, zlib1g-dev, python3-setuptools
    """)
    executor = FakeExecutor(responses={
        "grep -q '^Types: deb deb-src$'": make_result(returncode=0),
        "apt-cache showsrc lxml": make_result(stdout=showsrc_stdout, returncode=0),
        "apt-get install -s": make_result(returncode=0),  # set IS installable
    })
    proj = _project_node("lxml")
    graph = _graph(proj)

    out = project_native_obligations(graph, str(tmp_path), executor, executor)

    for apt_name in ("libxml2-dev", "libxslt1-dev", "zlib1g-dev"):
        node = out.get(apt_build_id(apt_name))
        assert node is not None, f"missing {apt_name}"
        assert node.chosen_fix == f"apt:{apt_name}"
        assert any(
            e.src == proj.id and e.dst == apt_build_id(apt_name) for e in out.edges
        )
    # machinery is filtered out, never seeded as an aptdep node.
    assert out.get(apt_build_id("debhelper")) is None
    assert out.get(apt_build_id("python3-all-dev")) is None


def test_floor_seeded_and_deduped_against_existing_build_essential_node(tmp_path):
    # §2.5: a .pyx file with no Extension()/libraries= -- has_native_build_signal
    # is True even though scan_native_build_surface finds no specific library.
    _write(tmp_path, "speedups.pyx", "cdef int add(int a, int b):\n    return a + b\n")

    pkg = Node(
        id=package_id("otherpkg", "1.0"), type=NodeType.PACKAGE, name="otherpkg",
        layer=Layer.PIP, discovered_by=DiscoveredBy.RESOLVER, version="1.0",
        build_from_source=True,
    )
    proj = _project_node("cython-proj")
    graph = seed_wheel_oracle_prior(_graph(proj, pkg))  # pre-seeds build-essential
    pre_be_nodes = [n for n in graph.nodes if n.id == tool_id("build-essential")]
    assert len(pre_be_nodes) == 1  # sanity: the union scenario already has one

    out = project_native_obligations(graph, str(tmp_path), _EX, _EX)

    be_nodes = [n for n in out.nodes if n.id == tool_id("build-essential")]
    assert len(be_nodes) == 1  # deduped, not a second copy
    assert any(
        e.src == proj.id and e.dst == tool_id("build-essential") for e in out.edges
    )
    # the original package's edge is untouched (union, not replacement).
    assert any(
        e.src == pkg.id and e.dst == tool_id("build-essential") for e in out.edges
    )


def test_pure_python_project_is_a_no_op(tmp_path):
    _write(tmp_path, "setup.py", """
        from setuptools import setup
        setup(name="purepkg", packages=["purepkg"])
    """)
    proj = _project_node("purepkg")
    graph = _graph(proj)

    out = project_native_obligations(graph, str(tmp_path), _EX, _EX)

    assert out.nodes == graph.nodes
    assert out.edges == graph.edges


def test_non_native_project_never_consults_its_debian_namesake(tmp_path):
    # REGRESSION GUARD (the sweep's click/rich over-prediction): a pure-Python
    # project ("click") whose Debian NAMESAKE (Ubuntu's Click package manager)
    # has a real, unrelated Build-Depends. §2.3 is gated on the native signal,
    # so with NO Extension/.pyx the Debian source is NEVER consulted -- byte-
    # identical to the pre-R1b baseline (predicted_apt == []).
    _write(tmp_path, "setup.py", """
        from setuptools import setup
        setup(name="click", packages=["click"])
    """)
    showsrc_stdout = textwrap.dedent("""
        Package: click
        Binary: click, python3-click-package
        Build-Depends: valac, libgee-0.8-dev, dbus-test-runner,
         gobject-introspection, python3:any
    """)
    executor = FakeExecutor(responses={
        "grep -q '^Types: deb deb-src$'": make_result(returncode=0),
        "apt-cache showsrc click": make_result(stdout=showsrc_stdout, returncode=0),
        "apt-get install -s": make_result(returncode=0),
    })
    proj = _project_node("click")
    graph = _graph(proj)

    out = project_native_obligations(graph, str(tmp_path), executor, executor)

    # §2.3 skipped entirely -> not one aptdep node, and a full no-op (§2.5 also
    # off: pure-Python has no native signal).
    assert [n for n in out.nodes if n.id.startswith("aptdep:")] == []
    assert out.nodes == graph.nodes
    assert out.edges == graph.edges


def test_uninstallable_debian_set_is_dropped_but_floor_still_fires(tmp_path):
    # §2.3 apt-installability guard: a NATIVE project (`.pyx` -> signal True)
    # whose own Debian source has a conflicting/uninstallable JOINT
    # Build-Depends. `apt-get install -s` returns rc!=0 -> the WHOLE Debian set
    # is dropped (no aptdep nodes), but the §2.5 build-essential floor still
    # fires (native signal is present regardless of the Debian set).
    _write(tmp_path, "foo.pyx", "cdef int add(int a, int b):\n    return a + b\n")
    showsrc_stdout = textwrap.dedent("""
        Package: uwsgi
        Binary: python3-uwsgi
        Build-Depends: libpcre3-dev, libjansson-dev, libconflict-a-dev,
         libconflict-b-dev
    """)
    executor = FakeExecutor(responses={
        "grep -q '^Types: deb deb-src$'": make_result(returncode=0),
        "apt-cache showsrc uwsgi": make_result(stdout=showsrc_stdout, returncode=0),
        "apt-get install -s": make_result(returncode=1),  # set is NOT installable
    })
    proj = _project_node("uwsgi")
    graph = _graph(proj)

    out = project_native_obligations(graph, str(tmp_path), executor, executor)

    # entire Debian set dropped -> no aptdep nodes at all.
    assert [n for n in out.nodes if n.id.startswith("aptdep:")] == []
    # but the floor still fires (native signal is True).
    assert out.get(tool_id("build-essential")) is not None
    assert any(
        e.src == proj.id and e.dst == tool_id("build-essential") for e in out.edges
    )
