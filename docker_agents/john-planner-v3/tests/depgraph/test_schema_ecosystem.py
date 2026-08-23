from python_deps.depgraph.schema import DiscoveredBy, Layer, Node, NodeType


def _node(**kw):
    base = dict(
        id="pkg:demo==1.0",
        type=NodeType.PACKAGE,
        name="demo",
        layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER,
    )
    base.update(kw)
    return Node(**base)


def test_ecosystem_defaults_to_python():
    assert _node().ecosystem == "python"


def test_to_dict_omits_ecosystem_for_python_nodes():
    assert "ecosystem" not in _node().to_dict()


def test_to_dict_emits_ecosystem_for_non_python_nodes():
    assert _node(ecosystem="rust").to_dict()["ecosystem"] == "rust"


def test_to_dict_key_set_is_byte_identical_for_python_nodes():
    expected = {
        "id", "type", "name", "layer", "tier", "discovered_by", "state",
        "version", "check_command", "evidence", "fix_candidates", "chosen_fix",
        "attempts", "provenance", "discovered_cycle", "certified_cycle",
        "build_from_source", "artifact", "hash", "resolved_python",
        "resolved_platform", "exclude_newer", "setup_commands", "strength",
        "phase", "data",
    }
    assert set(_node().to_dict()) == expected
