import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for p in (str(_ROOT), str(_ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from python_deps.depgraph.schema import (
    DepGraph, DiscoveredBy, Layer, Node, NodeType, State,
)
from src.envstate.install_localizer import (
    LocalizedFailure, localize_install_failure, certify_reciped_only,
    assemble_install_debug_bundle,
)

_SCRIPT = """#!/usr/bin/env bash
set -Eeuo pipefail
# ==================== SYSTEM ====================
apt-get update
#@node syslib:libgl1  provider=apt:libgl1  requires=-
#@check dpkg -s libgl1
apt-get install -y --no-install-recommends libgl1
# ==================== PIP ====================
#@node pkg:numpy==2.4.6  version=2.4.6  requires=-
#@check python -m pip show numpy
python3 -m pip install --break-system-packages --no-deps numpy==2.4.6
"""


def test_localize_maps_failing_command_to_node():
    loc = localize_install_failure(_SCRIPT, "apt-get install -y --no-install-recommends libgl1")
    assert loc.node_id == "syslib:libgl1"
    assert any("apt-get install -y --no-install-recommends libgl1" in l for l in loc.block_lines)


def test_localize_maps_pip_line_to_pip_node():
    loc = localize_install_failure(_SCRIPT, "python3 -m pip install --break-system-packages --no-deps numpy==2.4.6")
    assert loc.node_id == "pkg:numpy==2.4.6"


def test_localize_none_command_returns_no_node():
    loc = localize_install_failure(_SCRIPT, None)
    assert loc.node_id is None


def _syslib(state: State) -> Node:
    return Node(id="syslib:libgl1", type=NodeType.SYSTEM_LIB, name="libgl1",
                layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER, state=state,
                check_command="dpkg -s libgl1", chosen_fix="apt:libgl1")


def test_certify_reciped_only_flags_unsatisfied_reciped(monkeypatch):
    g = DepGraph().with_node(_syslib(State.MISSING))
    # Inject a fake certify_refresh that leaves the node MISSING (install "succeeded" but check fails).
    import src.envstate.install_localizer as mod
    monkeypatch.setattr(mod, "certify_refresh", lambda graph, ro, cycle: graph)
    out_graph, unsat = certify_reciped_only(g, lambda cmd: (1, ""), cycle=1)
    assert "syslib:libgl1" in unsat


def test_certify_reciped_only_clean_when_satisfied(monkeypatch):
    g = DepGraph().with_node(_syslib(State.SATISFIED))
    import src.envstate.install_localizer as mod
    monkeypatch.setattr(mod, "certify_refresh", lambda graph, ro, cycle: graph)
    out_graph, unsat = certify_reciped_only(g, lambda cmd: (0, "ok"), cycle=1)
    assert unsat == ()


def test_assemble_bundle_contains_all_three_parts():
    loc = LocalizedFailure(node_id="syslib:libgl1", block_lines=("#@node syslib:libgl1", "apt-get install -y libgl1"))
    bundle = assemble_install_debug_bundle(loc, "E: Unable to locate package",
                                           "RepairScope: providers=[apt:libgl1]", ("ctx line",))
    assert "syslib:libgl1" in bundle
    assert "Unable to locate package" in bundle
    assert "RepairScope" in bundle
