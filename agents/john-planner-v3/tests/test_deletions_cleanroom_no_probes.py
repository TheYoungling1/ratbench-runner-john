"""
After the migration, cleanroom.py must NOT import from src.envstate.probes.
verify_cleanroom must accept probe_commands: list[str] instead of probes: list[ProbeSpec].
"""
import inspect
import pathlib
import tempfile
import unittest

SRC = pathlib.Path(__file__).parent.parent / "src" / "envstate" / "cleanroom.py"


class CleanroomNoProbesImportTest(unittest.TestCase):
    def test_cleanroom_does_not_import_probes(self):
        text = SRC.read_text(encoding="utf-8")
        self.assertNotIn(
            "from src.envstate.probes",
            text,
            "cleanroom.py must not import from src.envstate.probes after migration",
        )

    def test_verify_cleanroom_signature_accepts_probe_commands_list(self):
        from src.envstate.cleanroom import verify_cleanroom
        sig = inspect.signature(verify_cleanroom)
        params = list(sig.parameters.keys())
        # New signature uses probe_commands not probes
        self.assertIn(
            "probe_commands",
            params,
            "verify_cleanroom must accept probe_commands: list[str] after probes.py removal",
        )
        self.assertNotIn(
            "probes",
            params,
            "verify_cleanroom must no longer accept a 'probes' parameter",
        )

    def test_verify_cleanroom_runs_with_string_commands(self):
        """verify_cleanroom works when probe_commands is a list of bare command strings."""
        from src.envstate.cleanroom import verify_cleanroom

        def fake_run(image_ref, command):
            return 0, "ok"

        class FakeImages:
            def build(self, **kwargs):
                return ("img-id", iter([]))

        class FakeClient:
            images = FakeImages()

        result = verify_cleanroom(
            FakeClient(),
            dockerfile_text="FROM python:3.11-slim\n",
            build_context_dir=tempfile.mkdtemp(),
            probe_commands=["command -v python3"],
            test_commands=["pytest --collect-only -q"],
            run_command=fake_run,
        )
        self.assertTrue(result.passed)
