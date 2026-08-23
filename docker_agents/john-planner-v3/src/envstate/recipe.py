"""Fix 2 — closure-authoritative recipe builder.

Replaces the package-install portion of the ledger recipe. The captured closure is
the single source of truth: emit the frozen closure (deps) plus exactly ONE project
install (the project is excluded from the pin by name). Returns RUN-body strings in
order, suitable for ``Synthesizer.add_build_instruction``.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from src.envstate.ledger import ActionLedger
from src.envstate.project_install import resolve_project_install
from src.envstate.synthesis import PIN_PATH, build_pin_instructions


def build_closure_recipe(
    installed: Sequence,
    *,
    project_name: Optional[str],
    ledger: Optional[ActionLedger] = None,
    project_root: Optional[str] = None,
    pin_path: str = PIN_PATH,
) -> List[str]:
    """``[pin_write, pin_install, project_install?]`` — pinned closure then one project install."""
    cmds: List[str] = list(
        build_pin_instructions(installed, project_name=project_name, pin_path=pin_path)
    )
    project_install = resolve_project_install(
        installed, project_name, ledger=ledger, project_root=project_root
    )
    if project_install:
        cmds.append(project_install)
    return cmds
