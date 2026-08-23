"""Docker-integration self-tests for the container-certify harness (`provision_certify`).

Network-free and deterministic: both cases use `install=[]` and commands already present
in `debian:bookworm`, so no `apt`/network is ever touched. Skips cleanly when Docker is
unavailable. Run: python3 -m pytest tests/evals/test_provision_certify.py -q -m docker
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for _p in (str(_ROOT), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evals.service_config_detection.provision_certify import (  # noqa: E402
    _docker_available,
    certify_setup,
)

pytestmark = pytest.mark.docker

_DOCKER_OK = _docker_available()


@pytest.mark.skipif(not _DOCKER_OK, reason="docker required")
def test_trivial_setup_satisfied():
    result = certify_setup({"install": [], "start": "sleep 1000 &", "probe": "true"})
    assert result.state == "SATISFIED"
    assert result.iters == 1


@pytest.mark.skipif(not _DOCKER_OK, reason="docker required")
def test_bad_probe_missing():
    result = certify_setup(
        {"install": [], "start": ":", "probe": "false"}, boot_timeout_s=4
    )
    assert result.state == "MISSING"


@pytest.mark.skipif(not _DOCKER_OK, reason="docker required")
def test_foreground_start_times_out_missing():
    """`start` with no `&`/daemonize blocks bash in the foreground forever, so the poll
    loop is never reached. Without the host-side `subprocess.run` timeout, this would hang
    `certify_setup` (and the calling test run) indefinitely. Assert it instead returns
    bounded, scored MISSING.
    """
    import time

    started = time.monotonic()
    result = certify_setup(
        {"install": [], "start": "sleep 3000", "probe": "false"}, boot_timeout_s=4
    )
    elapsed = time.monotonic() - started

    assert result.state == "MISSING"
    assert result.iters is None
    # fix bound is boot_timeout_s + _HOST_TIMEOUT_SLACK_S (~19s here); well under 60s
    # leaves headroom for slow image pulls without masking a real hang.
    assert elapsed < 60
