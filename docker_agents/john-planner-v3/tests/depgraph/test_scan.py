"""Task 2 — static import scan -> Import + Test nodes (scan.py)."""

from __future__ import annotations

from pathlib import Path

from python_deps.depgraph.ids import TEST_NODE_ID, import_id
from python_deps.depgraph.scan import scan_to_nodes
from python_deps.depgraph.schema import (
    DiscoveredBy,
    EdgeType,
    Layer,
    NodeType,
    State,
)


def _write_fixture_repo(root: Path) -> None:
    """A tiny repo: one external native import, one aliased external import,
    and one stdlib import that must be excluded."""
    (root / "vision.py").write_text(
        "import cv2\nimport os\n\n\ndef load():\n    return cv2\n",
        encoding="utf-8",
    )
    (root / "imaging.py").write_text(
        "from PIL import Image\n\n\ndef open_image(path):\n    return Image.open(path)\n",
        encoding="utf-8",
    )


def test_external_imports_become_import_nodes(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)

    graph = scan_to_nodes(str(tmp_path))

    cv2 = graph.get(import_id("cv2"))
    pil = graph.get(import_id("PIL"))
    assert cv2 is not None
    assert pil is not None

    for node in (cv2, pil):
        assert node.type is NodeType.IMPORT
        assert node.layer is Layer.NAMING
        assert node.discovered_by is DiscoveredBy.STATIC_SCAN
        assert node.state is State.UNKNOWN

    assert cv2.name == "cv2"
    assert cv2.check_command == 'python -c "import cv2"'
    assert pil.check_command == 'python -c "import PIL"'


def test_stdlib_import_excluded(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)

    graph = scan_to_nodes(str(tmp_path))

    assert graph.get(import_id("os")) is None


def test_provenance_records_source_file(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)

    graph = scan_to_nodes(str(tmp_path))

    cv2 = graph.get(import_id("cv2"))
    assert cv2 is not None
    assert cv2.provenance is not None
    assert "vision.py" in cv2.provenance


def test_test_node_present_with_requires_edges(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)

    graph = scan_to_nodes(str(tmp_path))

    test_node = graph.get(TEST_NODE_ID)
    assert test_node is not None
    assert test_node.type is NodeType.TEST
    assert test_node.layer is Layer.TESTS
    assert test_node.discovered_by is DiscoveredBy.GOAL
    assert test_node.check_command == "python -m pytest -q"

    required = {n.id for n in graph.requires_of(TEST_NODE_ID)}
    assert required == {import_id("cv2"), import_id("PIL")}

    for edge in graph.edges:
        assert edge.relation is EdgeType.REQUIRES
        assert edge.src == TEST_NODE_ID
        assert edge.origin == "scan"


def test_no_external_imports_yields_only_test_node(tmp_path: Path) -> None:
    (tmp_path / "plain.py").write_text("import os\nimport sys\n", encoding="utf-8")

    graph = scan_to_nodes(str(tmp_path))

    assert graph.get(TEST_NODE_ID) is not None
    assert all(n.type is NodeType.TEST for n in graph.nodes)
    assert graph.edges == ()


def test_scan_scopes_out_examples_and_docs(tmp_path: Path) -> None:
    """Imports seen ONLY under examples/docs/build are dropped; src/tests kept."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("import requests\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("import pytest\n", encoding="utf-8")
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "demo.py").write_text(
        "import blueprintapp\n", encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "conf.py").write_text("import sphinx_only\n", encoding="utf-8")

    graph = scan_to_nodes(str(tmp_path))
    names = {n.name for n in graph.nodes if n.type is NodeType.IMPORT}

    assert "requests" in names  # src kept
    assert "pytest" in names  # tests kept
    assert "blueprintapp" not in names  # examples dropped
    assert "sphinx_only" not in names  # docs dropped


def test_scan_scopes_out_tools_dir(tmp_path: Path) -> None:
    """Imports seen ONLY under a repo-root tools/ dev-tooling dir are dropped;
    package-source imports are kept (closes Finding B for vizro: `import github`
    lives in tools/pycafe/, CI/docs tooling outside every installable package)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("import requests\n", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "helper.py").write_text("import click\n", encoding="utf-8")

    graph = scan_to_nodes(str(tmp_path))
    names = {n.name for n in graph.nodes if n.type is NodeType.IMPORT}

    assert "requests" in names  # src kept
    assert "click" not in names  # tools dropped


def test_scan_drops_local_fixture_packages_and_typing(tmp_path: Path) -> None:
    """In-repo fixture packages (nested under tests/) and typing-only modules are
    not external PyPI deps and must not become Import nodes."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "import requests\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    import _typeshed\n",
        encoding="utf-8",
    )
    # a local fixture package nested under tests/ (flask's blueprintapp pattern)
    fixtures = tmp_path / "tests" / "test_apps" / "blueprintapp"
    fixtures.mkdir(parents=True)
    (fixtures / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_x.py").write_text(
        "import blueprintapp\nimport requests\n", encoding="utf-8"
    )

    graph = scan_to_nodes(str(tmp_path))
    names = {n.name for n in graph.nodes if n.type is NodeType.IMPORT}

    assert "requests" in names  # real external dep kept
    assert "blueprintapp" not in names  # local fixture package dropped
    assert "_typeshed" not in names  # typing-only dropped
