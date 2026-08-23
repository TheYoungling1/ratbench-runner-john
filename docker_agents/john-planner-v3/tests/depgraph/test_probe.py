"""Stage 4 probing: discover SystemLib / Tool nodes from install/import output.

All tests use ``FakeExecutor`` (from conftest) — no Docker, no network, no real
``pip``/``python``.  Probing is discovery-only here (no remediation loop); the
host certifies the fix later (Task 8).
"""

from __future__ import annotations

import subprocess

from python_deps.depgraph.ids import binary_id, import_id, package_id, pkgconfig_id, syslib_id
from python_deps.depgraph.os_resolver import ObservedNeed, check_command_for
from python_deps.depgraph.probe import import_probe, install_closure, reconcile_predicted
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


# --------------------------------------------------------------------------- #
# install_closure: build-time gaps -> Tool nodes                              #
# --------------------------------------------------------------------------- #
def test_install_closure_build_gap_creates_tool_node(fake_executor, make_result_fixture):
    pkg = _package("psycopg2", "2.9.9")
    graph = DepGraph().with_node(pkg)
    fake_executor.responses = {
        "pip install": make_result_fixture(
            returncode=1,
            stderr="    Error: pg_config executable not found.\n",
        )
    }

    out = install_closure(graph, fake_executor)

    tool = out.get(binary_id("pg_config"))
    assert tool is not None
    assert tool.type is NodeType.TOOL
    assert tool.name == "pg_config"
    assert tool.layer is Layer.TOOLCHAIN
    assert tool.discovered_by is DiscoveredBy.PROBE
    assert tool.state is State.MISSING
    assert tool.fix_candidates == ("apt:libpq-dev",)
    assert "pg_config executable not found" in (tool.evidence or "")
    assert tool.check_command == "command -v pg_config"


def test_install_closure_links_tool_from_owning_package(fake_executor, make_result_fixture):
    pkg = _package("psycopg2", "2.9.9")
    graph = DepGraph().with_node(pkg)
    fake_executor.responses = {
        "pip install": make_result_fixture(
            returncode=1, stderr="Error: pg_config executable not found."
        )
    }

    out = install_closure(graph, fake_executor)

    deps = out.requires_of(pkg.id)
    assert any(d.id == binary_id("pg_config") for d in deps)


def test_install_closure_records_attempt_on_packages(fake_executor, make_result_fixture):
    pkg = _package("psycopg2", "2.9.9")
    graph = DepGraph().with_node(pkg)
    fake_executor.responses = {
        "pip install": make_result_fixture(
            returncode=1, stderr="Error: pg_config executable not found."
        )
    }

    out = install_closure(graph, fake_executor)

    node = out.get(pkg.id)
    assert len(node.attempts) == 1
    assert node.attempts[0].command.startswith("python -m pip install")
    assert node.attempts[0].outcome == "failed"
    # the command pins the resolved version
    assert "psycopg2==2.9.9" in node.attempts[0].command


def test_make_syslib_node_is_self_contained():
    # A probe-discovered SystemLib must carry chosen_fix, provenance, and evidence
    # so an agent can diagnose+fix it without traversing to the import node.
    from python_deps.depgraph.probe import _make_syslib_node

    node = _make_syslib_node(
        "libxcb.so.1",
        "ImportError: libxcb.so.1: cannot open shared object file",
        'python -c "import cv2"',
        apt="libxcb1",
    )
    assert node.fix_candidates == ("apt:libxcb1",)
    assert node.chosen_fix == "apt:libxcb1"
    assert node.provenance  # records that this was probe-observed
    assert "libxcb.so.1" in (node.evidence or "")


def test_make_capability_node_is_self_contained(fake_executor):
    from python_deps.depgraph.os_resolver import ObservedNeed
    from python_deps.depgraph.probe import _make_capability_node

    need = ObservedNeed(kind="binary", name="pg_config", context="build")
    node = _make_capability_node(
        need, "Error: pg_config executable not found.",
        "python -m pip install psycopg2", fake_executor,
    )
    assert node.chosen_fix == "apt:libpq-dev"
    assert node.provenance
    assert node.data["kind"] == "binary"
    assert node.data["context"] == "build"


def test_failed_build_packages_parses_pip_patterns():
    from python_deps.depgraph.probe import _failed_build_packages

    assert _failed_build_packages("Failed building wheel for picamera\n") == {"picamera"}
    assert _failed_build_packages(
        "ERROR: Could not build wheels for psycopg2, pymssql, which is required\n"
    ) == {"psycopg2", "pymssql"}
    assert _failed_build_packages(
        "ERROR: Failed to build installable wheels for some pyproject.toml "
        "based projects (lxml, pyzbar)\n"
    ) == {"lxml", "pyzbar"}
    # names are canonicalized (PEP 503) so they match Package node names
    assert _failed_build_packages("Failed building wheel for Py_Cool.Lib\n") == {"py-cool-lib"}
    assert _failed_build_packages("some generic resolution error") == set()


