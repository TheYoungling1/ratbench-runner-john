import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph, Node, NodeType, Layer, State, DiscoveredBy,
)
from src.envstate.graph_scheduler import next_decision, packet_to_task  # noqa: E402
from src.envstate.orchestrator import VERIFY_TEST_CMD  # noqa: E402
from python_deps.depgraph.schedule import frame_obligation  # noqa: E402


def _missing(state=State.MISSING):
    return DepGraph().with_node(Node(
        id="pkg:requests", type=NodeType.PACKAGE, name="requests", layer=Layer.PIP,
        discovered_by=DiscoveredBy.STATIC_SCAN, state=state,
        check_command="python -c 'import requests'",
    ))


def test_actionable_frontier_yields_task_and_chosen_id():
    decision, chosen = next_decision(_missing(), run_tests=lambda: False)
    assert decision.action == "task"
    assert decision.task.target_node_ids == ("pkg:requests",)
    assert decision.task.done_when == "python -c 'import requests'"
    assert chosen == "pkg:requests"


def test_clean_frontier_passing_tests_yields_done():
    decision, chosen = next_decision(_missing(State.SATISFIED), run_tests=lambda: True)
    assert decision.action == "done"
    assert chosen is None


def test_clean_frontier_failing_tests_yields_discover_task():
    decision, chosen = next_decision(DepGraph(), run_tests=lambda: False)
    assert decision.action == "task"
    assert decision.task.done_when == VERIFY_TEST_CMD
    assert chosen is None


def test_none_graph_falls_to_sufficiency():
    decision, chosen = next_decision(None, run_tests=lambda: True)
    assert decision.action == "done"
    decision2, _ = next_decision(None, run_tests=lambda: False)
    assert decision2.action == "task"   # discover task when no graph and tests red


def test_oscillation_cap_skips_over_handed_node():
    # the only frontier node is at the cap → fall to the sufficiency branch
    decision, chosen = next_decision(
        _missing(), run_tests=lambda: True, handed={"pkg:requests": 3}, attempt_cap=3,
    )
    assert decision.action == "done"   # frontier filtered empty, tests green → done
    assert chosen is None


def test_packet_to_task_maps_fields():
    g = _missing()
    t = packet_to_task(frame_obligation(g, g.get("pkg:requests")))
    assert t.target_node_ids == ("pkg:requests",)
    assert t.done_when == "python -c 'import requests'"
    assert t.layer == Layer.PIP.value


def test_residual_ids_excluded_from_frontier():
    # A node marked residual is dropped from the eligible frontier -> the
    # scheduler falls through to a discover task instead of re-handing it
    # (design: residual-node-drop.md, part a).
    decision, chosen = next_decision(
        _missing(), run_tests=lambda: False,
        residual_ids=frozenset({"pkg:requests"}),
    )
    assert chosen is None
    assert decision.task.target_node_ids == ()   # discover, not the excluded node


def test_residual_ids_default_is_noop():
    # Default frozenset() -> byte-identical to today's behavior for every
    # existing caller that doesn't pass residual_ids.
    decision, chosen = next_decision(_missing(), run_tests=lambda: False)
    assert chosen == "pkg:requests"
