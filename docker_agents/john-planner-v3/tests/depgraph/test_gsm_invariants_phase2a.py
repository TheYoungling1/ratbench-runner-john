"""Design §16 invariants assertable in Phase 2a (PatchGate)."""
from python_deps.depgraph.patch import PatchProposal, NodeSpec, ScriptPatch
from python_deps.depgraph.patch_gate import apply_proposal, validate_proposal
from python_deps.depgraph.schema import (
    DepGraph, Node, NodeType, Layer, State, DiscoveredBy,
)

_EV = frozenset({"ev1"})


def test_invariant3_4_apply_never_yields_satisfied():
    p = PatchProposal(add_requirements=(NodeSpec(id="syslib:libpq.so", type="SystemLib",
        name="libpq.so", layer="system", check_command="ldconfig -p | grep -q libpq",
        evidence_ref="ev1", promotion="candidate"),))
    res = apply_proposal(DepGraph(), p)
    assert res.graph.get("syslib:libpq.so").state is State.MISSING


def test_invariant6_model_cannot_carry_state():
    # NodeSpec has no `state` field; a SATISFIED attempt can only arrive as a promotion tag,
    # which validate rejects.
    assert not hasattr(NodeSpec("x:y", "Tool", "y", "toolchain"), "state")
    p = PatchProposal(add_requirements=(NodeSpec(id="tool:foo", type="Tool", name="foo",
        layer="toolchain", check_command="foo --version", evidence_ref="ev1",
        promotion="SATISFIED"),))
    assert any("promotion" in e.lower() for e in validate_proposal(DepGraph(), p, known_evidence_ids=_EV))


def test_invariant8_every_accepted_block_targets_existing_node():
    g = DepGraph().with_node(Node(id="syslib:libpq.so", type=NodeType.SYSTEM_LIB, name="libpq.so",
        layer=Layer.SYSTEM, discovered_by=DiscoveredBy.PROBE, state=State.MISSING))
    sp = ScriptPatch(block_id="system.x", wave="system",
        commands=("apt-get install -y libpq-dev",), target_node_ids=("syslib:libpq.so",),
        evidence_ref="ev1")
    res = apply_proposal(g, PatchProposal(script_patches=(sp,)))
    for b in res.blocks:
        for nid in b.target_node_ids:
            assert res.graph.get(nid) is not None


def test_validate_is_pure():
    g = DepGraph()
    before = (g.nodes, g.edges)
    validate_proposal(g, PatchProposal(), known_evidence_ids=frozenset())
    assert (g.nodes, g.edges) == before
