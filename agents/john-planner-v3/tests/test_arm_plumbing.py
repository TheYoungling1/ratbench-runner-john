"""
Tests for arm plumbing in run_rat_benchmark.py.

Covers:
- _apply_arm_env("v3") sets the full v3 flag stack
- _apply_arm_env("v1") sets only V1=1, all others 0
- _apply_arm_env("arm0") clears all flags

The module has top-level imports from eval.common (RAT repo) that are not
present in this environment.  We stub those out via sys.modules before
importing run_rat_benchmark so the _apply_arm_env helper can be tested
without any external infrastructure.
"""

import importlib
import os
import sys
import types
import unittest
from unittest.mock import MagicMock

import pytest

# ── stub out external RAT-repo imports BEFORE importing run_rat_benchmark ─────
_RAT_STUBS = [
    "eval",
    "eval.common",
    "eval.common.scorers",
    "eval.models",
    "eval.models.dockeragent_model",
    "eval.models.rat_model",
    "eval.models.repo2run_model",
    "dotenv",
]

for _mod in _RAT_STUBS:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Stub specific names that run_rat_benchmark imports from eval.common.scorers
_scorers_mock = sys.modules["eval.common.scorers"]
_scorers_mock.success_scorer = MagicMock()
_scorers_mock.pytest_pass_rate_scorer = MagicMock()
_scorers_mock.pytest_collect_scorer = MagicMock()

# Stub DockerAgentModel
_da_mock = sys.modules["eval.models.dockeragent_model"]
_da_mock.DockerAgentModel = MagicMock()

# Stub load_dotenv (from dotenv import load_dotenv)
sys.modules["dotenv"].load_dotenv = MagicMock()

# ── import the module under test ──────────────────────────────────────────────
# Insert repo root so 'run_rat_benchmark' resolves to the local copy.
_REPO_ROOT = str(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Clear any cached version so stubs take effect.
sys.modules.pop("run_rat_benchmark", None)
import run_rat_benchmark as rrb  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# _apply_arm_env tests
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyArmEnv(unittest.TestCase):
    def setUp(self):
        for k in list(os.environ):
            if k.startswith("DOCKERAGENT_ENABLE_"):
                del os.environ[k]

    def tearDown(self):
        for k in [k for k in os.environ if k.startswith("DOCKERAGENT_ENABLE_")]:
            del os.environ[k]

    def test_v3_sets_full_stack(self):
        rrb._apply_arm_env("v3")
        for var in ("V1", "DEP_GRAPH", "DEP_EMIT", "RUNTIME_FEEDBACK",
                    "GRAPH_SCHEDULER", "RUNTIME_PIN", "SERVICE_PROVISION"):
            self.assertEqual(os.environ[f"DOCKERAGENT_ENABLE_{var}"], "1", var)

    def test_v3_clears_contract_graph(self):
        rrb._apply_arm_env("v3")
        self.assertEqual(os.environ["DOCKERAGENT_ENABLE_CONTRACT_GRAPH"], "0")

    def test_v1_sets_only_v1(self):
        rrb._apply_arm_env("v1")
        self.assertEqual(os.environ["DOCKERAGENT_ENABLE_V1"], "1")
        for var in ("DEP_GRAPH", "DEP_EMIT", "RUNTIME_FEEDBACK",
                    "GRAPH_SCHEDULER", "RUNTIME_PIN", "SERVICE_PROVISION", "CONTRACT_GRAPH"):
            self.assertEqual(os.environ[f"DOCKERAGENT_ENABLE_{var}"], "0", var)

    def test_arm0_clears_all_flags(self):
        rrb._apply_arm_env("arm0")
        for var in ("V1", "DEP_GRAPH", "DEP_EMIT", "RUNTIME_FEEDBACK",
                    "GRAPH_SCHEDULER", "RUNTIME_PIN", "SERVICE_PROVISION", "CONTRACT_GRAPH"):
            self.assertEqual(os.environ[f"DOCKERAGENT_ENABLE_{var}"], "0", var)
