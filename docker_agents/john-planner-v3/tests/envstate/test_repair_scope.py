import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

import src.envstate.repair_scope as rs
from python_deps.depgraph.block import Block
from python_deps.depgraph.evidence_log import Evidence, EvidenceBundle

def _bundle():
    return EvidenceBundle().with_item(Evidence(
        evidence_id="ev.1.0", container_kind="canonical",
        command="apt-get install -y libplacebodev", rc=1,
        output_excerpt="Unable to locate package libplacebodev", cycle=1,
        block_id="system.libplacebo", node_id="syslib:libplacebo"))

def test_build_scope_carries_failure_and_evidence():
    fb = Block(block_id="system.libplacebo", wave="system",
               commands=("apt-get install -y libplacebodev",),
               target_node_ids=("syslib:libplacebo",))
    scope = rs.build_repair_scope(
        object(), target_node_id="syslib:libplacebo", failed_block=fb, bundle=_bundle(),
        known_invalid=("apt:libplacebodev",), constraints={"package_manager": "apt"})
    assert scope.failed_command == "apt-get install -y libplacebodev"
    assert "libplacebodev" in scope.failed_output
    assert "ev.1.0" in scope.known_evidence_ids
    assert scope.constraints == (("package_manager", "apt"),)
    assert scope.slice_lines == ()   # graph context stripped from the repair scope

def test_build_scope_tolerates_none_bundle():
    # Binding-install repair passes bundle=None (no obligation packet — failure evidence is the
    # install stderr, surfaced separately). build_repair_scope must not crash on bundle.items.
    scope = rs.build_repair_scope(
        object(), target_node_id=None, failed_block=None, bundle=None)
    assert scope.known_evidence_ids == frozenset()
    assert scope.failed_command is None


def test_render_surfaces_avoidlist_and_schema():
    scope = rs.build_repair_scope(
        object(), target_node_id="syslib:libplacebo", failed_block=None, bundle=_bundle(),
        known_invalid=("apt:libplacebodev",), constraints=None)
    text = rs.render_repair_scope(scope)
    assert "apt:libplacebodev" in text          # avoid-list
    assert "Graph context:" not in text          # graph context is stripped from the repair prompt
    assert "Failure output:" in text             # the raw build-script failure IS shown
    assert "add_providers" in text               # schema hint
