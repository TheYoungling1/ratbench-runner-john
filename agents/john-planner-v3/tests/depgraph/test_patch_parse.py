from python_deps.depgraph.patch import (
    parse_patch_proposal, PatchProposal, NodeSpec, ProviderSpec, EdgeSpec, ScriptPatch,
)

# The master spec §9 example (id values are illustrative; the gate validates canonicality).
_SPEC9 = {
    "rationale": {"failure": "meson cannot find libplacebo", "hypothesis": "missing -dev"},
    "patch": {
        "add_requirements": [{
            "id": "syslib:libplacebo.pc", "type": "SystemLib", "name": "libplacebo.pc",
            "layer": "system", "check_command": "pkg-config --exists libplacebo",
            "evidence_ref": "ev:block:meson_setup:stderr",
        }],
        "add_providers": [{
            "id": "apt:libplacebo-dev", "kind": "apt",
            "command": "apt-get install -y --no-install-recommends libplacebo-dev",
            "provides": ["syslib:libplacebo.pc"],
        }],
        "add_edges": [{
            "source": "test:repo_tests_pass", "relation": "requires", "target": "syslib:libplacebo.pc",
        }],
        "script_patches": [{
            "op": "add_block", "block_id": "system.libplacebo", "wave": "system",
            "command": "apt-get update && apt-get install -y --no-install-recommends libplacebo-dev",
            "target_node_ids": ["syslib:libplacebo.pc"], "checks": ["pkg-config --exists libplacebo"],
        }],
        "request_checks": ["syslib:libplacebo.pc"],
    },
}


def test_parses_spec9_example():
    p = parse_patch_proposal(_SPEC9)
    assert isinstance(p, PatchProposal) and not p.is_empty()
    assert p.add_requirements[0] == NodeSpec(
        id="syslib:libplacebo.pc", type="SystemLib", name="libplacebo.pc", layer="system",
        check_command="pkg-config --exists libplacebo", evidence_ref="ev:block:meson_setup:stderr")
    assert p.add_providers[0].kind == "apt" and p.add_providers[0].provides == ("syslib:libplacebo.pc",)
    assert p.add_edges[0] == EdgeSpec(source="test:repo_tests_pass", target="syslib:libplacebo.pc")
    # singular "command" is normalised to the commands tuple
    assert p.script_patches[0].commands == (
        "apt-get update && apt-get install -y --no-install-recommends libplacebo-dev",)
    assert p.request_checks == ("syslib:libplacebo.pc",)


def test_empty_and_defaults():
    p = parse_patch_proposal({})
    assert p.is_empty()
    assert p.add_requirements == () and p.add_edges == () and p.request_checks == ()


def test_unknown_keys_ignored_and_state_maps_to_promotion():
    p = parse_patch_proposal({"patch": {
        "bogus": 123,
        "add_requirements": [{"id": "config:DATABASE_URL", "type": "Config",
                              "name": "DATABASE_URL", "layer": "config", "state": "HINT"}],
    }})
    assert p.add_requirements[0].promotion == "HINT"   # raw value carried; gate normalises/validates


def test_parse_carries_service_fields():
    from python_deps.depgraph.patch import parse_patch_proposal
    p = parse_patch_proposal({"patch": {"add_requirements": [{
        "id": "service:postgres", "type": "Service", "layer": "services",
        "service_kind": "postgres", "service_params": {"db": "appdb"},
    }, {
        "id": "config:DATABASE_URL", "type": "Config", "layer": "config",
        "service_kind": "postgres", "service_params": {"var": "DATABASE_URL", "db": "appdb"},
    }]}})
    svc, cfg = p.add_requirements
    assert svc.service_kind == "postgres"
    assert svc.service_params == {"db": "appdb"}
    assert cfg.service_kind == "postgres"
    assert cfg.service_params == {"var": "DATABASE_URL", "db": "appdb"}


def test_parse_service_fields_default_absent():
    from python_deps.depgraph.patch import parse_patch_proposal
    p = parse_patch_proposal({"patch": {"add_requirements": [{
        "id": "pkg:x", "type": "Package", "layer": "pip"}]}})
    n = p.add_requirements[0]
    assert n.service_kind is None and n.service_params is None