def test_install_closure_drops_build_failing_package_and_reinstalls_survivors(
    fake_executor, make_result_fixture
):
    # A multi-package closure where ONE package (picamera) cannot build must not
    # starve the survivors: drop it and reinstall numpy + opencv-python so the
    # downstream probe/relink see real installed packages.
    numpy = _package("numpy", "2.0.0")
    opencv = _package("opencv-python", "4.9.0.80")
    picamera = _package("picamera", "1.13")
    graph = DepGraph().with_node(numpy).with_node(opencv).with_node(picamera)
    fake_executor.responses = {
        # the bulk install (which includes picamera) fails on picamera's build
        "picamera": make_result_fixture(
            returncode=1,
            stderr=(
                "Building wheel for picamera (setup.py): started\n"
                "      error: this package requires a Raspberry Pi\n"
                "Failed building wheel for picamera\n"
                "ERROR: Could not build wheels for picamera, which is required\n"
            ),
        ),
    }
    # the survivor reinstall (no picamera) succeeds
    fake_executor.default = make_result_fixture(returncode=0, stdout="Successfully installed")

    out = install_closure(graph, fake_executor)

    install_cmds = [c for c in fake_executor.calls if "pip install" in c]
    assert len(install_cmds) == 2  # bulk (failed) + survivor reinstall
    retry = install_cmds[1]
    assert "numpy==2.0.0" in retry and "opencv-python==4.9.0.80" in retry
    assert "picamera" not in retry
    # survivors ended up installed; the build-failing package did not
    assert any(a.outcome == "succeeded" for a in out.get(opencv.id).attempts)
    assert any(a.outcome == "succeeded" for a in out.get(numpy.id).attempts)
    assert all(a.outcome == "failed" for a in out.get(picamera.id).attempts)


