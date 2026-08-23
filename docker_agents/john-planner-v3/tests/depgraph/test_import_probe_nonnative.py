"""P2.3 (Correction 4) — honest flag for METADATA-PRESENT non-native import failures.

The third failure class: an import whose ``import X`` raises for a NON-native reason
(a broken/under-provisioned distribution, a Python-level ``ImportError``/``RuntimeError``
at import time). Phase A saw it "provided" (a dist supplies the import name → there is
an outgoing REQUIRES->Package edge); Phase B's ``import_probe`` used to only create a
``SystemLib`` on a ``NATIVE_LIBRARY_RE`` match and otherwise SILENTLY drop the failure.
Now such a failure is surfaced honestly on the owning Import node as
``data["unresolved_runtime"] = True`` + a short ``data["import_error"]`` — distinct
from the metadata-ABSENT ``data["unresolved"]`` flag (P0.3) and from a fabricated
``SystemLib`` (native path, unchanged).

All hermetic: ``FakeExecutor`` (conftest) + the P1.5 autouse network guard.
"""

from __future__ import annotations

from dataclasses import replace

from python_deps.depgraph.ids import import_id, package_id, syslib_id
from python_deps.depgraph.probe import import_probe
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


def _package(name: str, version: str) -> Node:
    return Node(
        id=package_id(name, version),
        type=NodeType.PACKAGE,
        name=name,
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
        version=version,
        check_command=f"python -m pip show {name}",
        fix_candidates=(f"pip:{name}",),
    )


def _import(name: str) -> Node:
    return Node(
        id=import_id(name),
        type=NodeType.IMPORT,
        name=name,
        layer=Layer.NAMING,
        discovered_by=DiscoveredBy.STATIC_SCAN,
        check_command=f'python -c "import {name}"',
    )


def _provided_graph(pkg_name: str, pkg_ver: str, import_name: str) -> tuple[DepGraph, Node, Node]:
    """A metadata-PRESENT import: Import --requires--> Package (relink certified)."""
    pkg = _package(pkg_name, pkg_ver)
    imp = _import(import_name)
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(imp)
        .with_edge(Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="certified"))
    )
    return graph, pkg, imp


# --------------------------------------------------------------------------- #
# 1. Native soname miss — UNCHANGED (still fabricates a SystemLib).           #
# --------------------------------------------------------------------------- #
def test_native_soname_path_unchanged(fake_executor, make_result_fixture):
    graph, _pkg, imp = _provided_graph("somelib", "1.0.0", "somelib")
    fake_executor.responses = {
        'import somelib': make_result_fixture(
            returncode=1,
            stderr="ImportError: libfoo.so.1: cannot open shared object file: "
            "No such file or directory",
        )
    }

    out = import_probe(graph, fake_executor)

    syslib = out.get(syslib_id("libfoo.so.1"))
    assert syslib is not None
    assert syslib.type is NodeType.SYSTEM_LIB
    assert syslib.state is State.MISSING
    # A native miss is NOT the runtime-flag class.
    node = out.get(imp.id)
    assert node.data.get("unresolved_runtime") is not True
    assert "import_error" not in node.data


# --------------------------------------------------------------------------- #
# 2. Metadata-present, non-native failure — honest per-Import flag.           #
# --------------------------------------------------------------------------- #
def test_metadata_present_nonnative_failure_flagged(fake_executor, make_result_fixture):
    graph, _pkg, imp = _provided_graph("brokenpkg", "2.0.0", "brokenmod")
    fake_executor.responses = {
        'import brokenmod': make_result_fixture(
            returncode=1,
            stderr=(
                "Traceback (most recent call last):\n"
                '  File "<string>", line 1, in <module>\n'
                "ImportError: cannot import name 'thing' from 'brokenmod'\n"
            ),
        )
    }

    out = import_probe(graph, fake_executor)

    node = out.get(imp.id)
    assert node.data.get("unresolved_runtime") is True
    assert node.data.get("import_error")  # non-empty short reason
    # the short reason is the exception line, not the whole traceback
    assert "cannot import name 'thing'" in node.data["import_error"]
    assert "Traceback" not in node.data["import_error"]
    # honest flag, NOT a fabricated SystemLib
    assert not [n for n in out.nodes if n.type is NodeType.SYSTEM_LIB]
    # NOT the metadata-absent class
    assert node.data.get("unresolved") is not True


