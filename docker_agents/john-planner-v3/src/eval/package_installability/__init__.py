"""Package-installability eval — see docs/superpowers/specs/2026-07-05-package-installability-eval-design.md."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]  # src/eval/package_installability/ -> repo root (depth 3)
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
