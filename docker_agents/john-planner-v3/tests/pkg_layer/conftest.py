"""Shared test scaffolding for the pkg_layer package.

Puts <repo>/src on sys.path so ``python_deps.pkg_layer.*`` imports without
installation (mirrors the pattern used by tests/depgraph/conftest.py and the
sys.path shim in top-level test files that import from python_deps).
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
