# tests/test_deletions_acl_gone.py
"""
acl.py must be absent and no surviving file must import from it.
Files that use advance_revision only for Arms A/B/C stubs are listed in
ALLOWED_ACL_REFS — they must be updated before acl.py is deleted.
"""
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent

ACL_FILE = REPO_ROOT / "src" / "envstate" / "acl.py"
ACL_TEST = REPO_ROOT / "tests" / "test_envstate_acl.py"

# Files allowed to still reference acl because they test arm-B/C back-compat
# paths that import advance_revision inline inside _build_observer / _run_supervisor.
# These must be cleaned up before acl.py is deleted.
ALLOWED_REFS: set[str] = {
    "agent.py",                          # _build_observer inner import (arms A/B/C only)
    "tests/test_deletions_acl_gone.py",  # this file itself
    "tests/test_deletions_preflight.py", # defines ACL_SYMBOL string constant (no import)
    "tests/test_deletions_final_verification.py",  # lists deleted modules as strings, not live imports
}

SKIP_PATHS = {
    "tests/test_deletions_acl_gone.py",
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


class AclGoneTests(unittest.TestCase):
    def test_acl_py_file_does_not_exist(self):
        self.assertFalse(
            ACL_FILE.exists(),
            f"Expected {ACL_FILE} to be deleted but it still exists.",
        )

    def test_acl_test_file_does_not_exist(self):
        self.assertFalse(
            ACL_TEST.exists(),
            f"Expected {ACL_TEST} to be deleted but it still exists.",
        )

    def test_surviving_acl_imports_are_only_allowed(self):
        refs = _find_references(REPO_ROOT, "src.envstate.acl")
        unexpected = [r for r in refs if r not in ALLOWED_REFS]
        self.assertEqual(
            unexpected,
            [],
            f"Unexpected references to src.envstate.acl found: {unexpected}. "
            "Remove these before deleting acl.py.",
        )
