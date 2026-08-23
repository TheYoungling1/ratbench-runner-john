"""
src/envstate/types.py (v0 snapshot types) must be absent.
No surviving file may import from it.
"""
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent

TYPES_FILE = REPO_ROOT / "src" / "envstate" / "types.py"

SKIP_PATHS = {
    "tests/test_deletions_types_gone.py",
    "tests/test_deletions_final_verification.py",  # lists deleted modules as strings, not live imports
}


def _find_references(root: pathlib.Path, module_substr: str) -> list[str]:
    hits = []
    for py in sorted(root.rglob("*.py")):
        if ".venv" in py.parts or "__pycache__" in py.parts:
            continue
        try:
            src = py.read_text(encoding="utf-8")
        except Exception:
            continue
        if module_substr in src:
            rel = str(py.relative_to(root))
            if rel not in SKIP_PATHS:
                hits.append(rel)
    return hits


class TypesGoneTests(unittest.TestCase):
    def test_types_py_does_not_exist(self):
        self.assertFalse(
            TYPES_FILE.exists(),
            f"Expected {TYPES_FILE} to be deleted but it still exists.",
        )

    def test_no_surviving_import_of_types(self):
        refs = _find_references(REPO_ROOT, "src.envstate.types")
        self.assertEqual(
            refs, [],
            f"Surviving imports of src.envstate.types found: {refs}. "
            "Update these to use v1 types (Fact, WorldModelMap, etc.) from "
            "src.envstate.world_model before deleting types.py.",
        )
