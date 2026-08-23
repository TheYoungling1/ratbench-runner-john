"""Seed-adapter input: WorldModelMap carries the built DepGraph object.

Task 1 is plumbing only — the field is stored, defaults to None, and survives
merge_map so a later task can seed contract nodes from it.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Put <repo>/src on the path so the canonical ``python_deps.depgraph.*`` import
# resolves (mirrors tests/depgraph/conftest.py; this test lives one level up).
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from src.envstate.world_model import initial_map, merge_map  # noqa: E402
from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph,
    DiscoveredBy,
    Layer,
    Node as DNode,
    NodeType,
)


def _base_kwargs() -> dict:
    return dict(
        base_image="python:3.11",
        workdir="/app",
        language="python 3.11",
        build_system="pip",
        repo_layout=("pyproject.toml",),
    )


def _tiny_depgraph() -> DepGraph:
    n = DNode(id="import:cv2", type=NodeType.IMPORT, name="cv2",
              layer=Layer.NAMING, discovered_by=DiscoveredBy.STATIC_SCAN)
    return DepGraph(nodes=(n,))


def test_initial_map_stores_dep_graph():
    g = _tiny_depgraph()
    m = initial_map(**_base_kwargs(), dep_graph=g)
    assert m.dep_graph is g


def test_initial_map_defaults_dep_graph_none():
    m = initial_map(**_base_kwargs())
    assert m.dep_graph is None


def test_merge_map_preserves_dep_graph():
    g = _tiny_depgraph()
    m = initial_map(**_base_kwargs(), dep_graph=g)
    m2 = merge_map(m, done_flag=True)
    assert m2.dep_graph is g
