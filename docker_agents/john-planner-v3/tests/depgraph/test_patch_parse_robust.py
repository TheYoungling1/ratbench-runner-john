import pytest
from python_deps.depgraph.patch import parse_patch_proposal, PatchParseError

def test_missing_required_key_raises_structured():
    with pytest.raises(PatchParseError) as ei:
        parse_patch_proposal({"patch": {"add_requirements": [{"type": "SystemLib", "layer": "system"}]}})
    assert any("id" in e for e in ei.value.errors)

def test_provider_missing_command_raises_structured():
    with pytest.raises(PatchParseError):
        parse_patch_proposal({"patch": {"add_providers": [{"id": "apt:x", "kind": "apt"}]}})

def test_override_round_trips():
    p = parse_patch_proposal({"patch": {"add_providers": [
        {"id": "apt:libpq-dev", "kind": "apt", "command": "apt-get install -y libpq-dev",
         "provides": ["syslib:libpq"], "override": True}]}})
    assert p.add_providers[0].override is True

def test_well_formed_without_override_defaults_false():
    p = parse_patch_proposal({"patch": {"add_providers": [
        {"id": "apt:x", "kind": "apt", "command": "apt-get install -y x"}]}})
    assert p.add_providers[0].override is False
