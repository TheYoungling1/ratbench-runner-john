"""Shared envstate constants.

Kept in a dependency-free leaf module so that both ``orchestrator`` and
``graph_scheduler`` can import the canonical commands without forming an
import cycle (``graph_scheduler`` previously lazy-imported this from
``orchestrator`` at call time purely to dodge the cycle).
"""
from __future__ import annotations

# Canonical execution-verify command used by the Phase-1 execution gate.
# The gate requires a bare interpreter (no venv wrapper) and >=1 passed test.
VERIFY_TEST_CMD: str = "python -m pytest -q"

# run_v3 no-progress bound: give up honestly once the VERIFY_TEST_CMD outcome
# signature is an identical FAILURE for this many consecutive cycles despite
# repair activity (design: residual-giveup-fix.md). Chosen == graph_scheduler
# attempt_cap so a single obligation gets its full retry budget before "even
# fully repairing it did not move the gate" can be concluded.
NO_PROGRESS_CYCLES: int = 3

# run_v3 residual-churn bound: give up honestly after this many consecutive
# cycles whose ONLY repair diagnosis was RESIDUAL (a test-logic / phantom
# obligation that no environment change can close) with no real ENVIRONMENT
# repair in between. Unlike NO_PROGRESS_CYCLES this does NOT depend on a stable
# pytest outcome signature — it is driven by the (reliable) repair-time
# diagnosis — so it converges even when pytest output is nondeterministic and
# a fresh phantom is minted every cycle (design: residual-node-drop.md).
RESIDUAL_GIVEUP_CYCLES: int = 3
