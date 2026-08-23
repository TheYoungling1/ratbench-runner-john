# tests/test_deletions_probes_gone.py
"""
probes.py must be absent and no surviving file must import from it.
"""
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent

PROBES_FILE = REPO_ROOT / "src" / "envstate" / "probes.py"
PROBES_TEST = REPO_ROOT / "tests" / "test_envstate_probes.py"

SKIP_PATHS = {
    "tests/test_deletions_probes_gone.py",  # this file itself
    # Files that contain the string as assertion literals, not as live imports:
    "tests/test_deletions_cleanroom_no_probes.py",
    "tests/test_deletions_agent_cleanroom_api.py",
    "tests/test_deletions_preflight.py",
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


class ProbesGoneTests(unittest.TestCase):
    def test_probes_py_file_does_not_exist(self):
        self.assertFalse(
            PROBES_FILE.exists(),
            f"Expected {PROBES_FILE} to be deleted but it still exists.",
        )

    def test_probes_test_file_does_not_exist(self):
        self.assertFalse(
            PROBES_TEST.exists(),
            f"Expected {PROBES_TEST} to be deleted but it still exists.",
        )

    def test_no_surviving_import_of_probes(self):
        refs = _find_references(REPO_ROOT, "src.envstate.probes")
        self.assertEqual(
            refs, [],
            f"Surviving imports of src.envstate.probes found: {refs}",
        )