class _SeqExecutor:
    """Executor returning canned results by call order (the substring FakeExecutor
    can't distinguish a command from a subset-command across reinstall rounds)."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run(self, command, *, timeout=300):
        self.calls.append(command)
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


def test_install_closure_reinstalls_across_multiple_rounds(make_result_fixture):
    # Round 0 (all 3) fails on Bravo_Pkg; round 1 (alpha+charlie) then fails on
    # charlie; round 2 (alpha) succeeds. Exercises the bounded multi-round retry
    # AND node-name normalization (node `Bravo_Pkg` matched by canonical `bravo-pkg`).
    alpha = _package("alpha", "1.0")
    bravo = _package("Bravo_Pkg", "1.0")
    charlie = _package("charlie", "1.0")
    graph = DepGraph().with_node(alpha).with_node(bravo).with_node(charlie)
    ex = _SeqExecutor([
        make_result_fixture(returncode=1, stderr="Failed building wheel for bravo-pkg\n"),
        make_result_fixture(returncode=1, stderr="Failed building wheel for charlie\n"),
        make_result_fixture(returncode=0, stdout="Successfully installed"),
    ])

    out = install_closure(graph, ex)

    install_cmds = [c for c in ex.calls if "pip install" in c]
    assert len(install_cmds) == 3  # bulk + two survivor rounds
    final = install_cmds[-1]
    assert "alpha==1.0" in final
    assert "Bravo_Pkg" not in final and "charlie" not in final
    assert any(a.outcome == "succeeded" for a in out.get(alpha.id).attempts)
    assert all(a.outcome == "failed" for a in out.get(bravo.id).attempts)
    assert all(a.outcome == "failed" for a in out.get(charlie.id).attempts)


def test_install_closure_single_failing_package_no_retry(fake_executor, make_result_fixture):
    # When the ONLY package fails to build there are no survivors -> no retry,
    # and the existing build-tool-gap discovery still fires (no regression).
    psy = _package("psycopg2", "2.9.9")
    graph = DepGraph().with_node(psy)
    fake_executor.responses = {
        "pip install": make_result_fixture(
            returncode=1,
            stderr="Failed building wheel for psycopg2\nError: pg_config executable not found.\n",
        )
    }

    out = install_closure(graph, fake_executor)

    install_cmds = [c for c in fake_executor.calls if "pip install" in c]
    assert len(install_cmds) == 1  # only package failed -> nothing to retry
    assert out.get(binary_id("pg_config")) is not None  # tool gap still surfaced


def test_install_closure_clean_install_no_tool_nodes(fake_executor, make_result_fixture):
    pkg = _package("requests", "2.31.0")
    graph = DepGraph().with_node(pkg)
    fake_executor.responses = {
        "pip install": make_result_fixture(returncode=0, stdout="Successfully installed")
    }

    out = install_closure(graph, fake_executor)

    assert not [n for n in out.nodes if n.type is NodeType.TOOL]
    node = out.get(pkg.id)
    assert node.attempts[0].outcome == "succeeded"


def test_install_closure_unknown_tool_has_empty_fix(fake_executor, make_result_fixture):
    # ``cc`` substring must NOT spuriously match inside ``gcc`` (word-boundary).
    pkg = _package("psycopg2", "2.9.9")
    graph = DepGraph().with_node(pkg)
    fake_executor.responses = {
        "pip install": make_result_fixture(
            returncode=1, stderr="gcc: command not found"
        )
    }

    out = install_closure(graph, fake_executor)

    assert out.get(binary_id("gcc")) is not None
    assert out.get(binary_id("cc")) is None  # not a false positive from "gcc"


# --------------------------------------------------------------------------- #
# install_closure: pkg-config build gaps -> pkgconfig capability nodes        #
# --------------------------------------------------------------------------- #
def test_install_closure_table_independent_unknown_header(fake_executor, make_result_fixture):
    # A header NOT in PROVIDER_TABLE is discovered anyway (via extract_needs) and
    # resolved through the apt-file fallback — the whole point of the extractor.
    from python_deps.depgraph.ids import header_id
    pkg = _package("hiredis", "2.3.2")
    graph = DepGraph().with_node(pkg)
    fake_executor.responses = {
        "pip install": make_result_fixture(
            returncode=1,
            stderr=(
                "Building wheel for hiredis\n"
                "  fatal error: hiredis/hiredis.h: No such file or directory\n"
            ),
        ),
        "command -v apt-file": make_result_fixture(stdout="/usr/bin/apt-file"),
        "apt-file search hiredis/hiredis.h": make_result_fixture(
            stdout="libhiredis-dev: /usr/include/hiredis/hiredis.h\n"
        ),
    }

    out = install_closure(graph, fake_executor)

    node = out.get(header_id("hiredis/hiredis.h"))
    assert node is not None
    assert node.chosen_fix == "apt:libhiredis-dev"
    assert any(d.id == node.id for d in out.requires_of(pkg.id))


def test_install_closure_ignores_bare_tool_mention(fake_executor, make_result_fixture):
    # The false positive the old _tool_gaps produced: a bare 'pg_config' mention in
    # a build log (no not-found signature) must NOT create a phantom tool node.
    from python_deps.depgraph.ids import binary_id
    pkg = _package("psycopg2", "2.9.9")
    graph = DepGraph().with_node(pkg)
    fake_executor.responses = {
        "pip install": make_result_fixture(
            returncode=1,
            stderr=(
                "Building wheel for psycopg2\n"
                "  Using pg_config at /usr/bin/pg_config\n"
                "  error: some unrelated compile error\n"
            ),
        )
    }

    out = install_closure(graph, fake_executor)

    assert out.get(binary_id("pg_config")) is None


def test_install_closure_pkgconfig_gap_creates_node_via_apt_file(
    fake_executor, make_result_fixture
):
    # gobject-introspection-1.0 is NOT in PROVIDER_TABLE (unseeded) -> the
    # apt-file fallback must resolve it.
    pkg = _package("pygobject", "3.46.0")
    graph = DepGraph().with_node(pkg)
    fake_executor.responses = {
        "pip install": make_result_fixture(
            returncode=1,
            stderr=(
                "Building wheel for pygobject\n"
                "  No package 'gobject-introspection-1.0' found\n"
            ),
        ),
        "command -v apt-file": make_result_fixture(stdout="/usr/bin/apt-file"),
        "apt-file search gobject-introspection-1.0.pc": make_result_fixture(
            stdout=(
                "libgirepository1.0-dev: "
                "/usr/lib/x86_64-linux-gnu/pkgconfig/gobject-introspection-1.0.pc\n"
            )
        ),
    }

    out = install_closure(graph, fake_executor)

    node = out.get(pkgconfig_id("gobject-introspection-1.0"))
    assert node is not None
    assert node.chosen_fix == "apt:libgirepository1.0-dev"
    assert node.check_command == "pkg-config --exists gobject-introspection-1.0"
    assert any(d.id == node.id for d in out.requires_of(pkg.id))


def test_install_closure_linker_lib_resolves_to_dev(fake_executor, make_result_fixture):
    # Build-time `cannot find -lssl` -> the UNVERSIONED libssl.so -> a -dev
    # package; the directional opposite of runtime soname resolution.
    from python_deps.depgraph.ids import linker_id
    pkg = _package("pycrypto", "2.6.1")
    graph = DepGraph().with_node(pkg)
    fake_executor.responses = {
        "pip install": make_result_fixture(
            returncode=1,
            stderr=(
                "Building wheel for pycrypto\n"
                "  /usr/bin/ld: cannot find -lssl\n"
            ),
        ),
        "command -v apt-file": make_result_fixture(stdout="/usr/bin/apt-file"),
        "apt-file search libssl.so": make_result_fixture(
            stdout=(
                "libssl-dev: /usr/lib/x86_64-linux-gnu/libssl.so\n"
                "libssl3: /usr/lib/x86_64-linux-gnu/libssl.so.3\n"
            )
        ),
    }

    out = install_closure(graph, fake_executor)

    node = out.get(linker_id("ssl"))
    assert node is not None
    assert node.type is NodeType.TOOL
    assert node.chosen_fix == "apt:libssl-dev"
    assert any(d.id == node.id for d in out.requires_of(pkg.id))


def test_install_closure_reconciles_predicted_pkgconfig(fake_executor, make_result_fixture):
    # dbus-python predicted pkgconfig:dbus-1 (curated build-dep prior); build
    # observes the same pkg-config gap post-install, so the two collapse onto
    # ONE node instead of two.
    pkg = _package("dbus-python", "1.3.2")
    predicted = _predicted_pkgconfig("dbus-1", "libdbus-1-dev")
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(predicted)
        .with_edge(Edge(src=pkg.id, dst=predicted.id, relation=EdgeType.REQUIRES, origin="resolver"))
    )
    fake_executor.responses = {
        "pip install": make_result_fixture(
            returncode=1, stderr="No package 'dbus-1' found\n"
        )
    }

    out = install_closure(graph, fake_executor)

    node = out.get(pkgconfig_id("dbus-1"))
    assert node is not None
    assert node.discovered_by is DiscoveredBy.RESOLVER  # discovery origin kept
    assert node.check_command == "pkg-config --exists dbus-1"
    assert "dbus-1" in (node.evidence or "")
    assert node.chosen_fix == "apt:libdbus-1-dev"
    # exactly one node exists at this id — the collapse holds, no fresh duplicate
    assert len([n for n in out.nodes if n.id == pkgconfig_id("dbus-1")]) == 1


# --------------------------------------------------------------------------- #
# check_command_for: header check must fail rc!=0 when the header is absent   #
# --------------------------------------------------------------------------- #
def test_header_check_exits_nonzero_when_absent():
    need = ObservedNeed("header", "definitely_absent_xyz.h", context="build")
    cmd = check_command_for(need)
    rc = subprocess.run(cmd, shell=True).returncode
    assert rc != 0, "absent header must NOT certify"


def test_header_check_is_shell_find_not_python_print():
    # Regression guard: the old python "print()"-based check silently returned
    # rc 0 even when the header was absent; the `find | grep -q .` shell
    # pipeline check_command_for emits does not have that footgun.
    need = ObservedNeed("header", "Python.h", context="build")
    cmd = check_command_for(need)
    assert "find" in cmd
    assert "print(" not in cmd


# --------------------------------------------------------------------------- #
# import_probe: run-time gaps -> SystemLib nodes                              #
# --------------------------------------------------------------------------- #
def test_import_probe_native_lib_creates_syslib(fake_executor, make_result_fixture):
    pkg = _package("opencv-python", "4.9.0.80")
    imp = _import("cv2")
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(imp)
        .with_edge(Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="resolver"))
    )
    fake_executor.responses = {
        'import cv2': make_result_fixture(
            returncode=1,
            stderr="ImportError: libGL.so.1: cannot open shared object file: "
            "No such file or directory",
        )
    }

    out = import_probe(graph, fake_executor)

    syslib = out.get(syslib_id("libGL.so.1"))
    assert syslib is not None
    assert syslib.type is NodeType.SYSTEM_LIB
    assert syslib.name == "libGL.so.1"
    assert syslib.layer is Layer.SYSTEM
    assert syslib.discovered_by is DiscoveredBy.PROBE
    assert syslib.state is State.MISSING
    assert syslib.fix_candidates == ("apt:libgl1",)
    assert "libGL.so.1" in (syslib.evidence or "")
    assert syslib.check_command == "ldconfig -p | grep libGL.so.1"


def test_import_probe_links_syslib_from_owning_package(fake_executor, make_result_fixture):
    pkg = _package("opencv-python", "4.9.0.80")
    imp = _import("cv2")
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(imp)
        .with_edge(Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="resolver"))
    )
    fake_executor.responses = {
        'import cv2': make_result_fixture(
            returncode=1,
            stderr="ImportError: libGL.so.1: cannot open shared object file",
        )
    }

    out = import_probe(graph, fake_executor)

    deps = out.requires_of(pkg.id)
    assert any(d.id == syslib_id("libGL.so.1") for d in deps)


def test_import_probe_records_attempt_on_import(fake_executor, make_result_fixture):
    pkg = _package("opencv-python", "4.9.0.80")
    imp = _import("cv2")
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(imp)
        .with_edge(Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="resolver"))
    )
    fake_executor.responses = {
        'import cv2': make_result_fixture(
            returncode=1, stderr="ImportError: libGL.so.1: cannot open shared object file"
        )
    }

    out = import_probe(graph, fake_executor)

    node = out.get(imp.id)
    assert len(node.attempts) == 1
    assert node.attempts[0].command == 'python -c "import cv2"'
    assert node.attempts[0].outcome == "failed"


def test_import_probe_clean_import_no_syslib(fake_executor, make_result_fixture):
    pkg = _package("numpy", "1.26.4")
    imp = _import("numpy")
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(imp)
        .with_edge(Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="resolver"))
    )
    fake_executor.responses = {
        'import numpy': make_result_fixture(returncode=0, stdout="")
    }

    out = import_probe(graph, fake_executor)

    assert not [n for n in out.nodes if n.type is NodeType.SYSTEM_LIB]
    node = out.get(imp.id)
    assert node.attempts[0].outcome == "succeeded"


def test_import_probe_non_native_failure_creates_no_syslib(
    fake_executor, make_result_fixture
):
    # A plain ModuleNotFoundError is NOT a native-lib gap -> no SystemLib node.
    pkg = _package("lxml", "5.2.1")
    imp = _import("lxml")
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(imp)
        .with_edge(Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="resolver"))
    )
    fake_executor.responses = {
        'import lxml': make_result_fixture(
            returncode=1, stderr="ModuleNotFoundError: No module named 'lxml'"
        )
    }

    out = import_probe(graph, fake_executor)

    assert not [n for n in out.nodes if n.type is NodeType.SYSTEM_LIB]


def test_import_probe_native_risk_package_probed_by_name(fake_executor, make_result_fixture):
    # psycopg2 is a native-risk package whose import name == package name; it is
    # probed even with no static Import node, and its syslib links back to it.
    pkg = _package("psycopg2", "2.9.9")
    graph = DepGraph().with_node(pkg)
    fake_executor.responses = {
        'import psycopg2': make_result_fixture(
            returncode=1,
            stderr="ImportError: libpq.so.5: cannot open shared object file",
        )
    }

    out = import_probe(graph, fake_executor)

    syslib = out.get(syslib_id("libpq.so.5"))
    assert syslib is not None
    assert syslib.fix_candidates == ("apt:libpq5",)
    deps = out.requires_of(pkg.id)
    assert any(d.id == syslib_id("libpq.so.5") for d in deps)


def test_import_probe_widens_to_binary_gap(fake_executor, make_result_fixture):
    # A C-extension import that fails on a missing *binary* (not a .so) now
    # surfaces a binary Tool node — import_probe is no longer soname-only.
    from python_deps.depgraph.ids import binary_id
    pkg = _package("somepkg", "1.0.0")
    imp = _import("somepkg")
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(imp)
        .with_edge(Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="resolver"))
    )
    fake_executor.responses = {
        "import somepkg": make_result_fixture(
            returncode=1, stderr="bash: llvm-config: command not found",
        )
    }

    out = import_probe(graph, fake_executor)

    tool = out.get(binary_id("llvm-config"))
    assert tool is not None
    assert tool.type is NodeType.TOOL
    assert any(d.id == tool.id for d in out.requires_of(pkg.id))


def test_import_probe_surfaces_all_sonames_not_just_first(fake_executor, make_result_fixture):
    pkg = _package("opencv-python", "4.9.0.80")
    imp = _import("cv2")
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(imp)
        .with_edge(Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="resolver"))
    )
    fake_executor.responses = {
        "import cv2": make_result_fixture(
            returncode=1,
            stderr=(
                "ImportError: libGL.so.1: cannot open shared object file\n"
                "ImportError: libSM.so.6: cannot open shared object file\n"
            ),
        )
    }

    out = import_probe(graph, fake_executor)

    assert out.get(syslib_id("libGL.so.1")) is not None
    assert out.get(syslib_id("libSM.so.6")) is not None


def _predicted_syslib(key: str) -> Node:
    """A resolver-predicted SystemLib node keyed by ``key`` — post Task 9,
    callers pass the canonical SONAME (mirrors ``seed._predicted_syslib_node``).
    """
    return Node(
        id=syslib_id(key),
        type=NodeType.SYSTEM_LIB,
        name=key,
        layer=Layer.SYSTEM,
        discovered_by=DiscoveredBy.RESOLVER,  # a prediction
        state=State.UNKNOWN,
        check_command=f"dpkg -s {key}",
        fix_candidates=(f"apt:{key}",),
    )


def _predicted_tool(name: str, apt: str) -> Node:
    """A resolver-predicted capability Tool node — mirrors
    ``build_deps._capability_node`` (Task 5): keyed by ``capability_id``
    (``binary:``/``header:``), not the apt package name.
    """
    return Node(
        id=binary_id(name),
        type=NodeType.TOOL,
        name=name,
        layer=Layer.TOOLCHAIN,
        discovered_by=DiscoveredBy.RESOLVER,  # a prediction
        state=State.UNKNOWN,
        check_command=f"command -v {name}",
        fix_candidates=(f"apt:{apt}",),
        chosen_fix=f"apt:{apt}",
    )


def _predicted_pkgconfig(name: str, apt: str) -> Node:
    """A resolver-predicted pkgconfig capability node — mirrors
    ``build_deps._capability_node`` (Task 5): keyed by ``capability_id``
    (``pkgconfig:``), not the apt package name.
    """
    return Node(
        id=pkgconfig_id(name),
        type=NodeType.TOOL,
        name=name,
        layer=Layer.TOOLCHAIN,
        discovered_by=DiscoveredBy.RESOLVER,  # a prediction
        state=State.UNKNOWN,
        check_command=f"pkg-config --exists {name}",
        fix_candidates=(f"apt:{apt}",),
        chosen_fix=f"apt:{apt}",
    )


# --------------------------------------------------------------------------- #
# Reconciliation: probe observation merges into a resolver prediction          #
# --------------------------------------------------------------------------- #
def test_import_probe_reconciles_predicted_syslib(fake_executor, make_result_fixture):
    # opencv predicted the canonical soname node (Task 9); probe observes the
    # SAME soname libGL.so.1, so reconciliation lands on ONE node.
    pkg = _package("opencv-python", "4.9.0.80")
    imp = _import("cv2")
    predicted = _predicted_syslib("libGL.so.1")
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(imp)
        .with_node(predicted)
        .with_edge(Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="resolver"))
        .with_edge(Edge(src=pkg.id, dst=predicted.id, relation=EdgeType.REQUIRES, origin="resolver"))
    )
    fake_executor.responses = {
        "import cv2": make_result_fixture(
            returncode=1,
            stderr="ImportError: libGL.so.1: cannot open shared object file",
        )
    }

    out = import_probe(graph, fake_executor)

    node = out.get(syslib_id("libGL.so.1"))
    assert node is not None
    assert node.discovered_by is DiscoveredBy.RESOLVER  # discovery origin kept
    assert node.check_command == "ldconfig -p | grep libGL.so.1"  # real check
    assert "libGL.so.1" in (node.evidence or "")
    assert node.attempts and node.attempts[-1].outcome == "failed"
    assert len([n for n in out.nodes if n.type is NodeType.SYSTEM_LIB]) == 1
    # single requires edge from the owning package (deduped)
    libs = [d for d in out.requires_of(pkg.id) if d.id == syslib_id("libGL.so.1")]
    assert len(libs) == 1


def test_import_probe_reconciles_even_when_apt_resolution_unresolved(
    fake_executor, make_result_fixture
):
    # Task 9 regression: canonical identity is the soname, so reconciliation
    # succeeds even when soname->apt resolution is ABSENT at probe time (table
    # miss + apt-file unavailable) -- no rival PROBE node.
    pkg = _package("somepkg", "1.0.0")
    imp = _import("somepkg")
    predicted = _predicted_syslib("libcustomthing.so.2")
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(imp)
        .with_node(predicted)
        .with_edge(Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="resolver"))
        .with_edge(
            Edge(src=pkg.id, dst=predicted.id, relation=EdgeType.REQUIRES, origin="resolver")
        )
    )
    fake_executor.responses = {
        'import somepkg': make_result_fixture(
            returncode=1,
            stderr="ImportError: libcustomthing.so.2: cannot open shared object file",
        )
        # No "apt-file"/"command -v apt-file" response registered -> resolve()
        # returns [] (unresolved).
    }

    out = import_probe(graph, fake_executor)

    syslibs = [n for n in out.nodes if n.id == syslib_id("libcustomthing.so.2")]
    assert len(syslibs) == 1  # single canonical node, no rival PROBE node
    assert syslibs[0].discovered_by is DiscoveredBy.RESOLVER


def test_install_closure_reconciles_predicted_tool(fake_executor, make_result_fixture):
    # psycopg2 predicted binary:pg_config (capability-keyed, Task 5's
    # seed_build_deps); build observes the same pg_config gap post-install, so
    # the two collapse onto ONE node instead of two.
    pkg = _package("psycopg2", "2.9.9")
    predicted = _predicted_tool("pg_config", "libpq-dev")
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(predicted)
        .with_edge(Edge(src=pkg.id, dst=predicted.id, relation=EdgeType.REQUIRES, origin="resolver"))
    )
    fake_executor.responses = {
        "pip install": make_result_fixture(
            returncode=1, stderr="Error: pg_config executable not found."
        )
    }

    out = install_closure(graph, fake_executor)

    node = out.get(binary_id("pg_config"))
    assert node is not None
    assert node.discovered_by is DiscoveredBy.RESOLVER
    assert node.check_command == "command -v pg_config"
    assert "pg_config" in (node.evidence or "")
    assert node.chosen_fix == "apt:libpq-dev"
    tools = [d for d in out.requires_of(pkg.id) if d.id == binary_id("pg_config")]
    assert len(tools) == 1
    # exactly one Tool node exists for this gap — the collapse holds
    assert len([n for n in out.nodes if n.type is NodeType.TOOL]) == 1


def test_reconcile_skips_non_resolver_prediction(fake_executor, make_result_fixture):
    # A pre-existing node at the predicted CAPABILITY id but discovered_by=PROBE
    # is NOT a resolver prediction: reconciliation is skipped (the guard at
    # reconcile_predicted) and a fresh observed node is created instead — which
    # lands at the SAME capability id (binary:pg_config), replacing the stale one.
    pkg = _package("psycopg2", "2.9.9")
    stale = Node(
        id=binary_id("pg_config"),
        type=NodeType.TOOL,
        name="pg_config",
        layer=Layer.TOOLCHAIN,
        discovered_by=DiscoveredBy.PROBE,  # not a prediction
        state=State.MISSING,
    )
    graph = DepGraph().with_node(pkg).with_node(stale)
    fake_executor.responses = {
        "pip install": make_result_fixture(
            returncode=1, stderr="Error: pg_config executable not found."
        )
    }

    out = install_closure(graph, fake_executor)

    observed = out.get(binary_id("pg_config"))
    assert observed is not None
    assert observed.discovered_by is DiscoveredBy.PROBE
    # the guard skipped reconciliation; the fresh probe observation carries real
    # evidence (unlike the bare stale placeholder it replaced)
    assert "pg_config" in (observed.evidence or "")


def test_reconcile_predicted_fills_chosen_fix_when_prediction_resolved_none():
    # Real defect this guards: a wheel_preflight-seeded syslib:* prior resolved
    # NO provider at seed time (chosen_fix=None); the observe-path apt-file
    # fallback later learns the apt. Spec: "observation fills a chosen_fix that
    # prediction left None" so the node becomes renderable instead of staying
    # stuck with an unrenderable None fix.
    predicted = _predicted_syslib("libcustomthing.so.2")
    assert predicted.chosen_fix is None  # seed resolved no provider
    graph = DepGraph().with_node(predicted)

    reconciled = reconcile_predicted(
        graph,
        predicted.id,
        check="ldconfig -p | grep libcustomthing.so.2",
        evidence="ImportError: libcustomthing.so.2: cannot open shared object file",
        command='python -c "import somepkg"',
        chosen_fix="apt:libcustomthing-dev",
        fix_candidates=("apt:libcustomthing-dev",),
    )

    assert reconciled is not None
    assert reconciled.id == predicted.id  # SAME canonical node, not a rival
    assert reconciled.discovered_by is DiscoveredBy.RESOLVER  # origin kept
    assert reconciled.chosen_fix == "apt:libcustomthing-dev"
    assert reconciled.fix_candidates == ("apt:libcustomthing-dev",)
    assert reconciled.data["resolution_status"] == "resolved"


def test_reconcile_predicted_does_not_override_existing_chosen_fix():
    # Complementary invariant: a prediction that already resolved a provider is
    # NEVER clobbered by a (possibly different) apt the observation resolves.
    predicted = _predicted_tool("pg_config", "libpq-dev")
    assert predicted.chosen_fix == "apt:libpq-dev"
    graph = DepGraph().with_node(predicted)

    reconciled = reconcile_predicted(
        graph,
        predicted.id,
        check="command -v pg_config",
        evidence="Error: pg_config executable not found.",
        command="python -m pip install psycopg2",
        chosen_fix="apt:some-other-package",
        fix_candidates=("apt:some-other-package",),
    )

    assert reconciled is not None
    assert reconciled.chosen_fix == "apt:libpq-dev"  # unchanged, not overridden
    assert reconciled.fix_candidates == ("apt:libpq-dev",)  # unchanged


def test_import_probe_fills_chosen_fix_left_none_by_seed(fake_executor, make_result_fixture):
    # End-to-end via the real caller: import_probe's own resolve() finds the apt
    # (table hit for libGL.so.1 -> libgl1) and reconcile_predicted must fill it
    # into the seed node that resolved none, instead of silently dropping it.
    pkg = _package("opencv-python", "4.9.0.80")
    imp = _import("cv2")
    predicted = _predicted_syslib("libGL.so.1")
    assert predicted.chosen_fix is None
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(imp)
        .with_node(predicted)
        .with_edge(Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="resolver"))
        .with_edge(Edge(src=pkg.id, dst=predicted.id, relation=EdgeType.REQUIRES, origin="resolver"))
    )
    fake_executor.responses = {
        "import cv2": make_result_fixture(
            returncode=1,
            stderr="ImportError: libGL.so.1: cannot open shared object file",
        )
    }

    out = import_probe(graph, fake_executor)

    node = out.get(syslib_id("libGL.so.1"))
    assert node is not None
    assert node.discovered_by is DiscoveredBy.RESOLVER  # reconciled, not replaced
    assert node.chosen_fix == "apt:libgl1"  # filled in, was None
    assert node.fix_candidates == ("apt:libgl1",)
    assert node.data["resolution_status"] == "resolved"


def test_install_closure_wheel_for_attribution_beats_fallback(
    fake_executor, make_result_fixture
):
    # "Building wheel for X" attributes the build-time gap to X, NOT to the
    # native-risk fallback set (psycopg2).
    target = _package("somepkg", "1.0")
    native = _package("psycopg2", "2.9.9")  # native-risk fallback candidate
    graph = DepGraph().with_node(target).with_node(native)
    fake_executor.responses = {
        "pip install": make_result_fixture(
            returncode=1,
            stderr="Building wheel for somepkg\n  Error: pg_config executable not found.",
        )
    }

    out = install_closure(graph, fake_executor)

    tool = binary_id("pg_config")
    assert any(d.id == tool for d in out.requires_of(target.id))
    assert not any(d.id == tool for d in out.requires_of(native.id))


def test_import_probe_syslib_falls_back_to_import_node(
    fake_executor, make_result_fixture
):
    # An Import node with a native-lib failure and NO owning Package: the requires
    # edge originates from the Import id (the _edge_sources fallback).
    imp = _import("cv2")
    graph = DepGraph().with_node(imp)
    fake_executor.responses = {
        "import cv2": make_result_fixture(
            returncode=1,
            stderr="ImportError: libGL.so.1: cannot open shared object file",
        )
    }

    out = import_probe(graph, fake_executor)

    syslib = syslib_id("libGL.so.1")
    assert out.get(syslib) is not None
    assert any(d.id == syslib for d in out.requires_of(imp.id))


def test_install_closure_no_packages_returns_input(fake_executor):
    # No Package nodes -> early return of the same graph object (no executor call).
    graph = DepGraph().with_node(_import("cv2"))
    out = install_closure(graph, fake_executor)
    assert out is graph
    assert fake_executor.calls == []


def test_install_closure_excludes_resolver_missing_packages(
    fake_executor, make_result_fixture
):
    # A resolver-diagnosed MISSING package (unresolvable / conflict placeholder,
    # no real version) must NOT be added to the bulk `pip install` — including it
    # makes the single command fail and poisons the whole closure (every good
    # package then certifies MISSING). It must be installed without the bad one.
    from dataclasses import replace

    good = _package("requests", "2.31.0")
    bad = replace(
        _package("does-not-exist-zzz", ""), state=State.MISSING, version=None,
        evidence="not found in the package registry",
    )
    graph = DepGraph().with_node(good).with_node(bad)
    fake_executor.responses = {
        "pip install": make_result_fixture(returncode=0, stdout="Successfully installed")
    }

    out = install_closure(graph, fake_executor)

    install_calls = [c for c in fake_executor.calls if "pip install" in c]
    assert len(install_calls) == 1
    assert "requests==2.31.0" in install_calls[0]
    assert "does-not-exist-zzz" not in install_calls[0]
    # The good package still records its (successful) install attempt; the missing
    # node is left untouched (no spurious install attempt).
    assert out.get(good.id).attempts and out.get(good.id).attempts[0].outcome == "succeeded"
    assert out.get(bad.id).attempts == ()
    assert out.get(bad.id).state is State.MISSING


def test_install_closure_uses_generous_timeout(fake_executor, make_result_fixture):
    # A cold install of a large closure can exceed the 300s default and FALSE-fail,
    # which then certifies the whole graph MISSING (breaks honest certification).
    # The bulk install must therefore ask for generous headroom.
    from python_deps.depgraph.probe import INSTALL_TIMEOUT

    pkg = _package("requests", "2.31.0")
    graph = DepGraph().with_node(pkg)
    fake_executor.responses = {
        "pip install": make_result_fixture(returncode=0, stdout="Successfully installed")
    }

    install_closure(graph, fake_executor)

    install_calls = [
        i for i, c in enumerate(fake_executor.calls) if "pip install" in c
    ]
    assert install_calls, "expected a pip install call"
    assert fake_executor.timeouts[install_calls[0]] == INSTALL_TIMEOUT
    assert INSTALL_TIMEOUT >= 600


def test_import_probe_probes_each_name_once(fake_executor, make_result_fixture):
    # An Import node and a native-risk Package of the SAME name dedup to one probe.
    pkg = _package("psycopg2", "2.9.9")
    imp = _import("psycopg2")
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(imp)
        .with_edge(
            Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="resolver")
        )
    )
    fake_executor.responses = {
        "import psycopg2": make_result_fixture(returncode=0, stdout="")
    }

    import_probe(graph, fake_executor)

    probes = [c for c in fake_executor.calls if 'import psycopg2' in c]
    assert len(probes) == 1


def test_probe_returns_new_graph_originals_unchanged(fake_executor, make_result_fixture):
    pkg = _package("opencv-python", "4.9.0.80")
    imp = _import("cv2")
    graph = (
        DepGraph()
        .with_node(pkg)
        .with_node(imp)
        .with_edge(Edge(src=imp.id, dst=pkg.id, relation=EdgeType.REQUIRES, origin="resolver"))
    )
    fake_executor.responses = {
        'import cv2': make_result_fixture(
            returncode=1, stderr="ImportError: libGL.so.1: cannot open shared object file"
        )
    }

    out = import_probe(graph, fake_executor)

    assert out is not graph
    assert graph.get(syslib_id("libGL.so.1")) is None  # original untouched
    assert len(graph.get(imp.id).attempts) == 0


class _ContentExecutor:
    """Fails any `pip install` whose command contains a build-failing spec.
    A package that re-pulls the failing one (a requirer) therefore also fails,
    naming the failing wheel in stderr — modelling pip's transitive re-pull."""

    def __init__(self, make_result, fail_substrings, fail_stderr):
        self.calls = []
        self._mk = make_result
        self._fail = list(fail_substrings)
        self._stderr = fail_stderr

    def run(self, command, *, timeout=300):
        self.calls.append(command)
        if "pip install" in command and any(s in command for s in self._fail):
            return self._mk(returncode=1, stderr=self._stderr)
        return self._mk(returncode=0, stdout="Successfully installed")


