from python_deps.depgraph.patch import PatchProposal, ProviderSpec
from python_deps.depgraph.patch_gate import admit_proposal

def test_admit_rejects_with_errors(_graph_with_missing_syslib):
    bad = PatchProposal(add_providers=(ProviderSpec(
        id="apt:x", kind="apt", command="echo not-an-install", provides=("syslib:libpq",)),))
    res = admit_proposal(_graph_with_missing_syslib, bad, known_evidence_ids=frozenset())
    assert res.accepted is False and res.errors

def test_admit_accepts_and_recomposes(_graph_with_missing_syslib):
    good = PatchProposal(add_providers=(ProviderSpec(
        id="apt:libpq-dev", kind="apt", command="apt-get install -y libpq-dev",
        provides=("syslib:libpq",), override=True),))
    res = admit_proposal(_graph_with_missing_syslib, good, known_evidence_ids=frozenset())
    assert res.accepted is True
    assert any("libpq" in c for b in res.blocks for c in b.commands)

def test_admit_empty_proposal_accepts_noop(_graph_with_missing_syslib):
    res = admit_proposal(_graph_with_missing_syslib, PatchProposal(), known_evidence_ids=frozenset())
    assert res.accepted is True
