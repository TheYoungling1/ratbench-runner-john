"""Deterministic check-quality fixes (Stage 2): rewrite brittle SystemLib checks and
reject checks structurally incapable of detecting absence. Pure; no Docker/LLM."""
from __future__ import annotations

import re

from python_deps.depgraph.schema import NodeType
from python_deps.depgraph.check_quality import check_can_detect_absence  # re-export

__all__ = ["rewrite_syslib_check", "check_can_detect_absence"]


def _node_soname(node) -> str | None:
    """Return a real shared-object soname carried by the node (e.g. 'libGL.so.1'), or None.

    A SystemLib node discovered by ldd-probe carries its soname in name/id; an apt-discovered
    node carries only the PACKAGE name (e.g. 'libglib2.0-0'), which does NOT map deterministically
    to its soname ('libglib-2.0.so.0'). We only rewrite when a true soname is available.
    """
    for cand in ((getattr(node, "name", "") or ""), node.id.split(":", 1)[-1]):
        if ".so" in cand:
            return cand
    return None


def rewrite_syslib_check(node) -> str | None:
    """Rewrite a brittle exact `dpkg -s <pkg>` SystemLib check into a soname capability
    check (`ldconfig -p | grep -qF <soname>`) that survives Debian renames (t64) — but ONLY
    when the node carries a real soname. A bare apt PACKAGE name cannot be mapped to its
    soname by string munging, so we return None rather than emit a check that false-negatives.
    """
    if node.type is not NodeType.SYSTEM_LIB or not node.check_command:
        return None
    if not re.match(r"^\s*dpkg\s+-s\s+\S+\s*$", node.check_command):
        return None
    soname = _node_soname(node)
    if soname is None:
        return None
    # -F (fixed string): '.' in the soname must match literally, not as a regex wildcard.
    return f"ldconfig -p | grep -qF {soname}"
