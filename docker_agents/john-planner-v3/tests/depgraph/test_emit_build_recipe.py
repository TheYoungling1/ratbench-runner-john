# tests/depgraph/test_emit_build_recipe.py
from python_deps.depgraph.emit import build_recipe, EmitStep
from python_deps.depgraph.schema import (
    DepGraph, Layer, Node, NodeType, State, DiscoveredBy,
)


def _pkg(name, version="1.0"):
    return Node(id=f"pkg:{name}", type=NodeType.PACKAGE, name=name, layer=Layer.PIP,
                discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING, version=version)


def _tool(name, apt):
    return Node(id=f"tool:{name}", type=NodeType.TOOL, name=name, layer=Layer.TOOLCHAIN,
                discovered_by=DiscoveredBy.PROBE, state=State.MISSING,
                fix_candidates=(f"apt:{apt}",), chosen_fix=f"apt:{apt}")


def test_build_recipe_apt_then_pip_pinned():
    g = DepGraph()
    ordered = (_tool("gcc", "build-essential"), _pkg("numpy", "1.26.4"), _pkg("lxml", "5.1.0"))
    steps = build_recipe(g, ordered)
    assert [s.kind for s in steps] == ["system_install", "python_install"]
    assert steps[0].command == "apt-get update && apt-get install -y build-essential"
    assert steps[1].command == (
        "python3 -m pip install --break-system-packages numpy==1.26.4 lxml==5.1.0"
    )
    assert steps[1].target_node_ids == ("pkg:numpy", "pkg:lxml")


def test_build_recipe_dedupes_apt_names():
    g = DepGraph()
    ordered = (_tool("gcc", "build-essential"), _tool("g++", "build-essential"))
    steps = build_recipe(g, ordered)
    assert steps[0].command == "apt-get update && apt-get install -y build-essential"


def test_build_recipe_empty_when_nothing_emittable():
    assert build_recipe(DepGraph(), ()) == ()
