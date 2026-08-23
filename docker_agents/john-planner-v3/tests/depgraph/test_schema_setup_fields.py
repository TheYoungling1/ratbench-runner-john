from python_deps.depgraph.schema import (
    DiscoveredBy, Layer, Node, NodeType, Phase, Strength,
)


def _pkg(**kw):
    base = dict(id="pkg:requests", type=NodeType.PACKAGE, name="requests",
                layer=Layer.PIP, discovered_by=DiscoveredBy.RESOLVER)
    base.update(kw)
    return Node(**base)


def test_new_fields_default_inert():
    n = _pkg()
    assert n.setup_commands == ()
    assert n.strength is Strength.SOFT
    assert n.phase is Phase.SETUP


def test_to_dict_includes_new_fields():
    n = _pkg(
        setup_commands=("python3 -m pip install --break-system-packages --no-deps requests==2.0",),
        strength=Strength.HARD,
        phase=Phase.RUNTIME,
    )
    d = n.to_dict()
    assert d["setup_commands"] == [
        "python3 -m pip install --break-system-packages --no-deps requests==2.0"
    ]
    assert d["strength"] == "hard"
    assert d["phase"] == "runtime"


def test_enum_values():
    assert {s.value for s in Strength} == {"soft", "hard"}
    assert {p.value for p in Phase} == {"setup", "runtime", "test", "gate"}
