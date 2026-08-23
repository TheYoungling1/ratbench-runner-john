"""Regression test for BUG-11: apply_recipe_patch with the contract graph OFF.

The planner's prompt is recipe-based, so it emits action='apply_recipe_patch'
regardless of the graph flag.  With enable_contract_graph=False the recipe must
still EXECUTE (run the commands + maintainer + honest done-gate) — it must NOT
fall through to the legacy 'task' branch whose `assert decision.task is not None`
crashes the no-graph arm at cycle 1.
"""
from __future__ import annotations

from src.envstate.orchestrator import run_v1
from src.envstate.world_model import (
    CommandRecord,
    Fact,
    PlannerDecision,
    RecipePatch,
    RecipeStep,
    TaskReport,
    initial_map,
)
from src.envstate.ledger import ActionLedger


class _Planner:
    def __init__(self, decisions: list) -> None:
        self._q = list(decisions)

    def decide(self, m):  # type: ignore[override]
        assert self._q, "Planner queue exhausted"
        return self._q.pop(0)


class _BuildAgent:
    """Fake BuildAgent that records recipes and executes steps via sandbox_execute."""

    def __init__(self) -> None:
        self.recipes: list[RecipePatch] = []

    def run_recipe(
        self,
        recipe: RecipePatch,
        sandbox_execute,
        ledger,
        step_offset: int = 0,
    ) -> TaskReport:
        self.recipes.append(recipe)
        for s in recipe.steps:
            sandbox_execute(s.command)
        return TaskReport(
            task_goal="recipe",
            status="blocked",
            commands=tuple(CommandRecord(s.command, 0, "ok") for s in recipe.steps),
            learning="ran",
            completed_steps=len(recipe.steps),
        )


class _Maintainer:
    def __init__(self) -> None:
        self.calls: list = []

    def update(self, m, report):  # type: ignore[override]
        self.calls.append(report)
        return m


def test_run_v1_executes_recipe_with_graph_off() -> None:
    """run_v1 with enable_contract_graph=False and an apply_recipe_patch decision
    must execute the recipe (no assertion crash) and run the maintainer."""
    rp = RecipePatch(
        steps=(
            RecipeStep(
                "s1",
                "system_install",
                "apt-get install -y libgl1",
                ("contract:system_library:libGL.so.1",),
            ),
        )
    )
    planner = _Planner(
        [
            PlannerDecision(action="apply_recipe_patch", recipe_patch=rp),
            PlannerDecision(action="giveup", reason="stop"),
        ]
    )
    ba = _BuildAgent()
    mt = _Maintainer()
    m0 = initial_map(
        "python:3.11",
        "/repo",
        "python 3.11",
        "pip",
        ("tests/",),
        required=(Fact("opencv-python", ""),),
    )

    # This must NOT raise (BUG-11 regression: it raised
    # "AssertionError: PlannerDecision action='task' but .task is None").
    _final_map, reason = run_v1(
        planner,
        ba,
        mt,
        m0,
        ActionLedger(),
        sandbox_execute=lambda c: (True, "ok"),
        max_cycles=3,
    )

    assert ba.recipes, "build_agent.run_recipe must have been called (recipe executed)"
    assert ba.recipes[0].steps[0].command == "apt-get install -y libgl1"
    assert mt.calls, "maintainer.update must have been called"
    assert reason in ("planner_giveup", "max_cycles")
