from src.envstate.contracts.nodes import Node, Edge
from src.envstate.contracts.graph import (
    ContractGraph, project_status, depends_on_closure, frontier_by_layer, goal_ready,
)

def _g():
    nodes = (
        Node("contract:goal:repo_tests_pass", "Contract",
             {"level": "goal", "required": True, "layer": "tests", "kind": "tests_pass"}),
        Node("contract:goal:repo_imports_work", "Contract",
             {"level": "goal", "required": True, "layer": "deps", "kind": "imports_work"}),
        Node("contract:python_import:cv2", "Contract",
             {"level": "atomic", "layer": "deps", "kind": "python_import", "subject": "cv2"}),
        Node("blocker:importerror-libgl", "Blocker",
             {"signature": "ImportError: libGL.so.1", "kind": "missing_system_library",
              "layer": "system", "active": True, "summary": "libGL missing"}),
    )
    edges = (
        Edge("contract:goal:repo_tests_pass", "depends_on", "contract:goal:repo_imports_work"),
        Edge("contract:goal:repo_imports_work", "depends_on", "contract:python_import:cv2"),
        Edge("blocker:importerror-libgl", "violates", "contract:python_import:cv2"),
    )
    return ContractGraph(nodes=nodes, edges=edges)

def test_project_status_violated_from_active_blocker():
    g = _g()
    assert project_status(g, "contract:python_import:cv2", frozenset()) == "violated"

def test_project_status_satisfied_from_host_set():
    g = _g()
    assert project_status(g, "contract:python_import:cv2",
                          frozenset({"contract:python_import:cv2"})) == "satisfied"

def test_project_status_unknown_when_no_evidence():
    g = _g()
    assert project_status(g, "contract:goal:repo_imports_work", frozenset()) == "unknown"

def test_depends_on_closure_reaches_atomic():
    g = _g()
    cl = depends_on_closure(g, "contract:goal:repo_tests_pass")
    assert "contract:python_import:cv2" in cl

def test_frontier_by_layer_groups_unsatisfied():
    g = _g()
    fr = frontier_by_layer(g, frozenset())
    assert "contract:python_import:cv2" in fr["deps"]

def test_goal_ready_false_until_all_satisfied():
    g = _g()
    assert goal_ready(g, frozenset()) is False
    everything = frozenset(n.id for n in g.nodes if n.type == "Contract")
    assert goal_ready(g, everything) is True

def test_diagnostic_notes_roundtrip_capped():
    g = ContractGraph(diagnostic_notes=tuple(str(i) for i in range(15)))
    assert ContractGraph.from_dict(g.to_dict()).diagnostic_notes == g.diagnostic_notes


# --- Next-Target frontier + attempt history (advisory planning ops) ---
from src.envstate.contracts.graph import find_next_target_contracts, attempts_for_contract


def _chain():
    """repo_tests_pass -> cv2 -> libGL; blocker violates libGL (the root)."""
    nodes = (
        Node("contract:goal:repo_tests_pass", "Contract",
             {"level": "goal", "required": True, "layer": "tests",
              "kind": "tests_pass", "check": "python -m pytest -q"}),
        Node("contract:python_import:cv2", "Contract",
             {"level": "atomic", "layer": "deps", "kind": "python_import", "subject": "cv2"}),
        Node("contract:system_library:libgl", "Contract",
             {"level": "atomic", "layer": "system", "kind": "system_library", "subject": "libGL"}),
        Node("blocker:libgl", "Blocker",
             {"signature": "libGL.so.1: cannot open", "active": True,
              "root_or_downstream": "root", "summary": "libGL missing", "layer": "system"}),
    )
    edges = (
        Edge("contract:goal:repo_tests_pass", "depends_on", "contract:python_import:cv2"),
        Edge("contract:python_import:cv2", "depends_on", "contract:system_library:libgl"),
        Edge("blocker:libgl", "violates", "contract:system_library:libgl"),
    )
    return ContractGraph(nodes=nodes, edges=edges)


def test_next_target_returns_lowest_violated_root():
    g = _chain()
    # Only libGL is actionable (its prereqs are empty/satisfied); cv2 and the goal
    # are blocked behind it.
    assert find_next_target_contracts(g, "contract:goal:repo_tests_pass", frozenset()) == \
        ("contract:system_library:libgl",)


def test_next_target_advances_when_root_satisfied():
    g = _chain()
    hs = frozenset({"contract:system_library:libgl"})
    assert find_next_target_contracts(g, "contract:goal:repo_tests_pass", hs) == \
        ("contract:python_import:cv2",)


def test_next_target_reaches_goal_when_all_deps_satisfied():
    g = _chain()
    hs = frozenset({"contract:system_library:libgl", "contract:python_import:cv2"})
    # repo_tests_pass carries a real check command, so it is itself actionable.
    assert find_next_target_contracts(g, "contract:goal:repo_tests_pass", hs) == \
        ("contract:goal:repo_tests_pass",)


def test_next_target_orders_violated_before_unknown():
    nodes = (
        Node("contract:goal:repo_tests_pass", "Contract",
             {"level": "goal", "required": True, "layer": "tests", "kind": "tests_pass",
              "check": "python -m pytest -q"}),
        Node("contract:python_import:a", "Contract",
             {"level": "atomic", "layer": "deps", "kind": "python_import", "subject": "a"}),
        Node("contract:python_import:b", "Contract",
             {"level": "atomic", "layer": "deps", "kind": "python_import", "subject": "b"}),
        Node("blocker:b", "Blocker", {"active": True, "summary": "b broken"}),
    )
    edges = (
        Edge("contract:goal:repo_tests_pass", "depends_on", "contract:python_import:a"),
        Edge("contract:goal:repo_tests_pass", "depends_on", "contract:python_import:b"),
        Edge("blocker:b", "violates", "contract:python_import:b"),  # b is violated
    )
    g = ContractGraph(nodes=nodes, edges=edges)
    # b (violated) ranks before a (unknown); the goal is blocked behind both.
    assert find_next_target_contracts(g, "contract:goal:repo_tests_pass", frozenset()) == \
        ("contract:python_import:b", "contract:python_import:a")


def test_attempts_for_contract_tracks_addresses_edges():
    g = _chain()
    att = Node("attempt:install-cv2", "Attempt",
               {"kind": "python_install", "commands": ["pip install opencv-python"],
                "outcome": "ok_but_still_blocked"})
    g2 = ContractGraph(
        nodes=g.nodes + (att,),
        edges=g.edges + (Edge("attempt:install-cv2", "addresses", "contract:python_import:cv2"),),
    )
    got = attempts_for_contract(g2, "contract:python_import:cv2")
    assert tuple(a.id for a in got) == ("attempt:install-cv2",)
    assert got[0].data["outcome"] == "ok_but_still_blocked"
    # untried contract → empty
    assert attempts_for_contract(g2, "contract:system_library:libgl") == ()