def test_metadata_present_runtimeerror_flagged(fake_executor, make_result_fixture):
    # A non-ImportError exception at import time (broken package) is still surfaced.
    graph, _pkg, imp = _provided_graph("brokenpkg", "2.0.0", "brokenmod")
    fake_executor.responses = {
        'import brokenmod': make_result_fixture(
            returncode=1,
            stderr="RuntimeError: broken",
        )
    }

    out = import_probe(graph, fake_executor)

    node = out.get(imp.id)
    assert node.data.get("unresolved_runtime") is True
    assert node.data.get("import_error") == "RuntimeError: broken"
    assert not [n for n in out.nodes if n.type is NodeType.SYSTEM_LIB]


def test_flag_preserves_other_data_keys(fake_executor, make_result_fixture):
    # Immutability: the new keys are added; every pre-existing data key survives.
    pkg = _package("brokenpkg", "2.0.0")
    imp = replace(_import("brokenmod"), data={"foo": "bar", "optional": False})
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(imp)
        .with_edge(Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="certified"))
    )
    fake_executor.responses = {
        'import brokenmod': make_result_fixture(returncode=1, stderr="RuntimeError: broken")
    }

    out = import_probe(graph, fake_executor)

    node = out.get(imp.id)
    assert node.data.get("unresolved_runtime") is True
    assert node.data.get("foo") == "bar"          # preserved
    assert node.data.get("optional") is False     # preserved
    # original graph untouched (immutability)
    assert graph.get(imp.id).data.get("unresolved_runtime") is None


# --------------------------------------------------------------------------- #
# 3. Metadata-ABSENT non-native failure — NOT double-flagged (P0.3 class).    #
# --------------------------------------------------------------------------- #
def test_metadata_absent_not_double_flagged(fake_executor, make_result_fixture):
    # An unprovided import already flagged ``unresolved`` (P0.3) that ALSO fails to
    # import for a non-native reason must NOT additionally get ``unresolved_runtime``
    # — it is the metadata-absence class; the flag is scoped to provided imports.
    imp = replace(_import("ghostmod"), data={"unresolved": True})
    graph = DepGraph().with_node(imp)  # NO Package, NO provider edge
    fake_executor.responses = {
        'import ghostmod': make_result_fixture(
            returncode=1,
            stderr="ModuleNotFoundError: No module named 'ghostmod'",
        )
    }

    out = import_probe(graph, fake_executor)

    node = out.get(imp.id)
    assert node.data.get("unresolved") is True          # P0.3 flag intact
    assert node.data.get("unresolved_runtime") is not True  # NOT double-flagged
    assert "import_error" not in node.data
    assert not [n for n in out.nodes if n.type is NodeType.SYSTEM_LIB]


def test_unprovided_unflagged_import_not_flagged(fake_executor, make_result_fixture):
    # An unprovided import that is NOT flagged ``unresolved`` (e.g. an optional
    # import exempted by P0.3) is still metadata-ABSENT (no provider edge) and must
    # NOT get the runtime flag — provided-ness is the gate.
    imp = replace(_import("optmod"), data={"optional": True})
    graph = DepGraph().with_node(imp)  # NO provider edge
    fake_executor.responses = {
        'import optmod': make_result_fixture(
            returncode=1,
            stderr="ModuleNotFoundError: No module named 'optmod'",
        )
    }

    out = import_probe(graph, fake_executor)

    node = out.get(imp.id)
    assert node.data.get("unresolved_runtime") is not True
    assert "import_error" not in node.data


# --------------------------------------------------------------------------- #
# 4. Clean import (rc 0) — no flag.                                           #
# --------------------------------------------------------------------------- #
def test_clean_import_no_runtime_flag(fake_executor, make_result_fixture):
    graph, _pkg, imp = _provided_graph("goodpkg", "1.0.0", "goodmod")
    fake_executor.responses = {
        'import goodmod': make_result_fixture(returncode=0, stdout="")
    }

    out = import_probe(graph, fake_executor)

    node = out.get(imp.id)
    assert node.data.get("unresolved_runtime") is not True
    assert "import_error" not in node.data
    assert not [n for n in out.nodes if n.type is NodeType.SYSTEM_LIB]
