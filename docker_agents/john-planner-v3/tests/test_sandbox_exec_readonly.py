import unittest
from types import SimpleNamespace

from src.sandbox import Sandbox


class FakeContainer:
    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def exec_run(self, command, workdir=None):
        self.calls.append({"command": command, "workdir": workdir})
        return self._results.pop(0)


class SandboxExecReadonlyTests(unittest.TestCase):
    def _make_sandbox(self, results):
        sandbox = Sandbox.__new__(Sandbox)
        sandbox.workdir = "/app"
        sandbox.container = FakeContainer(results)
        return sandbox

    def test_returns_rc_and_decoded_output_without_side_effects(self):
        sandbox = self._make_sandbox([SimpleNamespace(exit_code=0, output=b"/usr/bin/pg_config\n")])
        rc, out = sandbox.exec_readonly("command -v pg_config")
        self.assertEqual(rc, 0)
        self.assertIn("/usr/bin/pg_config", out)
        # ran exactly one exec_run, with the raw command (no preflight, no commit)
        self.assertEqual(len(sandbox.container.calls), 1)

    def test_nonzero_exit_is_surfaced_as_rc(self):
        sandbox = self._make_sandbox([SimpleNamespace(exit_code=1, output=b"not found")])
        rc, out = sandbox.exec_readonly("command -v nope")
        self.assertEqual(rc, 1)

    def test_none_exit_code_is_normalized_to_nonzero(self):
        sandbox = self._make_sandbox([SimpleNamespace(exit_code=None, output=b"")])
        rc, out = sandbox.exec_readonly("command -v nope")
        self.assertIsNotNone(rc)
        self.assertNotEqual(rc, 0)
