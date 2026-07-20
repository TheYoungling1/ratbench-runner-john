# tests/test_depgraph_live_repair.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from python_deps.depgraph.schema import (  # noqa: E402
    DepGraph, Node, NodeType, Layer, State, DiscoveredBy,
)
from src.envstate.depgraph_live import repair_failed_nodes  # noqa: E402
from src.envstate.world_model import TaskReport  # noqa: E402


class _Ledger:                       # self-contained; the fake agent ignores it
    def append(self, *a, **k): pass
    def events(self): return []


class _FakeAgent:
    def __init__(self):
        self.tasks = []
    def run(self, task, sandbox_execute, ledger, step_offset=0, check=None, budget=8):
        self.tasks.append((task.done_when, check, budget))
        return TaskReport(task.goal, "done", (), "ok")


def _pkg(nid, name, state):
    return Node(id=nid, type=NodeType.PACKAGE, name=name, layer=Layer.PIP,
                discovered_by=DiscoveredBy.STATIC_SCAN, state=state,
                check_command=f"chk-{name}", version="1.0")


def test_repair_targets_each_failed_node_with_its_check_and_budget():
    g = DepGraph().with_node(_pkg("pkg:a", "a", State.MISSING))
    agent = _FakeAgent()
    new_graph, steps, repaired = repair_failed_nodes(
        g, agent, sandbox_execute=lambda c: (True, ""), ledger=_Ledger(),
        exec_readonly=lambda c: (0, ""),     # rc 0 → certify would pass (integer, not bool)
        step_offset=0, cycle=1, repaired_ids=set(), max_repair=3, budget=5,
    )
    assert agent.tasks == [("chk-a", "chk-a", 5)]   # check_command IS the stop; budget=5
    assert repaired == 1


def test_repair_is_capped_by_max_repair():
    g = (DepGraph()
         .with_node(_pkg("pkg:a", "a", State.MISSING))
         .with_node(_pkg("pkg:b", "b", State.MISSING))
         .with_node(_pkg("pkg:c", "c", State.MISSING)))
    agent = _FakeAgent()
    repair_failed_nodes(
        g, agent, sandbox_execute=lambda c: (True, ""), ledger=_Ledger(),
        exec_readonly=lambda c: (1, ""),     # nonzero rc → nodes stay MISSING
        step_offset=0, cycle=1, repaired_ids=set(), max_repair=2, budget=5,
    )
    assert len(agent.tasks) == 2     # capped at max_repair


def test_node_already_repaired_is_not_retried():
    g = DepGraph().with_node(_pkg("pkg:a", "a", State.MISSING))
    agent = _FakeAgent()
    seen = {"pkg:a"}                  # already repaired this run
    _, _, repaired = repair_failed_nodes(
        g, agent, sandbox_execute=lambda c: (True, ""), ledger=_Ledger(),
        exec_readonly=lambda c: (1, ""), step_offset=0, cycle=1,
        repaired_ids=seen, max_repair=3, budget=5,
    )
    assert agent.tasks == [] and repaired == 0
