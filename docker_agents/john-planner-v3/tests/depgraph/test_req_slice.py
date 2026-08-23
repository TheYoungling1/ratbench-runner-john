# tests/depgraph/test_req_slice.py
import sys
from pathlib import Path
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.depgraph.schema import Node, NodeType, Layer, State, DiscoveredBy, Attempt, DepGraph, Edge, EdgeType
from python_deps.depgraph.req_slice import providers_view, ProviderView, ProviderCand, TriedProvider, build_requirement_slice, RequirementSlice, DepView, render_requirement_slice


def _syslib(**kw):
    base = dict(id="syslib:libxml2", type=NodeType.SYSTEM_LIB, name="libxml2",
                layer=Layer.SYSTEM, discovered_by=DiscoveredBy.PROBE, state=State.MISSING,
                check_command="pkg-config --exists libxml-2.0",
                fix_candidates=("apt:libxml2-dev",), chosen_fix="apt:libxml2-dev")
    base.update(kw)
    return Node(**base)


def test_candidates_include_chosen_and_action_class():
    pv = providers_view(_syslib())
    ids = [c.id for c in pv.candidates]
    assert "apt:libxml2-dev" in ids
    assert pv.chosen == "apt:libxml2-dev"
    assert next(c.action_class for c in pv.candidates if c.id == "apt:libxml2-dev") == "apt"


def test_chosen_added_to_candidates_when_absent():
    pv = providers_view(_syslib(fix_candidates=()))   # chosen set but not in candidates
    assert "apt:libxml2-dev" in [c.id for c in pv.candidates]


def test_tried_failed_derived_from_failed_attempts_with_reverse_parse():
    node = _syslib(attempts=(
        Attempt(command="apt-get install -y libxml2dev", outcome="failed", check="", cycle=1),
        Attempt(command="apt-get install -y libxml2-dev", outcome="succeeded", check="", cycle=2),
    ))
    pv = providers_view(node)
    assert len(pv.tried_failed) == 1                       # only the failed one
    t = pv.tried_failed[0]
    assert t.command == "apt-get install -y libxml2dev"
    assert t.provider_id == "apt:libxml2dev"                # single-token reverse-parse


def test_batch_command_has_no_provider_id():
    node = _syslib(attempts=(Attempt(command="apt-get install -y a b c", outcome="failed"),))
    assert providers_view(node).tried_failed[0].provider_id is None   # batch -> not attributable


def test_pip_provider_action_class_and_reverse_parse():
    node = Node(id="pkg:lxml==5.0", type=NodeType.PACKAGE, name="lxml", layer=Layer.PIP,
                discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING, version="5.0",
                check_command="python -m pip show lxml", fix_candidates=("pip:lxml",),
                chosen_fix="pip:lxml",
                attempts=(Attempt(command="pip install lxml==5.0", outcome="failed"),))
    pv = providers_view(node)
    assert next(c.action_class for c in pv.candidates if c.id == "pip:lxml") == "pip"
    assert pv.tried_failed[0].provider_id == "pip:lxml"


def test_shell_provider_action_class_from_taxonomy():
    # "shell" is an explicit, audited member of the canonical taxonomy (action_class.ACTION_CLASSES).
    pv = providers_view(_syslib(fix_candidates=("shell:make",), chosen_fix="shell:make"))
    assert next(c.action_class for c in pv.candidates if c.id == "shell:make") == "shell"


def _graph_with_frontier():
    g = DepGraph()
    g = g.with_node(Node(id="test:repo_tests_pass", type=NodeType.TEST, name="repo_tests_pass",
        layer=Layer.TESTS, discovered_by=DiscoveredBy.GOAL, state=State.MISSING,
        check_command="python -m pytest -q"))
    g = g.with_node(Node(id="pkg:lxml==5.0", type=NodeType.PACKAGE, name="lxml", layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING, version="5.0",
        check_command="python -m pip show lxml"))
    g = g.with_node(Node(id="syslib:libxml2", type=NodeType.SYSTEM_LIB, name="libxml2",
        layer=Layer.SYSTEM, discovered_by=DiscoveredBy.PROBE, state=State.MISSING,
        check_command="pkg-config --exists libxml-2.0", chosen_fix="apt:libxml2-dev",
        fix_candidates=("apt:libxml2-dev",), evidence='Dependency "libxml2" not found, tried pkgconfig',
        attempts=(Attempt(command="apt-get install -y libxml2dev", outcome="failed"),)))
    g = g.with_node(Node(id="tool:pkg-config", type=NodeType.TOOL, name="pkg-config",
        layer=Layer.SYSTEM, discovered_by=DiscoveredBy.PROBE, state=State.SATISFIED,
        check_command="command -v pkg-config", chosen_fix="apt:pkg-config"))
    # Package -> SystemLib is EDGE_RULES-legal (see test_obligation_framing.py:44). pkg requires the syslib.
    g = g.with_edge(Edge(src="pkg:lxml==5.0", dst="syslib:libxml2", relation=EdgeType.REQUIRES))
    return g


