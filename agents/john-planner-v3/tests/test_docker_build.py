import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
MULTI_DOCKER_EVAL_ROOT = REPO_ROOT / "Multi-Docker-Eval"
if str(MULTI_DOCKER_EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(MULTI_DOCKER_EVAL_ROOT))

spec = importlib.util.spec_from_file_location(
    "docker_build",
    MULTI_DOCKER_EVAL_ROOT / "evaluation" / "docker_build.py",
)
docker_build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docker_build)


class DockerBuildTests(unittest.TestCase):
    def test_build_image_treats_plain_error_chunk_as_failure(self):
        class FakeAPI:
            def build(self, **_kwargs):
                return iter([
                    {"stream": "Step 1/1 : FROM python:3.11\n"},
                    {"error": "Dockerfile parse error"},
                ])

        client = SimpleNamespace(api=FakeAPI(), images=SimpleNamespace(get=lambda _name: None))

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(docker_build.BuildImageError) as context:
                docker_build.build_image(
                    image_name="test.image:latest",
                    setup_scripts={},
                    dockerfile="FROM python:3.11\n",
                    platform="linux/amd64",
                    client=client,
                    build_dir=Path(tmpdir),
                )

        self.assertIn("Dockerfile parse error", str(context.exception))

    def test_build_image_requires_created_image_tag(self):
        class FakeAPI:
            def build(self, **_kwargs):
                return iter([
                    {"stream": "Successfully built deadbeef\n"},
                ])

        def missing_image(_name):
            raise docker_build.docker.errors.ImageNotFound("missing")

        client = SimpleNamespace(api=FakeAPI(), images=SimpleNamespace(get=missing_image))

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(docker_build.BuildImageError) as context:
                docker_build.build_image(
                    image_name="test.image:latest",
                    setup_scripts={},
                    dockerfile="FROM python:3.11\n",
                    platform="linux/amd64",
                    client=client,
                    build_dir=Path(tmpdir),
                )

        self.assertIn("without creating image tag test.image:latest", str(context.exception))


if __name__ == "__main__":
    unittest.main()
