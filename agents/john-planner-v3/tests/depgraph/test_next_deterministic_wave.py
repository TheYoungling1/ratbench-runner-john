import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph, Node, NodeType, Layer, State, DiscoveredBy,
)
from python_deps.depgraph.emit import next_deterministic_wave  # noqa: E402


def _pkg(nid, name, version, state=State.MISSING):
    return Node(
        id=nid, type=NodeType.PACKAGE, name=name, layer=Layer.PIP,
        discovered_by=DiscoveredBy.STATIC_SCAN, state=state,
        check_command=f"python -c 'import {name}'", version=version,
    )


def test_empty_graph_yields_no_wave():
    assert next_deterministic_wave(DepGraph()) == ()


def test_emittable_packages_become_one_pip_step():
    g = (DepGraph()
         .with_node(_pkg("pkg:a", "a", "1.0"))
         .with_node(_pkg("pkg:b", "b", "2.0")))
    wave = next_deterministic_wave(g)
    assert len(wave) == 1
    assert wave[0].kind == "python_install"
    assert set(wave[0].target_node_ids) == {"pkg:a", "pkg:b"}


def test_satisfied_nodes_are_not_in_the_wave():
    g = DepGraph().with_node(_pkg("pkg:a", "a", "1.0", state=State.SATISFIED))
    assert next_deterministic_wave(g) == ()
