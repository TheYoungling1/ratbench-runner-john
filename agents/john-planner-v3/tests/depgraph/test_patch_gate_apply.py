# tests/depgraph/test_patch_gate_apply.py
from dataclasses import FrozenInstanceError

from python_deps.depgraph.patch import (
    PatchProposal, NodeSpec, ProviderSpec, EdgeSpec, ScriptPatch,
)
from python_deps.depgraph.patch_gate import apply_proposal, ApplyResult
from python_deps.depgraph.schema import (
    DepGraph, Node, NodeType, Layer, State, DiscoveredBy, EdgeType,
)


def _base():
    return DepGraph().with_node(Node(id="test:repo_tests_pass", type=NodeType.TEST,
        name="tests", layer=Layer.TESTS, discovered_by=DiscoveredBy.GOAL, state=State.MISSING))


def _proposal():
    return PatchProposal(
        add_requirements=(NodeSpec(id="syslib:libpq.so", type="SystemLib", name="libpq.so",
            layer="system", check_command="ldconfig -p | grep -q libpq", evidence_ref="ev1",
            promotion="candidate"),),
        add_providers=(ProviderSpec(id="apt:libpq-dev", kind="apt",
            command="apt-get install -y --no-install-recommends libpq-dev",
            provides=("syslib:libpq.so",)),),
        add_edges=(EdgeSpec(source="test:repo_tests_pass", target="syslib:libpq.so", hard=False),),
        script_patches=(ScriptPatch(block_id="system.libpq", wave="system",
            commands=("apt-get update && apt-get install -y libpq-dev",),
            target_node_ids=("syslib:libpq.so",), checks=("ldconfig -p | grep -q libpq",),
            evidence_ref="ev1"),),
    )


def test_apply_is_immutable():
    g = _base()
    before = (g.nodes, g.edges)
    apply_proposal(g, _proposal())
    assert (g.nodes, g.edges) == before          # input untouched


def test_node_added_missing_with_promotion_and_never_satisfied():
    res = apply_proposal(_base(), _proposal())
    node = res.graph.get("syslib:libpq.so")
    assert node is not None
    assert node.state is State.MISSING           # invariant #3/#4
    assert node.data.get("promotion") == "candidate"
    assert node.check_command == "ldconfig -p | grep -q libpq"


def test_provider_binds_chosen_fix():
    res = apply_proposal(_base(), _proposal())
    assert res.graph.get("syslib:libpq.so").chosen_fix == "apt:libpq-dev"


def test_soft_edge_carries_hard_false():
    res = apply_proposal(_base(), _proposal())
    edge = next(e for e in res.graph.edges if e.dst == "syslib:libpq.so")
    assert edge.relation is EdgeType.REQUIRES and edge.data.get("hard") is False


def test_script_patch_becomes_governed_block_not_state():
    res = apply_proposal(_base(), _proposal())
    assert len(res.blocks) == 1
    b = res.blocks[0]
    assert b.block_id == "system.libpq" and b.target_node_ids == ("syslib:libpq.so",)
    assert b.check_commands == ("ldconfig -p | grep -q libpq",)
    # the block never certified anything: target node is still MISSING
    assert res.graph.get("syslib:libpq.so").state is State.MISSING


def test_adversarial_apply_never_satisfied():
    # even a fully populated proposal yields no SATISFIED node
    res = apply_proposal(_base(), _proposal())
    assert all(n.state is not State.SATISFIED for n in res.graph.nodes if n.id != "test:repo_tests_pass")
    assert _base().get("test:repo_tests_pass").state is State.MISSING


def test_override_replaces_chosen_fix(_graph_with_missing_syslib):
    g = _graph_with_missing_syslib
    p = PatchProposal(add_providers=(ProviderSpec(
        id="apt:libpq-dev", kind="apt", command="apt-get install -y libpq-dev",
        provides=("syslib:libpq",), override=True),))
    assert apply_proposal(g, p).graph.get("syslib:libpq").chosen_fix == "apt:libpq-dev"

def test_no_override_keeps_first_writer(_graph_with_missing_syslib):
    g = _graph_with_missing_syslib
    p = PatchProposal(add_providers=(ProviderSpec(
        id="apt:other", kind="apt", command="apt-get install -y other",
        provides=("syslib:libpq",)),))
    assert apply_proposal(g, p).graph.get("syslib:libpq").chosen_fix == "apt:libpqdev"
