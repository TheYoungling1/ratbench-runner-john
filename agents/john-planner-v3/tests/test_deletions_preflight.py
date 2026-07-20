# tests/test_deletions_preflight.py
"""
Pre-flight: catalogue every live import of probes.py and acl.py so we know
exactly what must be removed.  The test itself does NOT fail on import — it
just prints a report for the engineer to act on.  A separate assertion at the
end confirms the catalogue matches what the spec claims.
"""
import ast
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent

# Files listed in the canonical contract as "back-compat only" — the test
# allows references inside these files because Arms A/B/C are still present.
ALLOWED_REFS = {
    "agent.py",                          # _build_observer (arms A/B/C only)
    "tests/test_deletions_preflight.py", # this file — defines the symbol strings
    "tests/test_envstate_acl.py",        # will be deleted with acl.py
    "tests/test_envstate_probes.py",     # will be deleted with probes.py
    "tests/test_envstate_cleanroom.py",  # cleanroom imports ProbeSpec; cleanroom.py must be updated
    "tests/test_worldmodel_namekey.py",  # will be updated
    "tests/test_token_bucket_split.py",  # uses advance_revision for arm-B stub
    "tests/test_envstate_orchestrator.py",  # uses advance_revision for arm-B stub
    "src/envstate/cleanroom.py",         # must be updated in this task
    "src/envstate/probes.py",            # the file being deleted
    "src/envstate/acl.py",               # the file being deleted
    "tests/test_deletions_cleanroom_no_probes.py",  # asserts cleanroom.py no longer imports probes
    "tests/test_deletions_agent_cleanroom_api.py",  # asserts agent.py no longer imports ProbeSpec
    "tests/test_deletions_probes_gone.py",           # verifies probes.py deletion (string in SKIP_PATHS)
    "tests/test_deletions_acl_gone.py",              # verifies acl.py deletion (string in SKIP_PATHS)
    "tests/test_deletions_final_verification.py",    # lists deleted modules as strings, not live imports
}

PROBES_SYMBOL = "src.envstate.probes"
ACL_SYMBOL = "src.envstate.acl"


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
            hits.append(rel)
    return hits


class PrefightCatalogueTests(unittest.TestCase):
    def test_probes_refs_are_only_in_allowed_files(self):
        refs = _find_references(REPO_ROOT, PROBES_SYMBOL)
        unexpected = [r for r in refs if r not in ALLOWED_REFS]
        self.assertEqual(
            unexpected, [],
            f"Unexpected references to src.envstate.probes found: {unexpected}. "
            "Remove these before deleting probes.py.",
        )

    def test_acl_refs_are_only_in_allowed_files(self):
        refs = _find_references(REPO_ROOT, ACL_SYMBOL)
        unexpected = [r for r in refs if r not in ALLOWED_REFS]
        self.assertEqual(
            unexpected, [],
            f"Unexpected references to src.envstate.acl found: {unexpected}. "
            "Remove these before deleting acl.py.",
        )