def test_build_slice_deps_unblocks_cohort_providers_gate():
    g = _graph_with_frontier()
    s = build_requirement_slice(g, g.get("syslib:libxml2"))
    assert isinstance(s, RequirementSlice)
    assert s.node_id == "syslib:libxml2" and s.kind == "SystemLib" and s.state == "missing"
    assert s.check == "pkg-config --exists libxml-2.0"
    # unblocks = reverse REQUIRES (recovers the dropped `blocks`): pkg:lxml needs the syslib
    assert "pkg:lxml==5.0" in s.unblocks
    # layer cohort (SYSTEM): the satisfied pkg-config tool, syslib itself excluded
    assert "tool:pkg-config" in s.layer_cohort_satisfied
    assert s.node_id not in s.layer_cohort_satisfied and s.node_id not in s.layer_cohort_missing
    # active gate synthesized from the TEST node's own check (no envstate import)
    assert s.active_gate == "python -m pytest -q"
    # providers + tried-failed carried through
    assert s.providers.chosen == "apt:libxml2-dev"
    assert s.providers.tried_failed and s.providers.tried_failed[0].provider_id == "apt:libxml2dev"
    # evidence reduced to the best line (text, not an id)
    assert "libxml2" in s.evidence


def test_active_gate_empty_when_no_test_node():
    g = DepGraph().with_node(Node(id="syslib:x", type=NodeType.SYSTEM_LIB, name="x",
        layer=Layer.SYSTEM, discovered_by=DiscoveredBy.PROBE, state=State.MISSING,
        check_command="pkg-config --exists x"))
    s = build_requirement_slice(g, g.get("syslib:x"))
    assert s.active_gate == ""          # no TEST node -> empty, never crashes


def test_render_contains_target_avoidance_and_no_crash():
    g = _graph_with_frontier()
    lines = render_requirement_slice(build_requirement_slice(g, g.get("syslib:libxml2")))
    blob = "\n".join(lines)
    assert any(l.startswith("target:") for l in lines)
    assert "apt:libxml2-dev" in blob                         # candidate/chosen surfaced
    assert "tried & FAILED" in blob and "avoid apt:libxml2dev" in blob  # the anti-ReAct signal
    assert "active gate: python -m pytest -q" in blob
    assert "unblocks: pkg:lxml==5.0" in blob                  # reverse-REQUIRES surfaced to the agent
    assert all(isinstance(l, str) for l in lines)


def test_render_empty_slice_does_not_crash():
    g = DepGraph().with_node(Node(id="syslib:x", type=NodeType.SYSTEM_LIB, name="x",
        layer=Layer.SYSTEM, discovered_by=DiscoveredBy.PROBE, state=State.MISSING))
    lines = render_requirement_slice(build_requirement_slice(g, g.get("syslib:x")))
    assert lines and lines[0].startswith("target:")          # minimal but valid


def _pip_node_with_cmd(cmd: str) -> Node:
    """Helper: a PACKAGE node whose single failed attempt uses `cmd`."""
    return Node(
        id="pkg:lxml==5.0", type=NodeType.PACKAGE, name="lxml", layer=Layer.PIP,
        discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING, version="5.0",
        check_command="python -m pip show lxml", fix_candidates=("pip:lxml",),
        chosen_fix="pip:lxml",
        attempts=(Attempt(command=cmd, outcome="failed"),),
    )


def test_requirements_file_install_yields_no_provider_id():
    # `pip install -r requirements.txt` is NOT a single named package; provider_id must be None.
    node = _pip_node_with_cmd("pip install -r requirements.txt")
    pv = providers_view(node)
    assert pv.tried_failed[0].provider_id is None


def test_editable_install_yields_no_provider_id():
    # `pip install -e .` is an editable install, not a single named package; must be None.
    node = _pip_node_with_cmd("pip install -e .")
    pv = providers_view(node)
    assert pv.tried_failed[0].provider_id is None
