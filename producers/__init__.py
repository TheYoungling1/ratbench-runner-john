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

register(DockerAgentProducer)

__all__ = [
    "CONTRACT_VERSION", "ProduceContext", "ProducedEnv", "Producer", "write_env_packet",
    "PRODUCERS", "register", "get", "DockerAgentProducer",
]
