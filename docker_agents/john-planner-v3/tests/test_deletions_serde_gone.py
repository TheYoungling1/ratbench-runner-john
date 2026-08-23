"""
src/envstate/serde.py must be absent.
No surviving file may import from it.
"""
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent

SERDE_FILE = REPO_ROOT / "src" / "envstate" / "serde.py"

SKIP_PATHS = {
    "tests/test_deletions_serde_gone.py",
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


class SerdeGoneTests(unittest.TestCase):
    def test_serde_py_does_not_exist(self):
        self.assertFalse(
            SERDE_FILE.exists(),
            f"Expected {SERDE_FILE} to be deleted but it still exists.",
        )

    def test_no_surviving_import_of_serde(self):
        refs = _find_references(REPO_ROOT, "src.envstate.serde")
        self.assertEqual(
            refs, [],
            f"Surviving imports of src.envstate.serde found: {refs}. "
            "Remove these before deleting serde.py.",
        )
