"""run_v3 bakes the python shim into rendered setup.sh (build_script.py) instead of
issuing a live, committing `ensure_python_shim(sandbox_execute)` call every cycle.
run_v1 (legacy, test-only) is untouched and still uses the live shim.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.envstate import orchestrator


def test_ensure_python_shim_absent_from_run_v3():
    src = inspect.getsource(orchestrator.run_v3)
    assert "ensure_python_shim" not in src


def test_run_v1_still_uses_live_shim():
    src = inspect.getsource(orchestrator.run_v1)
    assert "ensure_python_shim(sandbox_execute)" in src