def test_survivor_salvage_drops_direct_requirer_of_failed_build(make_result_fixture):
    # requirer --requires--> failing (un-buildable). Dropping `failing` alone leaves
    # `requirer`, which re-pulls it. The salvage must drop BOTH and install `clean`.
    clean = _package("clean-pkg", "1.0")
    failing = _package("failing-pkg", "1.0")
    requirer = _package("requirer-pkg", "1.0")
    graph = (
        DepGraph().with_node(clean).with_node(failing).with_node(requirer)
        .with_edge(Edge(src=requirer.id, dst=failing.id,
                        relation=EdgeType.REQUIRES, origin="resolver"))
    )
    ex = _ContentExecutor(
        make_result_fixture,
        fail_substrings=["failing-pkg", "requirer-pkg"],   # requirer re-pulls failing
        fail_stderr="Failed building wheel for failing-pkg\n",
    )

    out = install_closure(graph, ex)

    install_cmds = [c for c in ex.calls if "pip install" in c]
    final = install_cmds[-1]
    assert "clean-pkg==1.0" in final
    assert "failing-pkg" not in final and "requirer-pkg" not in final
    assert any(a.outcome == "succeeded" for a in out.get(clean.id).attempts)


def test_survivor_salvage_drops_transitive_requirer_chain(make_result_fixture):
    # grand --requires--> mid --requires--> failing. ALL of mid+grand must be dropped.
    clean = _package("clean-pkg", "1.0")
    failing = _package("failing-pkg", "1.0")
    mid = _package("mid-pkg", "1.0")
    grand = _package("grand-pkg", "1.0")
    graph = (
        DepGraph().with_node(clean).with_node(failing).with_node(mid).with_node(grand)
        .with_edge(Edge(src=mid.id, dst=failing.id, relation=EdgeType.REQUIRES, origin="resolver"))
        .with_edge(Edge(src=grand.id, dst=mid.id, relation=EdgeType.REQUIRES, origin="resolver"))
    )
    ex = _ContentExecutor(
        make_result_fixture,
        fail_substrings=["failing-pkg", "mid-pkg", "grand-pkg"],  # all re-pull failing
        fail_stderr="Failed building wheel for failing-pkg\n",
    )

    out = install_closure(graph, ex)

    final = [c for c in ex.calls if "pip install" in c][-1]
    assert "clean-pkg==1.0" in final
    for dropped in ("failing-pkg", "mid-pkg", "grand-pkg"):
        assert dropped not in final
    assert any(a.outcome == "succeeded" for a in out.get(clean.id).attempts)


def test_import_probe_unknown_soname_uses_apt_file_fallback(fake_executor, make_result_fixture):
    # An import whose runtime gap is a soname NOT in the curated table.
    imp = _import("widget")
    graph = DepGraph().with_node(imp)
    fake_executor.responses = {
        'python -c "import widget"': make_result_fixture(
            returncode=1,
            stderr="ImportError: libwidget.so.3: cannot open shared object file",
        ),
        "command -v apt-file": make_result_fixture(returncode=0),
        "apt-file search": make_result_fixture(
            stdout="libwidget3: /usr/lib/x86_64-linux-gnu/libwidget.so.3\n"
        ),
    }

    out = import_probe(graph, fake_executor)

    lib = out.get(syslib_id("libwidget.so.3"))
    assert lib is not None
    assert lib.type is NodeType.SYSTEM_LIB
    assert lib.state is State.MISSING
    assert lib.fix_candidates == ("apt:libwidget3",)
