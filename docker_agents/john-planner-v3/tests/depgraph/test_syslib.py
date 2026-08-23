from python_deps.depgraph.schema import DiscoveredBy, Layer, NodeType, State
from python_deps.depgraph.syslib import make_syslib_node


def test_resolver_prior_shape_with_apt():
    node = make_syslib_node(
        "libGL.so.1",
        discovered_by=DiscoveredBy.RESOLVER,
        state=State.UNKNOWN,
        apt="libgl1",
        provenance="wheel:opencv-python",
    )
    assert node.id == "syslib:libGL.so.1"
    assert node.type is NodeType.SYSTEM_LIB
    assert node.layer is Layer.SYSTEM
    assert node.discovered_by is DiscoveredBy.RESOLVER
    assert node.state is State.UNKNOWN
    assert node.check_command == "ldconfig -p | grep libGL.so.1"
    assert node.fix_candidates == ("apt:libgl1",)
    assert node.chosen_fix == "apt:libgl1"
    assert node.provenance == "wheel:opencv-python"
    assert node.attempts == ()


def test_missing_apt_leaves_fix_empty():
    node = make_syslib_node(
        "libfoo.so.9",
        discovered_by=DiscoveredBy.PROBE,
        state=State.MISSING,
    )
    assert node.fix_candidates == ()
    assert node.chosen_fix is None
    assert node.state is State.MISSING
    assert node.discovered_by is DiscoveredBy.PROBE
