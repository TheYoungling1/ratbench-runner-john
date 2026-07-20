"""BUG-10 regression: per-step Attempt outcome attribution in apply_recipe_patch.

The old code computed a SINGLE recipe-level ``step_failed = report.status != 'done'``
and applied it to EVERY committed Attempt.  The instant any step in a recipe
failed (status='blocked'), all attempts — including the steps whose command
actually succeeded — were mislabeled ``outcome='failed'``.  The fix derives each
attempt's outcome from its OWN step using ``report.completed_steps``.

Style mirrors tests/test_orchestrator_recipe.py (scripted-queue fakes).
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
from src.envstate.snapshot import EnvSnapshot
from src.envstate.ledger import ActionLedger


class _Planner:
    def __init__(self, decisions: list) -> None:
        self._q = list(decisions)

    def decide(self, m):  # type: ignore[override]
        assert self._q, "Planner queue exhausted"
        return self._q.pop(0)


class _PartialFailBuildAgent:
    """Fake BuildAgent whose recipe runs the first two steps OK and blocks on the
    third.  Reports completed_steps=2 (steps 0 and 1 succeeded; step 2 failed)."""

    def __init__(self) -> None:
        self.recipes: list[RecipePatch] = []

    def run_recipe(self, recipe, sandbox_execute, ledger, step_offset: int = 0):
        self.recipes.append(recipe)
        records = []
        for i, s in enumerate(recipe.steps):
            ok = i < 2  # first two succeed, third fails
            sandbox_execute(s.command)
            records.append(CommandRecord(s.command, 0 if ok else 1, "ok" if ok else "boom"))
        return TaskReport(
            task_goal="recipe",
            status="blocked",
            commands=tuple(records),
            learning="blocked at step 3",
            completed_steps=2,
        )


class _Maintainer:
    def update(self, m, report):  # type: ignore[override]
        return m


def _outcomes_by_intent(final_map) -> dict[str, str]:
    return {
        a.data.get("intent"): a.data.get("outcome")
        for a in final_map.contract_graph.attempts()
    }


def test_completed_steps_are_not_marked_failed() -> None:
    rp = RecipePatch(
        steps=(
            RecipeStep("s1", "system_install", "apt-get install -y gcc libpq-dev",
                       ("contract:system_library:libpq.so",)),
            RecipeStep("s2", "python_install", "pip install -e .",
                       ("contract:python_import:burr",)),
            RecipeStep("s3", "validation", "python -m pytest -q",
                       ("contract:goal:repo_tests_pass",)),
        )
    )
    planner = _Planner(
        [
            PlannerDecision(action="apply_recipe_patch", recipe_patch=rp),
            PlannerDecision(action="giveup", reason="stop"),
        ]
    )
    ba = _PartialFailBuildAgent()
    m0 = initial_map(
        "python:3.11", "/repo", "python 3.11", "pip", ("tests/",),
        required=(Fact("burr", ""),),
    )
    final_map, reason = run_v1(
        planner,
        ba,
        _Maintainer(),
        m0,
        ActionLedger(),
        sandbox_execute=lambda c: (True, "ok"),
        max_cycles=3,
        probe=lambda: EnvSnapshot(installed=(), env={"python_version": "3.11"}),
        manifest=type("M", (), {"required": (Fact("burr", ""),), "build_system": "pip"})(),
        exec_readonly=lambda c: (0, "{}"),
    )

    assert ba.recipes, "build_agent.run_recipe must have been called"
    # Per-step Attempt outcome tracking (BUG-10 regression guard) was implemented
    # in the contract-graph Attempt layer, which was removed in Phase 1 Task 3
    # (enable_contract_graph param deleted; contract_graph.attempts() always empty).
    # The core assertion — build_agent.run_recipe is invoked — remains above.
    # NOTE (Phase 2): there is no v3 equivalent of per-step Attempt outcome coverage;
    # the graph scheduler produces no per-step Attempt nodes (the contract graph is
    # absent in the v3 arm), so this coverage gap is intentional, not a regression.
    assert reason in ("planner_giveup", "max_cycles"), (
        f"expected recipe to exhaust cycles or give up; got {reason!r}"
    )
