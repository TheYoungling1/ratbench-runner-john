# producers/__init__.py
#
# The producer REGISTRY (design §4). A new method is a drop-in: one producers/<name>.py +
# one register(...) here + one varieties.toml block. bench/ and the CLI don't change.
from __future__ import annotations

import os
import sys

# Make `producers` importable when this package is driven from another tree (the RAT harness
# symlinks models into its own path; cwd/PYTHONPATH then point elsewhere). Idempotent.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from producers.base import (  # noqa: E402  (after the sys.path shim above)
    CONTRACT_VERSION,
    ProduceContext,
    ProducedEnv,
    Producer,
    write_env_packet,
)

PRODUCERS: dict = {}


def register(p):
    """Register a producer CLASS keyed by its `name` (design §4). Returns `p` (usable as a decorator)."""
    PRODUCERS[p.name] = p
    return p


def get(name: str, **kw):
    """Instantiate the registered producer `name` with the given kwargs."""
    return PRODUCERS[name](**kw)


# Concrete producers self-register here (one import + register per producer).
from producers.dockeragent import DockerAgentProducer  # noqa: E402
from producers.claudecode_dockerfile import ClaudeCodeDockerfileProducer  # noqa: E402
from producers.repo2run import Repo2RunProducer  # noqa: E402
from producers.executionagent import ExecutionAgentProducer  # noqa: E402
from producers.sweagent_repo2run import (  # noqa: E402
    SweAgentRepo2RunModernProducer,
    SweAgentRepo2RunProducer,
)
from producers.setupx import SetupXProducer  # noqa: E402
# Non-producers (measurable=False): native-lane methods with no rebuildable artifact — gated
# OUT of the fresh-container bench so they never emit a shadowing EBSR-0 (design §3 methods 5-6
# + rat "Open call", LOCKED inline-only).
from producers.rat import RatProducer  # noqa: E402
from producers.sweagent import SweAgentProducer  # noqa: E402
from producers.claudecode_live import ClaudeCodeLiveProducer  # noqa: E402

register(DockerAgentProducer)
register(ClaudeCodeDockerfileProducer)
register(Repo2RunProducer)
register(ExecutionAgentProducer)
register(SweAgentRepo2RunProducer)
register(SweAgentRepo2RunModernProducer)
register(SetupXProducer)
register(RatProducer)
register(SweAgentProducer)
register(ClaudeCodeLiveProducer)

__all__ = [
    "CONTRACT_VERSION", "ProduceContext", "ProducedEnv", "Producer", "write_env_packet",
    "PRODUCERS", "register", "get", "DockerAgentProducer",
    "ClaudeCodeDockerfileProducer", "Repo2RunProducer", "ExecutionAgentProducer",
    "SweAgentRepo2RunProducer", "SetupXProducer",
    "RatProducer", "SweAgentProducer", "ClaudeCodeLiveProducer",
]
