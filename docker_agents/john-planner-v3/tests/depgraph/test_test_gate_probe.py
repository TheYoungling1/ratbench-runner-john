"""test_gate_probe: the pytest gate's stderr -> apt-resolved SystemLib sonames.

The dlopen-tail oracle. ldd (DT_NEEDED) + import (eager module-init) are a
PARTIAL backstop; a lib reached only via a feature-gated dlopen surfaces solely
when a test exercises it. These cases assert that path end-to-end, reusing the
same extract_needs + _ingest_need machinery as import_probe.
"""
from __future__ import annotations

from python_deps.depgraph.emit import _is_reciped
from python_deps.depgraph.ids import TEST_NODE_ID, syslib_id
from python_deps.depgraph.probe import test_gate_probe
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, EdgeType, Layer, Node, NodeType, State,
)


def _test_node() -> Node:
    return Node(
        id=TEST_NODE_ID, type=NodeType.TEST, name="repo tests pass",
        layer=Layer.TESTS, discovered_by=DiscoveredBy.STATIC_SCAN,
        check_command="pytest -q",
    )


# The brief-required assertion: a test-phase soname failure becomes a soname need.
def test_libcudnn_test_failure_becomes_soname_node():
    stderr = (
        "onnxruntime/capi/_pybind_state.py:32: in <module>\n"
        "ImportError: libcudnn.so.8: cannot open shared object file: "
        "No such file or directory\n"
    )
    out = test_gate_probe(DepGraph(), None, stderr)
    node = out.get(syslib_id("libcudnn.so.8"))
    assert node is not None
    assert node.type is NodeType.SYSTEM_LIB
    assert node.name == "libcudnn.so.8"
    # Not in PROVIDER_TABLE and no executor -> discovered but unresolved (honest:
    # the tail is CAUGHT even when the provider is unknown; cf. ldd_probe).
    assert node.chosen_fix is None


def test_table_soname_resolves_apt_and_is_renderable():
    # libGL.so.1 is a known dlopen tail (opencv/Qt) — table hit, no container.
    stderr = "ImportError: libGL.so.1: cannot open shared object file"
    out = test_gate_probe(DepGraph(), None, stderr)
    node = out.get(syslib_id("libGL.so.1"))
    assert node.chosen_fix == "apt:libgl1"
    assert _is_reciped(node)  # now emittable into setup.sh


def test_requires_edge_from_test_node():
    stderr = "ImportError: libGL.so.1: cannot open shared object file"
    out = test_gate_probe(DepGraph().with_node(_test_node()), None, stderr)
    edges = [e for e in out.edges
             if e.dst == syslib_id("libGL.so.1") and e.relation is EdgeType.REQUIRES]
    assert len(edges) == 1
    assert edges[0].src == TEST_NODE_ID


def test_apt_file_fallback_reused_for_unknown_soname(fake_executor, make_result_fixture):
    # Reuse the os_resolver apt-file fallback (Executor path) for a soname not in
    # the table but present in Debian Contents.
    fake_executor.responses = {
        "command -v apt-file": make_result_fixture(returncode=0),
        "apt-file search": make_result_fixture(
            returncode=0,
            stdout="libfoo7: /usr/lib/x86_64-linux-gnu/libfoo.so.7\n",
        ),
    }
    stderr = "OSError: libfoo.so.7: cannot open shared object file"
    out = test_gate_probe(DepGraph(), fake_executor, stderr)
    assert out.get(syslib_id("libfoo.so.7")).chosen_fix == "apt:libfoo7"


def test_data_file_and_assertion_noise_produce_no_soname():
    # A missing DATA file that mentions a .so, and an assertion — neither is a
    # runtime shared-object gap (precision guard; SONAME_RES is anchored).
    stderr = (
        "note: libhelper.so referenced in conftest.py\n"
        "FileNotFoundError: [Errno 2] No such file or directory: 'share/proj/proj.db'\n"
        "AssertionError: expected 3 got 2\n"
    )
    out = test_gate_probe(DepGraph(), None, stderr)
    assert [n for n in out.nodes if n.type is NodeType.SYSTEM_LIB] == []


def test_returns_new_graph_immutability():
    g0 = DepGraph()
    g1 = test_gate_probe(g0, None, "ImportError: libGL.so.1: cannot open shared object file")
    assert g0.nodes == ()          # input untouched
    assert g1 is not g0


def test_gate_probe_logs_dlopen_tail(caplog):
    import logging as _log
    stderr = "ImportError: libGL.so.1: cannot open shared object file"
    with caplog.at_level(_log.INFO, logger="python_deps.depgraph.probe"):
        test_gate_probe(DepGraph(), None, stderr)
    line = next(r.getMessage() for r in caplog.records if "test_gate: dlopen-tail" in r.getMessage())
    assert "soname=libGL.so.1" in line and "fix=apt:libgl1" in line
