import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.depgraph.patch import NodeSpec, PatchProposal, ScriptPatch
from python_deps.depgraph.patch_gate import validate_proposal
from python_deps.depgraph.schema import DepGraph


def test_proposal_with_trivial_check_is_rejected():
    proposal = PatchProposal(add_requirements=(NodeSpec(
        id="syslib:libgl1", type="SystemLib", name="libgl1", layer="system",
        check_command="true", evidence_ref="ev-1"),))
    errs = validate_proposal(DepGraph(), proposal, known_evidence_ids=frozenset({"ev-1"}))
    assert any("check" in e.lower() and "libgl1" in e for e in errs)


def test_script_patch_with_trivial_check_is_rejected():
    # The anti-weakening guard must also cover ScriptPatch.checks, not only NodeSpec.check_command.
    proposal = PatchProposal(
        add_requirements=(NodeSpec(
            id="syslib:libgl1", type="SystemLib", name="libgl1", layer="system",
            check_command="dpkg -s libgl1", evidence_ref="ev-1"),),
        script_patches=(ScriptPatch(
            block_id="system.libgl1", wave="system",
            commands=("apt-get install -y libgl1",), target_node_ids=("syslib:libgl1",),
            checks=("true",), evidence_ref="ev-1"),))
    errs = validate_proposal(DepGraph(), proposal, known_evidence_ids=frozenset({"ev-1"}))
    assert any("system.libgl1" in e and "detect absence" in e for e in errs)
