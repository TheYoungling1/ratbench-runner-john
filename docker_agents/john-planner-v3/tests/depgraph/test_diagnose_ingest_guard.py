"""The diagnostic classifier drops repo-local imports at ingest (pure, no Docker)."""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.depgraph.diagnose import RepoContext, make_diagnostic_classifier
from python_deps.depgraph.ids import TEST_NODE_ID, package_id
from python_deps.depgraph.runtime_ingest import ingest_runtime_failures
from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Layer, Node, NodeType,
)


def _base_graph() -> DepGraph:
    return DepGraph().with_node(
        Node(id=TEST_NODE_ID, type=NodeType.TEST, name="repo_tests_pass",
             layer=Layer.TESTS, discovered_by=DiscoveredBy.GOAL)
    )


def test_local_import_is_not_ingested_as_package():
    ctx = RepoContext(local_names=frozenset({"docs_src"}))
    classifier = make_diagnostic_classifier(ctx)
    obs = [("python -m docs_src.build",
            "ModuleNotFoundError: No module named 'docs_src'")]
    new_graph, found = ingest_runtime_failures(_base_graph(), obs, classifiers=(classifier,))
    assert new_graph.get(package_id("docs_src", None)) is None
    assert found == []


def test_external_import_is_still_ingested():
    ctx = RepoContext(local_names=frozenset({"docs_src"}))
    classifier = make_diagnostic_classifier(ctx)
    obs = [("python app.py", "ModuleNotFoundError: No module named 'yaml'")]
    new_graph, found = ingest_runtime_failures(_base_graph(), obs, classifiers=(classifier,))
    assert new_graph.get(package_id("PyYAML", None)) is not None
    assert len(found) == 1
