"""Perturb a rendered setup.sh to plant a KNOWN-root-cause failure that survives
construction. Pure text transform — returns a new script, never mutates input."""
from __future__ import annotations

from src.eval.graph_repair_ablation.oracle import Injection

_PIP_LINE_HINTS = ("pip install", "pip3 install", "python -m pip install")


def apply_injection(script: str, inj: Injection) -> str:
    op = inj.mutation.get("op")
    if op == "strip_line":
        needle = inj.mutation["match"]
        lines = script.splitlines(keepends=True)
        kept = [ln for ln in lines if needle not in ln]
        if len(kept) == len(lines):
            raise ValueError(f"strip_line match {needle!r} not found in script")
        return "".join(kept)
    if op in ("add_install_pkg", "add_pin"):
        token = (inj.mutation["pkg"] if op == "add_install_pkg"
                 else f'{inj.mutation["pkg"]}{inj.mutation["spec"]}')
        # append an explicit pip install of the offending token as the last step
        # (deterministic; independent of how the base script installs deps).
        sep = "" if script.endswith("\n") else "\n"
        return f"{script}{sep}pip install {token}\n"
    raise ValueError(f"unknown injection op: {op!r}")
