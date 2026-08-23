"""Witness the residual-node-drop.md root cause: the co-occurrence guard
(diagnose.py's ``_RESIDUAL_RE``) keys on whether the literal token
``AssertionError`` appears anywhere in the SAME blob as a FileNotFoundError
tool signal. When it is absent (a pager-style test that *errors* through the
subprocess rather than asserting), the deterministic runtime-ingest path
mints a phantom TOOL node; when it co-occurs (another test's assertion
footer lands in the same pytest run), diagnosis correctly routes RESIDUAL
and mints nothing. Mirrors the offline repro in the design doc, tests/depgraph/
test_diagnose_ingest_guard.py's real diagnose+ingest harness.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.depgraph.diagnose import Mode, RepoContext, diagnose, make_diagnostic_classifier
from python_deps.depgraph.ids import TEST_NODE_ID
from python_deps.depgraph.runtime_ingest import ingest_runtime_failures
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Layer, Node, NodeType,
)

_CMD = "python -m pytest -q"


def _base_graph() -> DepGraph:
    return DepGraph().with_node(
        Node(id=TEST_NODE_ID, type=NodeType.TEST, name="repo_tests_pass",
             layer=Layer.TESTS, discovered_by=DiscoveredBy.GOAL)
    )


def _minted_ids(new_graph: DepGraph) -> list[str]:
    return [n.id for n in new_graph.nodes if n.id != TEST_NODE_ID]


def test_filenotfound_less_without_assertionerror_mints_tool():
    # click's flaky pager test: the pager test ERRORS via FileNotFoundError,
    # and no OTHER test in this cycle's run reports an AssertionError -> the
    # co-occurrence guard has nothing to key on -> ENVIRONMENT(TOOL).
    out = (
        "ERROR tests/test_termui.py::test_pager_command - FileNotFoundError\n"
        '  File "click/_termui_impl.py", line 10, in pager\n'
        "FileNotFoundError: [Errno 2] No such file or directory: 'less'\n"
        "=== 1 error in 0.10s ==="
    )
    ctx = RepoContext()
    d = diagnose(_CMD, out, ctx)
    assert d.mode is Mode.ENVIRONMENT
    assert d.discovery is not None
    assert d.discovery.node_type is NodeType.TOOL
    assert d.discovery.name == "less"

    classifier = make_diagnostic_classifier(ctx)
    new_graph, found = ingest_runtime_failures(_base_graph(), [(_CMD, out)], classifiers=(classifier,))
    assert _minted_ids(new_graph) == ["tool:less"]
    assert len(found) == 1


def test_filenotfound_less_with_assertionerror_is_residual():
    # Same pager FileNotFoundError, but this cycle's run ALSO carries another
    # test's AssertionError footer in the same blob -> the guard fires ->
    # RESIDUAL, no mint.
    out = (
        "ERROR tests/test_termui.py::test_pager_command - FileNotFoundError\n"
        '  File "click/_termui_impl.py", line 10, in pager\n'
        "FileNotFoundError: [Errno 2] No such file or directory: 'less'\n"
        "FAILED tests/test_basic.py::test_echo - AssertionError\n"
        "tests/test_basic.py:42: AssertionError\n"
        "=== 1 failed, 1 error in 0.10s ==="
    )
    ctx = RepoContext()
    d = diagnose(_CMD, out, ctx)
    assert d.mode is Mode.RESIDUAL
    assert d.discovery is None

    classifier = make_diagnostic_classifier(ctx)
    new_graph, found = ingest_runtime_failures(_base_graph(), [(_CMD, out)], classifiers=(classifier,))
    assert _minted_ids(new_graph) == []
    assert found == []
