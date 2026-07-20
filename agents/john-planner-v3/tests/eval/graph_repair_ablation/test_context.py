import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from python_deps.depgraph.schema import DepGraph, Node, Edge, NodeType, Layer, DiscoveredBy  # noqa: E402
from src.eval.graph_repair_ablation.context import flat_list_context, graph_context  # noqa: E402

def _graph():
    proj = Node("project:r", NodeType.PROJECT, "r", Layer.PIP, DiscoveredBy.GOAL)
    pkg = Node("pkg:requests==2.0", NodeType.PACKAGE, "requests", Layer.PIP,
               DiscoveredBy.RESOLVER, version="2.0")
    tool = Node("tool:libgraphviz-dev", NodeType.TOOL, "libgraphviz-dev", Layer.TOOLCHAIN,
                DiscoveredBy.RESOLVER, chosen_fix="apt:libgraphviz-dev")
    g = DepGraph((proj, pkg, tool))
    g = g.with_edge(Edge("project:r", "pkg:requests==2.0"))
    g = g.with_edge(Edge("project:r", "tool:libgraphviz-dev"))
    return g

def test_flat_list_has_names_but_no_structure():
    s = flat_list_context(_graph())
    assert "requests" in s and "2.0" in s
    for structural in ("tier", "requires", "TOOL", "chosen_fix", "apt:"):
        assert structural not in s

def test_graph_context_exposes_tier_fix_and_edges():
    s = graph_context(_graph())
    assert "apt:libgraphviz-dev" in s          # the fix hint
    assert "tier" in s.lower()                  # tier structure
    assert "requires" in s.lower()              # an edge relation
    assert "libgraphviz-dev" in s and "requests" in s
