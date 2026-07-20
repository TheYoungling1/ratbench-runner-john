# tests/test_orchestrator_v1_snapshot.py
from types import SimpleNamespace
from src.envstate.orchestrator import run_v1
from src.envstate.world_model import initial_map, Fact, PlannerDecision, Task, TaskReport
from src.envstate.ledger import ActionLedger


def _base():
    return initial_map(base_image="python:3.12", workdir="/app", language="python",
                       build_system="unknown", repo_layout=())


class _Planner:
    def __init__(self, actions): self.actions = list(actions); self.seen = []
    def decide(self, m):
        self.seen.append(m)
        a = self.actions.pop(0)
        if a == "task":
            return PlannerDecision(action="task", task=Task("g", "d", "deps", ()))
        return PlannerDecision(action=a)


class _Build:
    def run(self, task, ex, ledger, step_offset=0, check=None, budget=None):
        return TaskReport(task_goal="g", status="done", commands=(), learning="")


class _Maint:
    def update(self, m, report): return m


def test_probe_fills_facts_at_cycle0_and_each_cycle():
    calls = {"n": 0}
    def probe():
        calls["n"] += 1
        return SimpleNamespace(installed=(Fact("flask", "3.0.0"),), env={"arch": "x86_64"})
    man = SimpleNamespace(build_system="pip", required=(Fact("flask"),))
    planner = _Planner(["task", "done"])
    final, reason = run_v1(planner, _Build(), _Maint(), _base(), ActionLedger(),
                           lambda c: (True, ""), max_cycles=5, probe=probe, manifest=man)
    # cycle-0 fold + one cycle fold = 2 probe calls
    assert calls["n"] == 2
    # planner saw filled facts on its first decide
    assert planner.seen[0].installed == (Fact("flask", "3.0.0"),)
    assert planner.seen[0].build_system == "pip"


def test_probe_not_called_when_planner_gives_up_immediately():
    calls = {"n": 0}
    def probe():
        calls["n"] += 1
        return SimpleNamespace(installed=(), env={"arch": "x86_64"})
    man = SimpleNamespace(build_system="pip", required=())
    planner = _Planner(["giveup"])
    run_v1(planner, _Build(), _Maint(), _base(), ActionLedger(),
           lambda c: (True, ""), max_cycles=5, probe=probe, manifest=man)
    # only the cycle-0 fold ran; no per-cycle fold because planner gave up
    assert calls["n"] == 1
