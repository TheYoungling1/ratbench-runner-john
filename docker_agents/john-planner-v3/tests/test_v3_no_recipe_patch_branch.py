import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

import inspect
import src.envstate.orchestrator as orch


def test_run_v3_has_no_apply_recipe_patch_branch():
    src = inspect.getsource(orch.run_v3)
    assert "apply_recipe_patch" not in src, "dead v3 apply_recipe_patch branch must be removed"


def test_run_v1_still_has_recipe_branch():
    # guard: the deletion must not touch v1's recipe handling
    src = inspect.getsource(orch.run_v1)
    assert "RecipePatch" in src or "recipe" in src
