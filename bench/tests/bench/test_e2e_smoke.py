import shutil
import pytest
from bench.schema import HarvestedEnv, RepoSpec
from bench.measure import measure
from bench.docker_client import SubprocessDocker

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")

_DF = """FROM python:3.13-slim
RUN pip install --no-cache-dir itsdangerous pytest
RUN git clone --depth=1 https://github.com/pallets/itsdangerous /testbed
WORKDIR /testbed
"""


@pytest.mark.slow
def test_itsdangerous_measures_green():
    env = HarvestedEnv("smoke", RepoSpec("pallets/itsdangerous", "https://github.com/pallets/itsdangerous"),
                       _DF, base_image="python:3.13-slim", meta={"tokens_in": 0, "tokens_out": 0})
    row = measure(env, docker=SubprocessDocker())
    assert row.build_ok is True and row.executed is True and row.ebsr is True
    assert row.total > 0 and row.pass_rate > 0.9 and row.image_size_mb is not None
