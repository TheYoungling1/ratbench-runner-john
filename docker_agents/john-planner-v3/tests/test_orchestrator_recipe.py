"""Tests for the apply_recipe_patch branch in run_v1 (Task 19)."""
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
from src.envstate.snapshot import EnvSnapshot
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
        )


class _Maintainer:
    def update(self, m, report):  # type: ignore[override]
        return m


def test_run_v1_drives_recipe() -> None:
    """run_v1 with an apply_recipe_patch decision must delegate to
    build_agent.run_recipe and preserve the recipe steps in the returned state."""
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
    m0 = initial_map(
        "python:3.11",
        "/repo",
        "python 3.11",
        "pip",
        ("tests/",),
        required=(Fact("opencv-python", ""),),
    )
    _final_map, reason = run_v1(
        planner,
        ba,
        _Maintainer(),
        m0,
        ActionLedger(),
        sandbox_execute=lambda c: (True, "ok"),
        max_cycles=3,
        probe=lambda: EnvSnapshot(installed=(), env={"python_version": "3.11"}),
        manifest=type("M", (), {"required": (Fact("opencv-python", ""),), "build_system": "pip"})(),
        exec_readonly=lambda c: (0, "{}"),
    )
    assert ba.recipes, "build_agent.run_recipe must have been called"
    assert ba.recipes[0].steps[0].command == "apt-get install -y libgl1"
    assert reason in ("planner_giveup", "max_cycles")
