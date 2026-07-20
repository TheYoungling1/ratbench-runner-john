import tempfile
import unittest

from src.envstate.cleanroom import CleanroomResult, ensure_repo_in_dockerfile, verify_cleanroom


class FakeImages:
    def __init__(self, build_ok=True):
        self.build_ok = build_ok
        self.built = []

    def build(self, **kwargs):
        self.built.append(kwargs)
        if not self.build_ok:
            raise RuntimeError("build failed")
        return ("image-id", iter([]))


class FakeDockerClient:
    def __init__(self, build_ok=True):
        self.images = FakeImages(build_ok=build_ok)


class CleanroomTests(unittest.TestCase):
    def test_success_when_build_probes_and_tests_pass(self):
        client = FakeDockerClient(build_ok=True)

        def run_ok(image_ref, command):
            return 0, "ok"

        result = verify_cleanroom(
            client,
            dockerfile_text="FROM python:3.11-slim\nCOPY . /app\n",
            build_context_dir=tempfile.mkdtemp(),
            probe_commands=["command -v pg_config && pg_config --version"],
            test_commands=["pytest -q"],
            run_command=run_ok,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.reason, "clean-room verification passed")

    def test_fails_when_build_fails(self):
        client = FakeDockerClient(build_ok=False)

        def run_ok(image_ref, command):
            return 0, "ok"

        result = verify_cleanroom(
            client,
            dockerfile_text="FROM python:3.11-slim\n",
            build_context_dir=tempfile.mkdtemp(),
            probe_commands=[],
            test_commands=["pytest -q"],
            run_command=run_ok,
        )
        self.assertFalse(result.passed)
        self.assertIn("build failed", result.reason)

    def test_fails_when_probe_fails(self):
        client = FakeDockerClient()

        def run(image_ref, command):
            if "pg_config" in command:
                return 1, "not found"
            return 0, "ok"

        result = verify_cleanroom(
            client,
            dockerfile_text="FROM python:3.11-slim\n",
            build_context_dir=tempfile.mkdtemp(),
            probe_commands=["command -v pg_config && pg_config --version"],
            test_commands=["pytest -q"],
            run_command=run,
        )
        self.assertFalse(result.passed)
        self.assertIn("probe", result.reason)
        self.assertIn("command -v pg_config && pg_config --version", result.failed_probes)

    def test_fails_when_test_command_fails(self):
        client = FakeDockerClient()

        def run(image_ref, command):
            if "pytest" in command:
                return 1, "FAILED"
            return 0, "ok"

        result = verify_cleanroom(
            client,
            dockerfile_text="FROM python:3.11-slim\n",
            build_context_dir=tempfile.mkdtemp(),
            probe_commands=[],
            test_commands=["pytest -q"],
            run_command=run,
        )
        self.assertFalse(result.passed)
        self.assertIn("test command", result.reason)

    def test_fails_when_nothing_to_verify(self):
        client = FakeDockerClient()

        def run_ok(image_ref, command):
            return 0, "ok"

        result = verify_cleanroom(
            client,
            dockerfile_text="FROM python:3.11-slim\n",
            build_context_dir=tempfile.mkdtemp(),
            probe_commands=[],
            test_commands=[],
            run_command=run_ok,
        )
        self.assertFalse(result.passed)

    def test_ensure_repo_in_dockerfile_inserts_after_workdir(self):
        text = "FROM python:3.11-slim\nWORKDIR /app\nRUN pip install -e .\n"
        result = ensure_repo_in_dockerfile(text, "/app")
        lines = result.splitlines()
        workdir_idx = next(i for i, l in enumerate(lines) if l.startswith("WORKDIR"))
        copy_idx = next(i for i, l in enumerate(lines) if l.startswith("COPY . /app"))
        self.assertEqual(copy_idx, workdir_idx + 1)

    def test_ensure_repo_in_dockerfile_idempotent(self):
        text = "FROM python:3.11-slim\nWORKDIR /app\nCOPY . /app\nRUN pip install -e .\n"
        result = ensure_repo_in_dockerfile(text, "/app")
        self.assertEqual(result.count("COPY . /app"), 1)
